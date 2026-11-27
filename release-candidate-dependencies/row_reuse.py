#!/usr/bin/env python3
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Reuse a build row's artifacts from the newest earlier complete run with the same reuse key, or report a miss.

Dependency setup computed the row's key from every input that decides its output. Each earlier upload filed its
artifact under ``<content prefix>/<repository>/row-reuse-index/<key>/<artifact>/<run>-<attempt>.json``. A run is
taken only if, for every artifact it indexed under the key:

- the pointer's key record is exactly this row's;
- the run left a completion record (``completions/<run>.<attempt>.json``) in its train, from the same or a later
  attempt;
- its build record, artifact index and catalog still have the checksums recorded at upload, and every indexed
  payload still has the checksum its index names (whose S3 versions are recorded as reuse evidence);
- every artifact of this repository that the row consumed from its own run (``same_run_inputs``, recorded by the
  same-run downloader: kvikio Python consumes kvikio C++) has the same build input digest in this run, whether this
  run built it or reused it. The key cannot hold these: other rows of the repository may still be uploading when
  setup computes it, but the rows a row consumes always finished before it started.

Otherwise the next older run is tried, and with none the row builds. On a hit the reused references are written
into this train and this run's reuse bindings name them (``reference-binding.sh`` admits exactly those); both are
immutable, so a later attempt of the run reusing the same run again is a no-op. An earlier run of this same train
already left its original reference where the copy would go, so nothing is copied: the binding names that
reference, which must be exactly the one the index entry recorded. Writes ``reused=true|false`` and,
on a hit, ``run=<run>.<attempt>`` to --github-output.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess  # nosec B404 - the AWS CLI with fixed arguments
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

HEX64 = re.compile(r"[0-9a-f]{64}\Z")
RUN = re.compile(r"([0-9]+)-([0-9]+)\.json\Z")
SAFE = re.compile(r"[A-Za-z0-9_.-]+\Z")


class Miss(Exception):
    """This candidate run cannot be reused."""


class S3:
    def __init__(self, bucket: str) -> None:
        self.bucket = bucket
        self.completed: dict[tuple[str, str, str, str], bool] = {}
        self.completed_lock = threading.Lock()

    def _aws(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["aws", "s3api", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607 # nosec B603 B607

    def keys(self, prefix: str) -> list[str]:
        result = self._aws("list-objects-v2", "--bucket", self.bucket, "--prefix", prefix, "--output", "json")
        if result.returncode != 0:
            raise SystemExit(f"cannot list s3://{self.bucket}/{prefix}: {result.stderr.strip()}")
        return [item["Key"] for item in (json.loads(result.stdout or "{}") or {}).get("Contents", [])]

    def head(self, key: str) -> dict[str, Any] | None:
        result = self._aws("head-object", "--bucket", self.bucket, "--key", key, "--checksum-mode", "ENABLED")
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)

    def get(self, key: str) -> bytes | None:
        with tempfile.NamedTemporaryFile() as handle:
            result = self._aws("get-object", "--bucket", self.bucket, "--key", key, handle.name)
            if result.returncode != 0:
                return None
            return Path(handle.name).read_bytes()

    def put_once(self, key: str, data: bytes) -> None:
        with tempfile.NamedTemporaryFile() as handle:
            Path(handle.name).write_bytes(data)
            result = self._aws(
                "put-object", "--bucket", self.bucket, "--key", key, "--body", handle.name, "--if-none-match", "*"
            )
        if result.returncode == 0:
            return
        if self.get(key) != data:
            raise SystemExit(f"s3://{self.bucket}/{key} already exists with different content")


def _checksum(hex_digest: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_digest)).decode()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _json(s3: S3, key: str) -> Any:
    data = s3.get(key)
    if data is None:
        raise Miss(f"{key} is missing")
    try:
        return json.loads(data)
    except json.JSONDecodeError as error:
        raise Miss(f"{key} is not JSON") from error


def _unchanged(s3: S3, key: str, sha256: str) -> dict[str, Any]:
    head = s3.head(key)
    if head is None or not HEX64.fullmatch(sha256) or head.get("ChecksumSHA256") != _checksum(sha256):
        raise Miss(f"{key} is missing or changed")
    return head


def _completed(s3: S3, train_prefix: str, train: str, run: str, attempt: str) -> bool:
    # Completion records are written once and never change, so one store asks about each run once: the reuse plan
    # checks the same earlier run for every row it reuses from it.
    identity = (train_prefix, train, run, attempt)
    with s3.completed_lock:
        if identity not in s3.completed:
            s3.completed[identity] = _completion_recorded(s3, train_prefix, train, run, attempt)
        return s3.completed[identity]


def _completion_recorded(s3: S3, train_prefix: str, train: str, run: str, attempt: str) -> bool:
    prefix = f"{train_prefix}/{train}/completions/{run}."
    for key in s3.keys(prefix):
        recorded = key.removeprefix(prefix).removesuffix(".json")
        if not recorded.isdigit() or int(recorded) < int(attempt):
            continue
        record = json.loads(s3.get(key) or b"null")
        if record == {
            "schema_version": 1,
            "train_sha256": train,
            "run_id": run,
            "run_attempt": recorded,
            "result": "complete",
        }:
            return True
    return False


def _current_digest(s3: S3, args: argparse.Namespace, artifact: str) -> str:
    """The build input digest this run has for one of its repository's artifacts, built or reused, or raise Miss."""
    base = f"{args.train_prefix}/{args.train_sha256}/{args.repository.rsplit('/', 1)[-1]}"
    digests = []
    binding = s3.get(f"{base}/reuse-bindings/{args.run_id}/{artifact}.json")
    if binding is not None:
        try:
            digests.append(json.loads(binding).get("build_input_digest"))
        except (json.JSONDecodeError, AttributeError) as error:
            raise Miss(f"same-run input {artifact} has a malformed reuse binding in this run") from error
    # The newest attempt of this run, up to this one, that built it: "Re-run failed jobs" keeps the rows that
    # succeeded in an earlier attempt (26.12 release run 45926793154), as the downloader and collection read them.
    for attempt in range(int(args.run_attempt), 0, -1):
        prefix = f"{base}/{args.run_id}.{attempt}/"
        built = [
            parts[0]
            for parts in (key.removeprefix(prefix).split("/") for key in s3.keys(prefix))
            if len(parts) == 2 and parts[1] == f"bundle-reference.{artifact}.json"
        ]
        if built:
            digests.extend(built)
            break
    if len(digests) != 1 or not HEX64.fullmatch(str(digests[0])):
        raise Miss(f"same-run input {artifact} is not exactly one artifact in this run")
    return digests[0]


def _same_run_inputs_match(s3: S3, args: argparse.Namespace, pointer: dict, run: str, attempt: str) -> None:
    """Raise Miss unless every same-run input the earlier row consumed has the same digest in this run."""
    inputs = pointer.get("same_run_inputs")
    if not isinstance(inputs, list) or not all(
        isinstance(entry, dict)
        and set(entry) == {"artifact", "build_input_digest"}
        and SAFE.fullmatch(str(entry["artifact"]))
        and HEX64.fullmatch(str(entry["build_input_digest"]))
        for entry in inputs
    ):
        raise Miss(f"run {run}.{attempt} recorded no valid same-run inputs")
    for entry in inputs:
        if _current_digest(s3, args, entry["artifact"]) != entry["build_input_digest"]:
            raise Miss(f"same-run input {entry['artifact']} was built differently in this run")


def _verify(s3: S3, args: argparse.Namespace, key_record: Any, artifact: str, pointer: Any, run: str, attempt: str):
    """Return the reused reference for one indexed artifact of a candidate run, or raise Miss."""
    reference = pointer.get("reference") if isinstance(pointer, dict) else None
    if (
        not isinstance(reference, dict)
        or pointer.get("schema_version") != 1
        or pointer.get("row_reuse_key") != args.key
        or pointer.get("key_record") != key_record
        or reference.get("repository") != args.repository
        or reference.get("source_artifact") != artifact
        or (reference.get("source_run_id"), reference.get("source_run_attempt")) != (run, attempt)
        or reference.get("storage_layout") != 2
        or not HEX64.fullmatch(str(reference.get("train_sha256")))
        or not HEX64.fullmatch(str(reference.get("build_input_digest")))
    ):
        raise Miss(f"index entry for {artifact} in run {run}.{attempt} does not describe this row")
    if not _completed(s3, args.train_prefix, reference["train_sha256"], run, attempt):
        raise Miss(f"run {run}.{attempt} has no completion record")
    _same_run_inputs_match(s3, args, pointer, run, attempt)
    artifact_key = reference.get("artifact_key")
    if reference.get("artifact_index_key") != f"{artifact_key}/artifact-index.json":
        raise Miss(f"index entry for {artifact} names an inconsistent artifact index")
    _unchanged(s3, reference.get("build_record_key", ""), pointer.get("build_record_sha256", ""))
    _unchanged(s3, reference["artifact_index_key"], pointer.get("artifact_index_sha256", ""))
    _unchanged(s3, reference.get("bundle_key", ""), pointer.get("catalog_sha256", ""))
    index = _json(s3, reference["artifact_index_key"])
    entries = index.get("entries") if isinstance(index, dict) else None
    if (
        not isinstance(entries, list)
        or not entries
        or index.get("build_input_digest") != reference["build_input_digest"]
    ):
        raise Miss(f"artifact index for {artifact} is not this build's")
    objects = {}
    for entry in entries:
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str) or path.startswith("/") or ".." in path.split("/"):
            raise Miss(f"artifact index for {artifact} has an unsafe path")
        key = f"{artifact_key}/{path}"
        head = _unchanged(s3, key, str(entry.get("sha256")))
        if not head.get("VersionId"):
            raise Miss(f"{key} has no object version")
        objects[key] = {"version_id": head["VersionId"], "sha256": entry["sha256"], "size": int(head["ContentLength"])}
    if reference["train_sha256"] == args.train_sha256:
        # An earlier run of this same train: its own reference already sits where a copy would go. Bind to it
        # as it is, and only if it is exactly the reference the index entry recorded.
        repository_name = args.repository.rsplit("/", 1)[-1]
        original = (
            f"{args.train_prefix}/{args.train_sha256}/{repository_name}/{run}.{attempt}/"
            f"{reference['build_input_digest']}/bundle-reference.{artifact}.json"
        )
        if _json(s3, original) != reference:
            raise Miss(f"{original} is not the reference run {run}.{attempt} indexed")
        return reference
    return {
        **reference,
        "train_sha256": args.train_sha256,
        "reuse": {"source_train_sha256": reference["train_sha256"], "artifact_objects": objects},
    }


def find_reuse(s3: S3, args: argparse.Namespace, key_record: Any) -> tuple[str, str, dict[str, Any]] | None:
    """The newest earlier complete run indexed under the row's key, as (run, attempt, references), or None.

    ``args`` names the row: key, repository, train_sha256, run_id, run_attempt, train_prefix and content_prefix.
    """
    repository_name = args.repository.rsplit("/", 1)[-1]
    prefix = f"{args.content_prefix}/{repository_name}/row-reuse-index/{args.key}/"
    runs: dict[tuple[int, int], dict[str, str]] = {}
    for key in s3.keys(prefix):
        parts = key.removeprefix(prefix).split("/")
        match = RUN.fullmatch(parts[-1]) if len(parts) == 2 else None
        if match is None or not SAFE.fullmatch(parts[0]):
            continue
        runs.setdefault((int(match[1]), int(match[2])), {})[parts[0]] = key

    for (run_number, attempt_number), artifacts in sorted(runs.items(), reverse=True):
        run, attempt = str(run_number), str(attempt_number)
        try:
            reused = {
                artifact: _verify(s3, args, key_record, artifact, _json(s3, key), run, attempt)
                for artifact, key in sorted(artifacts.items())
            }
        except Miss as miss:
            print(f"Not reusing run {run}.{attempt}: {miss}", file=sys.stderr)
            continue
        return run, attempt, reused
    return None


def bind_reuse(s3: S3, args: argparse.Namespace, chosen: tuple[str, str, dict[str, Any]]) -> dict[str, dict]:
    """Write the reused references into this train and this run's reuse bindings; return the bound references."""
    run, attempt, reused = chosen
    repository_name = args.repository.rsplit("/", 1)[-1]
    base = f"{args.train_prefix}/{args.train_sha256}/{repository_name}"
    for artifact, reference in reused.items():
        digest = reference["build_input_digest"]
        if "reuse" in reference:
            s3.put_once(f"{base}/{run}.{attempt}/{digest}/bundle-reference.{artifact}.json", _canonical(reference))
        binding = {
            "schema_version": 1,
            "train_sha256": args.train_sha256,
            "repository": args.repository,
            "artifact": artifact,
            "run_id": args.run_id,
            "source_run_id": run,
            "source_run_attempt": attempt,
            "build_input_digest": digest,
        }
        s3.put_once(f"{base}/reuse-bindings/{args.run_id}/{artifact}.json", _canonical(binding))
        print(f"Reused {artifact} from run {run}.{attempt}.")
    return reused


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("key", "key-file", "repository", "run-id", "run-attempt", "train-sha256", "bucket", "github-output"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--train-prefix", default="train-state")
    parser.add_argument("--content-prefix", default="artifacts")
    args = parser.parse_args(argv)
    record_bytes = Path(args.key_file).read_bytes()
    if not HEX64.fullmatch(args.key) or hashlib.sha256(record_bytes).hexdigest() != args.key:
        raise SystemExit("the row reuse key does not match its key record")
    if not (
        HEX64.fullmatch(args.train_sha256)
        and args.run_id.isdigit()
        and args.run_attempt.isdigit()
        and "/" in args.repository
    ):
        raise SystemExit("invalid train, run or repository")
    if not SAFE.fullmatch(args.repository.rsplit("/", 1)[-1]):
        raise SystemExit("unsafe repository name")
    s3 = S3(args.bucket)
    chosen = find_reuse(s3, args, json.loads(record_bytes))
    with Path(args.github_output).open("a") as output:
        if chosen is None:
            print("No earlier complete build of this row; it builds.")
            output.write("reused=false\n")
            return 0
        bind_reuse(s3, args, chosen)
        output.write(f"reused=true\nrun={chosen[0]}.{chosen[1]}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
