# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
"""Probe only private candidate boundaries with the current workload identity."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess  # nosec B404 - fixed AWS CLI argument vectors, no shell.
import tempfile
from pathlib import Path

BUCKET = "rapids-release-candidates"
OPERATIONS = {
    "put-object": "PutObject",
    "get-object": "GetObject",
    "get-object-tagging": "GetObjectTagging",
    "put-object-tagging": "PutObjectTagging",
    "delete-object-tagging": "DeleteObjectTagging",
}


class ProbeFailure(ValueError):
    """A request did not establish the intended workload boundary."""


def validate_context(environment: dict[str, str], train: str, source: str) -> dict[str, str]:
    """Restrict probes to exact run-bound identities on reserved private refs."""
    repository = environment.get("GITHUB_REPOSITORY", "")
    run_id = environment.get("GITHUB_RUN_ID", "")
    attempt = environment.get("GITHUB_RUN_ATTEMPT", "")
    repository_id = environment.get("GITHUB_REPOSITORY_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository):
        raise ProbeFailure("invalid repository")
    if not all(re.fullmatch(r"[1-9][0-9]*", value) for value in (run_id, attempt, repository_id)):
        raise ProbeFailure("invalid run identity")
    if not re.fullmatch(r"[0-9a-f]{64}", train) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source):
        raise ProbeFailure("invalid frozen train/source identity")
    workflow = environment.get("GITHUB_WORKFLOW_REF", "")
    prefix = f"{repository}/.github/workflows/build.yaml@refs/heads/release-test-"
    if not workflow.startswith(prefix) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", workflow[len(prefix) :]):
        raise ProbeFailure("audit requires a reserved private workflow ref")
    if environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise ProbeFailure("audit requires workflow dispatch")
    return {
        "repository": repository,
        "repository_id": repository_id,
        "run_id": run_id,
        "run_attempt": attempt,
        "source_sha": source,
        "train_sha256": train,
    }


class Probe:
    """Keep request construction and error classification closed to callers."""

    def __init__(self, context: dict[str, str], directory: Path, run=subprocess.run):
        self.context = context
        self.directory = directory
        self.run = run
        name = context["repository"].split("/")[1]
        self.key = (
            f"train-state/{context['train_sha256']}/{name}/access-audit/"
            f"{context['run_id']}.{context['run_attempt']}.json"
        )
        self.body = (
            json.dumps(
                {"schema_version": 1, "purpose": "candidate-boundary-audit", **context},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        self.body_path = directory / "probe.json"
        self.body_path.write_bytes(self.body)
        self.checksum = base64.b64encode(hashlib.sha256(self.body).digest()).decode()
        self.report = {
            "schema_version": 1,
            "context": context,
            "bucket": BUCKET,
            "probe_key": self.key,
            "checks": [],
            "status": "failed",
            "gaps": ["Cross-repository binding read was not tested: no independently known existing binding supplied."],
        }

    def command(self, service: str, operation: str, *arguments: str):
        argv = ["aws", service, operation, *arguments, "--output", "json", "--no-cli-pager"]
        try:
            return self.run(argv, text=True, capture_output=True, check=False, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProbeFailure(f"{operation}: CLI unavailable or timed out") from error

    @staticmethod
    def result(response, operation: str) -> dict:
        if response.returncode:
            raise ProbeFailure(f"{operation}: required request failed")
        try:
            value = json.loads(response.stdout)
        except (ValueError, TypeError) as error:
            raise ProbeFailure(f"{operation}: invalid response") from error
        if not isinstance(value, dict):
            raise ProbeFailure(f"{operation}: invalid response")
        return value

    def expect_error(self, response, operation: str, code: str, label: str, key: str):
        if response.returncode == 0:
            raise ProbeFailure(f"{label}: forbidden request unexpectedly succeeded")
        expected = OPERATIONS[operation]
        match = re.search(r"An error occurred \(([^)]+)\) when calling the ([A-Za-z]+) operation:", response.stderr)
        if not match or match.groups() != (code, expected):
            raise ProbeFailure(f"{label}: expected exact {code}, received another failure")
        self.report["checks"].append({"check": label, "key": key, "result": code})

    def put(self, key: str, body: Path, *extra: str):
        return self.command("s3api", "put-object", "--bucket", BUCKET, "--key", key, "--body", str(body), *extra)

    def read_probe(self) -> dict:
        destination = self.directory / "readback.json"
        metadata = self.result(
            self.command(
                "s3api",
                "get-object",
                "--bucket",
                BUCKET,
                "--key",
                self.key,
                "--checksum-mode",
                "ENABLED",
                str(destination),
            ),
            "get-object",
        )
        if destination.read_bytes() != self.body or metadata.get("ChecksumSHA256") != self.checksum:
            raise ProbeFailure("probe bytes/checksum mismatch")
        version = metadata.get("VersionId", "")
        if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9_./+=-]{1,1024}", version) or version == "null":
            raise ProbeFailure("probe has no valid immutable version ID")
        return metadata

    def tags(self) -> dict:
        metadata = self.result(
            self.command("s3api", "get-object-tagging", "--bucket", BUCKET, "--key", self.key), "get-object-tagging"
        )
        if metadata.get("TagSet") != []:
            raise ProbeFailure("untagged probe has unexpected tags")
        return metadata

    def execute(self) -> dict:
        identity = self.result(self.command("sts", "get-caller-identity"), "get-caller-identity")
        expected_role = "gha-oidc-rc-producer-" + self.context["repository_id"]
        arn = identity.get("Arn", "")
        if identity.get("Account") != "279114543810" or not re.fullmatch(
            rf"arn:aws:sts::279114543810:assumed-role/{re.escape(expected_role)}/[A-Za-z0-9+=,.@_-]+", arn
        ):
            raise ProbeFailure("probe requires the exact enrolled producer workload role")
        self.report["role_arn"] = arn
        arguments = ("--if-none-match", "*", "--checksum-algorithm", "SHA256", "--checksum-sha256", self.checksum)
        first = self.put(self.key, self.body_path, *arguments)
        if first.returncode:
            self.expect_error(first, "put-object", "PreconditionFailed", "existing immutable probe", self.key)
        else:
            self.result(first, "put-object")
            self.report["checks"].append(
                {"check": "untagged conditional creation", "key": self.key, "result": "allowed"}
            )
        original = self.read_probe()
        self.tags()
        self.report["version_id"] = original["VersionId"]
        self.report["checksum_sha256"] = self.checksum
        self.expect_error(
            self.put(self.key, self.body_path, *arguments),
            "put-object",
            "PreconditionFailed",
            "identical conditional retry",
            self.key,
        )
        self.read_probe()
        changed = self.directory / "changed.json"
        changed.write_bytes(self.body + b" ")
        self.expect_error(
            self.put(self.key, changed, "--if-none-match", "*"),
            "put-object",
            "PreconditionFailed",
            "conflicting conditional creation",
            self.key,
        )
        self.expect_error(
            self.put(self.key, changed), "put-object", "AccessDenied", "unconditional overwrite", self.key
        )
        for value in ("candidate", "rejected", "superseded", "arbitrary"):
            key = self.key.removesuffix(".json") + f".tagged-{value}.json"
            self.expect_error(
                self.put(key, self.body_path, "--if-none-match", "*", "--tagging", f"release-candidate-status={value}"),
                "put-object",
                "AccessDenied",
                f"tagged creation {value}",
                key,
            )
        for version in ((), ("--version-id", original["VersionId"])):
            label = "version" if version else "current"
            for operation, extra in (
                (
                    "put-object-tagging",
                    ("--tagging", '{"TagSet":[{"Key":"release-candidate-status","Value":"rejected"}]}'),
                ),
                ("delete-object-tagging", ()),
            ):
                response = self.command("s3api", operation, "--bucket", BUCKET, "--key", self.key, *version, *extra)
                self.expect_error(response, operation, "AccessDenied", f"{label} {operation}", self.key)
        repository = self.context["repository"]
        suffix = f"{repository}/policy-probes/{self.context['run_id']}.{self.context['run_attempt']}.json"
        for prefix in ("train-state/run-bindings", "controller-state/run-bindings"):
            key = f"{prefix}/{suffix}"
            self.expect_error(
                self.put(key, self.body_path, "--if-none-match", "*"),
                "put-object",
                "AccessDenied",
                f"{prefix} write",
                key,
            )
        final = self.read_probe()
        self.tags()
        if final["VersionId"] != original["VersionId"]:
            raise ProbeFailure("denied requests changed probe version")
        self.report["checks"].append(
            {"check": "bytes/checksum/version/tags unchanged", "key": self.key, "result": "verified"}
        )
        self.report["status"] = "passed"
        return self.report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-sha256", required=True)
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    report = {"schema_version": 1, "status": "failed"}
    try:
        context = validate_context(dict(os.environ), args.train_sha256, args.source_sha)
        with tempfile.TemporaryDirectory(prefix="candidate-boundary-") as directory:
            probe = Probe(context, Path(directory))
            report = probe.report
            probe.execute()
    except (ProbeFailure, OSError) as error:
        report["failure"] = str(error)
        print(json.dumps(report, sort_keys=True))
        raise SystemExit(1) from error
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
