# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# ruff: noqa: S101
"""Exercise real request vectors and fail-closed workload audit outcomes."""

import base64
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SPEC = importlib.util.spec_from_file_location(
    "boundary_probe", Path(__file__).parents[1] / "release-candidate-context/boundary_probe.py"
)
assert SPEC is not None and SPEC.loader is not None
boundary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(boundary)


@pytest.fixture
def environment():
    return {
        "GITHUB_REPOSITORY": "rapidsai/rapids-logger",
        "GITHUB_REPOSITORY_ID": "12345",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_REF": (
            "rapidsai/rapids-logger/.github/workflows/build.yaml@refs/heads/release-test-build-system-enrollment"
        ),
    }


class FakeCLI:
    def __init__(self, *, wrong_error=None, allow_operation=None, wrong_role=False):
        self.calls = []
        self.objects = {}
        self.wrong_error = wrong_error
        self.allow_operation = allow_operation
        self.wrong_role = wrong_role

    @staticmethod
    def value(argv, name):
        return argv[argv.index(name) + 1]

    @staticmethod
    def success(value):
        return subprocess.CompletedProcess([], 0, json.dumps(value), "")

    def error(self, operation, code):
        if self.wrong_error:
            return subprocess.CompletedProcess([], 1, "", self.wrong_error)
        return subprocess.CompletedProcess(
            [], 1, "", f"An error occurred ({code}) when calling the {boundary.OPERATIONS[operation]} operation: denied"
        )

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        assert kwargs == {"text": True, "capture_output": True, "check": False, "timeout": 60}
        assert argv[0] == "aws" and argv[-3:] == ["--output", "json", "--no-cli-pager"]
        operation = argv[2]
        if operation == "get-caller-identity":
            role = "administrator" if self.wrong_role else "gha-oidc-rc-producer-12345"
            return self.success(
                {"Account": "279114543810", "Arn": f"arn:aws:sts::279114543810:assumed-role/{role}/run42"}
            )
        assert self.value(argv, "--bucket") == "rapids-release-candidates"
        key = self.value(argv, "--key")
        if operation == self.allow_operation:
            return self.success({})
        if operation == "put-object":
            if "--tagging" in argv or "run-bindings/" in key or "--if-none-match" not in argv:
                return self.error(operation, "AccessDenied")
            if key in self.objects:
                return self.error(operation, "PreconditionFailed")
            self.objects[key] = Path(self.value(argv, "--body")).read_bytes()
            return self.success({"VersionId": "v1"})
        if operation == "get-object":
            assert "--version-id" not in argv
            assert self.value(argv, "--checksum-mode") == "ENABLED"
            Path(argv[-4]).write_bytes(self.objects[key])
            return self.success(
                {
                    "VersionId": "v1",
                    "ChecksumSHA256": base64.b64encode(hashlib.sha256(self.objects[key]).digest()).decode(),
                }
            )
        if operation == "get-object-tagging":
            assert "--version-id" not in argv
            return self.success({"TagSet": [], "VersionId": "v1"})
        if operation in {"put-object-tagging", "delete-object-tagging"}:
            return self.error(operation, "AccessDenied")
        raise AssertionError(f"Unexpected command {argv}")


def make_probe(environment, tmp_path, cli):
    context = boundary.validate_context(environment, "f" * 64, "a" * 40)
    return boundary.Probe(context, tmp_path, run=cli)


def test_private_workload_allow_deny_and_immutable_retry(environment, tmp_path):
    cli = FakeCLI()
    probe = make_probe(environment, tmp_path, cli)
    result = probe.execute()
    assert result["status"] == "passed"
    assert result["version_id"] == "v1"
    assert len(cli.objects) == 1
    assert probe.key == f"train-state/{'f' * 64}/rapids-logger/access-audit/42.1.json"
    assert result["gaps"] == [
        "Cross-repository binding read was not tested: no independently known existing binding supplied."
    ]
    assert len(result["checks"]) == 15
    second = make_probe(environment, tmp_path, cli)
    assert second.execute()["status"] == "passed"
    assert len(cli.objects) == 1
    assert any(c["check"] == "existing immutable probe" for c in second.report["checks"])


def test_no_delete_public_or_legitimate_binding_commands(environment, tmp_path):
    cli = FakeCLI()
    make_probe(environment, tmp_path, cli).execute()
    versioned = [argv for argv in cli.calls if "--version-id" in argv]
    assert {argv[2] for argv in versioned} == {"put-object-tagging", "delete-object-tagging"}
    assert all(FakeCLI.value(argv, "--version-id") == "v1" for argv in versioned)
    for argv in cli.calls:
        assert argv[2] != "delete-object"
        if "--key" in argv and "run-bindings/" in FakeCLI.value(argv, "--key"):
            assert "/policy-probes/42.1.json" in FakeCLI.value(argv, "--key")
    report = json.dumps(make_probe(environment, tmp_path, cli).execute())
    assert "AccessKey" not in report and "Secret" not in report and "Token" not in report


@pytest.mark.parametrize("operation", ["put-object-tagging", "delete-object-tagging"])
def test_unexpected_permission_is_fatal(environment, tmp_path, operation):
    probe = make_probe(environment, tmp_path, FakeCLI(allow_operation=operation))
    with pytest.raises(boundary.ProbeFailure, match="unexpectedly succeeded"):
        probe.execute()
    assert probe.report["status"] == "failed"


@pytest.mark.parametrize("target", ["tagged", "old-binding", "new-binding", "overwrite", "retry"])
def test_unexpected_put_permissions_are_fatal(environment, tmp_path, target):
    cli = FakeCLI()

    def run(argv, **kwargs):
        if argv[2] == "put-object":
            key = cli.value(argv, "--key")
            forbidden = {
                "tagged": "--tagging" in argv,
                "old-binding": key.startswith("train-state/run-bindings/"),
                "new-binding": key.startswith("controller-state/run-bindings/"),
                "overwrite": "--if-none-match" not in argv,
                "retry": key in cli.objects and "--tagging" not in argv,
            }
            if forbidden[target]:
                return cli.success({})
        return cli(argv, **kwargs)

    probe = make_probe(environment, tmp_path, run)
    with pytest.raises(boundary.ProbeFailure, match="unexpectedly succeeded"):
        probe.execute()
    assert probe.report["status"] == "failed"


@pytest.mark.parametrize("field,value", [("VersionId", "null"), ("ChecksumSHA256", "wrong")])
def test_missing_version_or_corrupt_checksum_is_fatal(environment, tmp_path, field, value):
    cli = FakeCLI()

    def run(argv, **kwargs):
        response = cli(argv, **kwargs)
        if argv[2] == "get-object":
            metadata = json.loads(response.stdout)
            metadata[field] = value
            return cli.success(metadata)
        return response

    with pytest.raises(boundary.ProbeFailure):
        make_probe(environment, tmp_path, run).execute()


@pytest.mark.parametrize(
    "error",
    [
        "usage: aws [options] command",
        "An error occurred (ExpiredToken) when calling the PutObject operation: expired",
        "An error occurred (AccessDenied) when calling the GetObject operation: denied",
        "AccessDenied",
        "An error occurred (InvalidRequest) when calling the PutObject operation: bad",
    ],
)
def test_other_errors_never_count_as_policy_denial(environment, tmp_path, error):
    probe = make_probe(environment, tmp_path, FakeCLI(wrong_error=error))
    with pytest.raises(boundary.ProbeFailure, match="received another failure"):
        probe.execute()


def test_expired_session_during_identity_lookup_fails(environment, tmp_path):
    def expired(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1, "", "SECRET session ExpiredToken")

    probe = make_probe(environment, tmp_path, expired)
    with pytest.raises(boundary.ProbeFailure, match="required request failed") as error:
        probe.execute()
    assert "SECRET" not in str(error.value)


def test_admin_identity_rejected_before_any_probe(environment, tmp_path):
    cli = FakeCLI(wrong_role=True)
    with pytest.raises(boundary.ProbeFailure, match="exact enrolled producer"):
        make_probe(environment, tmp_path, cli).execute()
    assert len(cli.calls) == 1 and not cli.objects


@pytest.mark.parametrize(
    "name,value",
    [
        ("GITHUB_REPOSITORY", "rapidsai/../../public"),
        ("GITHUB_RUN_ID", "0"),
        ("GITHUB_RUN_ATTEMPT", "01"),
        ("GITHUB_REPOSITORY_ID", ""),
        ("GITHUB_EVENT_NAME", "push"),
        ("GITHUB_WORKFLOW_REF", "rapidsai/rapids-logger/.github/workflows/build.yaml@refs/heads/main"),
    ],
)
def test_untrusted_context_never_reaches_cli(environment, name, value):
    environment[name] = value
    with pytest.raises(boundary.ProbeFailure):
        boundary.validate_context(environment, "f" * 64, "a" * 40)


def test_conflicting_existing_probe_bytes_reject_retry(environment, tmp_path):
    cli = FakeCLI()
    probe = make_probe(environment, tmp_path, cli)
    cli.objects[probe.key] = b"forged"
    with pytest.raises(boundary.ProbeFailure, match="bytes/checksum mismatch"):
        probe.execute()


def test_action_default_is_off_and_probe_requires_successful_resolve():
    action = yaml.safe_load((Path(__file__).parents[1] / "release-candidate-context/action.yml").read_text())
    assert action["inputs"]["audit-boundary"]["default"] == "false"
    steps = action["runs"]["steps"]
    assert steps[-2]["id"] == "resolve"
    assert steps[-1]["if"] == "${{ inputs.audit-boundary == 'true' }}"
    assert steps[-1]["env"]["TRAIN_SHA256"] == "${{ steps.resolve.outputs.train-sha256 }}"
    assert "always()" not in steps[-1]["if"]


def test_cli_failure_report_is_json_and_contains_no_credential_error(environment, monkeypatch, capsys):
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["boundary_probe.py", "--train-sha256", "f" * 64, "--source-sha", "a" * 40])

    def failed(self, *_args):
        return subprocess.CompletedProcess([], 1, "", "ExpiredToken SECRET")

    monkeypatch.setattr(boundary.Probe, "command", failed)
    with pytest.raises(SystemExit) as error:
        boundary.main()
    assert error.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed" and "SECRET" not in json.dumps(report)
