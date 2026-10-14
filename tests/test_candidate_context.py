# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
"""Candidate identity must survive unchanged project caller workflows."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "candidate_context", Path(__file__).parents[1] / "release-candidate-context/resolver.py"
)
assert SPEC is not None and SPEC.loader is not None
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)


@pytest.fixture
def environment():
    return {
        "GITHUB_REPOSITORY": "rapidsai/rapids-logger",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW_REF": "rapidsai/rapids-logger/.github/workflows/build.yaml@refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
    }


@pytest.fixture
def binding():
    return {
        "schema_version": 2,
        "repository": "rapidsai/rapids-logger",
        "run_id": "42",
        "run_attempt": 1,
        "source_sha": "a" * 40,
        "workflow": "build.yaml",
        "workflow_ref": "main",
        "event_name": "workflow_dispatch",
        "train_sha256": "f" * 64,
        "custom_jobs": {"ci/build_static.sh": "test", "ci/build_dynamic.sh": "test"},
    }


def test_standard_build_recovers_digest_without_caller_input(binding, environment):
    assert resolver.validate_binding(binding, environment, "a" * 40, "") == {
        "train-sha256": "f" * 64,
        "custom-mode": "build",
    }


@pytest.mark.parametrize("script", ["ci/build_static.sh", "ci/build_dynamic.sh"])
def test_logger_custom_tests_need_no_project_changes(binding, environment, script):
    assert resolver.validate_binding(binding, environment, "a" * 40, script)["custom-mode"] == "test"


@pytest.mark.parametrize(
    "key,value",
    [
        ("run_id", "43"),
        ("run_attempt", 2),
        ("repository", "rapidsai/kvikio"),
        ("source_sha", "b" * 40),
        ("workflow_ref", "canary"),
        ("train_sha256", "bad\noutput=wrong"),
        ("schema_version", 1),
        ("event_name", "push"),
    ],
)
def test_mismatched_binding_fails_closed(binding, environment, key, value):
    binding[key] = value
    with pytest.raises(ValueError):
        resolver.validate_binding(binding, environment, "a" * 40, "")


def test_unknown_custom_release_job_fails_closed(binding, environment):
    with pytest.raises(ValueError, match="reviewed policy"):
        resolver.validate_binding(binding, environment, "a" * 40, "ci/build_java.sh")


def test_missing_binding_retries_only_not_found(binding):
    responses = iter(
        [
            subprocess.CompletedProcess([], 1, stderr="404 Not Found"),
            subprocess.CompletedProcess([], 0, stdout=json.dumps(binding)),
        ]
    )
    sleeps = []
    assert (
        resolver.read_binding("bucket", "key", run=lambda *_args, **_kwargs: next(responses), sleep=sleeps.append)
        == binding
    )
    assert sleeps == [5]


def test_access_denied_does_not_retry_or_guess():
    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1, stderr="AccessDenied")

    with pytest.raises(ValueError, match="access"):
        resolver.read_binding("bucket", "key", run=run, sleep=lambda _: pytest.fail("unexpected sleep"))


def test_missing_binding_is_bounded():
    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1, stderr="NoSuchKey")

    sleeps = []
    with pytest.raises(ValueError, match="refusing to guess"):
        resolver.read_binding("bucket", "key", run=run, sleep=sleeps.append)
    assert sum(sleeps) == 55


def test_binding_key_uses_controller_only_namespace_and_attempt():
    key = resolver.binding_key("rapidsai/rapids-logger", "42", "1")

    assert key == "controller-state/run-bindings/rapidsai/rapids-logger/42.1.json"
    assert not key.startswith("train-state/")


@pytest.mark.parametrize(
    "repository,run_id,run_attempt",
    [
        ("rapidsai/../other", "42", "1"),
        ("rapidsai/rapids-logger", "0", "1"),
        ("rapidsai/rapids-logger", "42", "0"),
        ("rapidsai/rapids-logger", "42", "01"),
    ],
)
def test_binding_key_rejects_unsafe_identity(repository, run_id, run_attempt):
    with pytest.raises(ValueError):
        resolver.binding_key(repository, run_id, run_attempt)


def test_missing_or_invalid_attempt_fails_closed(binding, environment):
    environment.pop("GITHUB_RUN_ATTEMPT")
    with pytest.raises(ValueError, match="attempt"):
        resolver.validate_binding(binding, environment, "a" * 40, "")


def test_attempt_must_be_an_integer_in_controller_binding(binding, environment):
    binding["run_attempt"] = True
    with pytest.raises(ValueError, match="attempt"):
        resolver.validate_binding(binding, environment, "a" * 40, "")
