# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
"""A train-generated run builds a producer from the train repository; the controller binding is the authority."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / "release-candidate-context"


def load(name):
    spec = importlib.util.spec_from_file_location(f"producer_{name}", ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolver = load("resolver")
boundary = load("boundary_probe")

PRODUCER = ("rapidsai/kvikio", "453147629")
TRAIN_REF = "rapidsai/release-tracking/.github/workflows/candidate-build.yml@refs/heads/candidate-build-26.12"


@pytest.fixture
def environment():
    return {
        "GITHUB_REPOSITORY": "rapidsai/release-tracking",
        "GITHUB_REPOSITORY_ID": "1353637969",
        "GITHUB_RUN_ID": "99",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW_REF": TRAIN_REF,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
    }


@pytest.fixture
def binding():
    return {
        "schema_version": 3,
        "repository": "rapidsai/kvikio",
        "repository_id": "453147629",
        "run_repository": "rapidsai/release-tracking",
        "run_id": "99",
        "run_attempt": 1,
        "source_sha": "a" * 40,
        "workflow": "candidate-build.yml",
        "workflow_ref": "candidate-build-26.12",
        "event_name": "workflow_dispatch",
        "train_sha256": "f" * 64,
        "custom_jobs": {"ci/build_static.sh": "test"},
    }


def test_producer_binding_is_accepted(binding, environment):
    assert resolver.validate_binding(binding, environment, "a" * 40, "", PRODUCER) == {
        "train-sha256": "f" * 64,
        "custom-mode": "build",
    }
    assert (
        resolver.validate_binding(binding, environment, "a" * 40, "ci/build_static.sh", PRODUCER)["custom-mode"]
        == "test"
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", 2),
        ("repository", "rapidsai/rmm"),
        ("repository_id", "160453496"),
        ("run_repository", "rapidsai/kvikio"),
        ("run_id", "98"),
        ("run_attempt", 2),
        ("source_sha", "b" * 40),
        ("event_name", "push"),
        ("workflow", "build.yaml"),
        ("workflow", "candidate-build/../x.yml"),
        ("workflow_ref", "main"),
        ("workflow_ref", "candidate-build-other"),
        ("train_sha256", "bad\noutput=wrong"),
    ],
)
def test_any_mismatched_producer_fact_fails_closed(binding, environment, key, value):
    binding[key] = value
    with pytest.raises(ValueError):
        resolver.validate_binding(binding, environment, "a" * 40, "", PRODUCER)


@pytest.mark.parametrize(
    "producer",
    [("rapidsai/rmm", "453147629"), ("rapidsai/kvikio", "160453496"), ("rapidsai/kvikio", "0"), ("kvikio", "1")],
)
def test_caller_cannot_pair_a_producer_with_another_binding_or_id(binding, environment, producer):
    with pytest.raises(ValueError):
        resolver.validate_binding(binding, environment, "a" * 40, "", producer)


def test_binding_from_producer_run_does_not_validate_in_producer_mode(binding, environment):
    environment["GITHUB_WORKFLOW_REF"] = "rapidsai/kvikio/.github/workflows/build.yaml@refs/heads/main"
    with pytest.raises(ValueError, match="generated workflow ref"):
        resolver.validate_binding(binding, environment, "a" * 40, "", PRODUCER)


def test_unreviewed_custom_job_fails_in_producer_mode(binding, environment):
    with pytest.raises(ValueError, match="reviewed policy"):
        resolver.validate_binding(binding, environment, "a" * 40, "ci/build_java.sh", PRODUCER)


def test_producer_identity_needs_both_parts():
    for pair in (("rapidsai/kvikio", ""), ("", "453147629"), ("rapidsai/kvikio", "01")):
        with pytest.raises(ValueError, match="both be valid"):
            resolver.validate_producer(*pair)


def test_binding_key_for_a_producer_uses_the_train_run_id():
    key = resolver.binding_key("rapidsai/kvikio", "99", "1")
    assert key == "controller-state/run-bindings/rapidsai/kvikio/99.1.json"


def test_original_behaviour_is_unchanged_without_a_producer():
    environment = {
        "GITHUB_REPOSITORY": "rapidsai/rapids-logger",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW_REF": "rapidsai/rapids-logger/.github/workflows/build.yaml@refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
    }
    binding = {
        "schema_version": 2,
        "repository": "rapidsai/rapids-logger",
        "run_id": "42",
        "run_attempt": 1,
        "source_sha": "a" * 40,
        "workflow": "build.yaml",
        "workflow_ref": "main",
        "event_name": "workflow_dispatch",
        "train_sha256": "f" * 64,
    }
    assert resolver.validate_binding(binding, environment, "a" * 40, "")["train-sha256"] == "f" * 64


def test_resolver_cli_rejects_half_a_producer_identity(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["resolver", "--source-sha", "a" * 40, "--producer-repository", "rapidsai/kvikio"])
    monkeypatch.setenv("GITHUB_REPOSITORY", "rapidsai/release-tracking")
    monkeypatch.setenv("GITHUB_RUN_ID", "99")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setattr(resolver, "read_binding", lambda *_a, **_k: pytest.fail("binding must not be read"))
    with pytest.raises(ValueError, match="both be valid"):
        resolver.main()
    assert not (tmp_path / "out").exists()


def test_probe_context_names_producer_and_run_repository(environment):
    context = boundary.validate_context(environment, "f" * 64, "a" * 40, PRODUCER)
    assert context["repository"] == "rapidsai/kvikio"
    assert context["repository_id"] == "453147629"
    assert context["run_repository"] == "rapidsai/release-tracking"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (
            "GITHUB_WORKFLOW_REF",
            "rapidsai/release-tracking/.github/workflows/build.yaml@refs/heads/candidate-build-26.12",
        ),
        ("GITHUB_WORKFLOW_REF", "rapidsai/release-tracking/.github/workflows/candidate-build.yml@refs/heads/main"),
        (
            "GITHUB_WORKFLOW_REF",
            "rapidsai/kvikio/.github/workflows/candidate-build.yml@refs/heads/candidate-build-26.12",
        ),
        ("GITHUB_EVENT_NAME", "push"),
    ],
)
def test_probe_rejects_unreserved_generated_workflow(environment, name, value):
    environment[name] = value
    with pytest.raises(boundary.ProbeFailure):
        boundary.validate_context(environment, "f" * 64, "a" * 40, PRODUCER)


def test_probe_expects_the_producer_role_not_the_train_repository_role(environment, tmp_path):
    context = boundary.validate_context(environment, "f" * 64, "a" * 40, PRODUCER)
    wrong = f"arn:aws:sts::279114543810:assumed-role/gha-oidc-rc-producer-{environment['GITHUB_REPOSITORY_ID']}/s"
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        import json
        import subprocess

        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"Account": "279114543810", "Arn": wrong}), stderr=""
        )

    with pytest.raises(boundary.ProbeFailure, match="exact enrolled producer workload role"):
        boundary.Probe(context, tmp_path, run=run).execute()
    assert [call[:2] for call in calls] == [["aws", "sts"]]


def test_action_exposes_producer_inputs_and_passes_them_to_both_steps():
    text = (ROOT / "action.yml").read_text()
    assert "producer-repository:" in text and "producer-repository-id:" in text
    assert text.count('--producer-repository "') == 2
    assert text.count('--producer-repository-id "') == 2
