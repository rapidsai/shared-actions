# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""A build row's reuse key: every input that decides its output, and nothing tied to one run."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "release-candidate-dependencies" / "row_reuse_key.py"
IMAGE = "ghcr.io/rapidsai/release-candidates/ci-conda@sha256:" + "a" * 64
REVISIONS = {"gha-tools": "1" * 40, "shared-actions": "2" * 40, "shared-workflows": "3" * 40}


def build_inputs(**changes):
    value = {
        "schema_version": 1,
        "train_inputs": {"lock_receipt_sha256": "4" * 64, "variant_receipt_sha256": "5" * 64},
        "build_policy": {"sccache": True},
        "build_implementation_repositories": {"shared-actions": "rapidsai/shared-actions"},
        "source_inputs": {"kvikio": "6" * 40},
    }
    value.update(changes)
    return value


def upstream(*dependencies):
    return {"schema_version": 1, "dependencies": list(dependencies)}


def dependency(sha="7", digest="8"):
    return {
        "artifact_key": "artifacts/rapids-logger/" + digest * 64,
        "path": "linux-64/rapids-logger-0.1-h0_0.conda",
        "release_catalog_key": "conda:rapids-logger",
        "producer": {"repository": "rapidsai/rapids-logger", "sha": sha * 40, "build_input_digest": digest * 64},
        "sha256": "9" * 64,
        "package": {"name": "rapids-logger"},
    }


def compute(tmp_path, *, inputs=None, deps=None, **overrides):
    (tmp_path / "build-inputs.json").write_text(json.dumps(inputs or build_inputs()))
    (tmp_path / "upstream-inputs.json").write_text(json.dumps(deps or upstream(dependency())))
    arguments = {
        "producer-repository": "rapidsai/kvikio",
        "producer-sha": "a" * 40,
        "unit": "conda:kvikio",
        "family": "conda",
        "arch": "amd64",
        "cuda-version": "12.9.2",
        "python-version": "3.12",
        "implementation-revisions": json.dumps(REVISIONS),
        "runtime-image": IMAGE,
        "build-inputs": str(tmp_path / "build-inputs.json"),
        "upstream-inputs": str(tmp_path / "upstream-inputs.json"),
        "output": str(tmp_path / "row-reuse-key.json"),
    }
    arguments.update(overrides)
    command = [sys.executable, str(SCRIPT)]
    for name, value in arguments.items():
        if value is not None:
            command += [f"--{name}", value]
    result = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    return result, tmp_path / "row-reuse-key.json"


def test_the_key_is_the_sha256_of_the_canonical_record_it_writes(tmp_path):
    result, output = compute(tmp_path)
    assert result.returncode == 0, result.stderr  # noqa: S101
    record = output.read_bytes()
    assert result.stdout.strip() == hashlib.sha256(record).hexdigest()  # noqa: S101
    document = json.loads(record)
    assert document["runtime_image"] == IMAGE  # noqa: S101
    assert document["producer"] == {"repository": "rapidsai/kvikio", "sha": "a" * 40}  # noqa: S101
    assert document["row"] == {  # noqa: S101
        "unit": "conda:kvikio",
        "family": "conda",
        "arch": "amd64",
        "cuda_version": "12.9.2",
        "python_version": "3.12",
    }
    assert document["implementation_revisions"] == REVISIONS  # noqa: S101
    assert record == json.dumps(document, sort_keys=True, separators=(",", ":")).encode()  # noqa: S101


def key(tmp_path, name, **overrides):
    directory = tmp_path / name
    directory.mkdir()
    result, _ = compute(directory, **overrides)
    assert result.returncode == 0, result.stderr  # noqa: S101
    return result.stdout.strip()


@pytest.mark.parametrize(
    "change",
    [
        {"producer-sha": "b" * 40},
        {"arch": "arm64"},
        {"cuda-version": "13.4.3"},
        {"python-version": "3.13"},
        {"unit": "wheel:kvikio"},
        {"runtime-image": "ghcr.io/rapidsai/release-candidates/ci-conda@sha256:" + "b" * 64},
        {"implementation-revisions": json.dumps({**REVISIONS, "shared-actions": "f" * 40})},
        {"inputs": build_inputs(build_policy={"sccache": False})},
        # A rebuilt upstream row has a new digest, so every row that consumed it must rebuild too.
        {"deps": upstream(dependency(digest="c"))},
        {"deps": upstream(dependency(), dependency(sha="d", digest="e"))},
    ],
)
def test_every_input_that_decides_the_output_changes_the_key(tmp_path, change):
    assert key(tmp_path, "base") != key(tmp_path, "changed", **change)  # noqa: S101


def test_dependency_order_does_not_change_the_key(tmp_path):
    first, second = dependency(), dependency(sha="d", digest="e")
    assert key(tmp_path, "a", deps=upstream(first, second)) == key(tmp_path, "b", deps=upstream(second, first))  # noqa: S101


@pytest.mark.parametrize(
    "overrides",
    [
        {"runtime-image": None},
        {"runtime-image": ""},
        {"runtime-image": "rapidsai/ci-conda:26.12-cuda12.9.2-rockylinux8-py3.12"},
        {"runtime-image": "ghcr.io/rapidsai/release-candidates/ci-conda:latest"},
    ],
)
def test_a_row_without_a_digest_pinned_image_gets_no_key(tmp_path, overrides):
    # Without its exact environment a row cannot be matched safely: it is never indexed or reused.
    result, output = compute(tmp_path, **overrides)
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert result.stdout == ""  # noqa: S101
    assert not output.exists()  # noqa: S101
    assert "no reuse key" in result.stderr  # noqa: S101


@pytest.mark.parametrize(
    "overrides",
    [
        {"producer-sha": "abc"},
        {"implementation-revisions": "{}"},
        {"implementation-revisions": "not json"},
        {"inputs": {"schema_version": 2}},
        {"deps": {"schema_version": 1, "dependencies": "x"}},
    ],
)
def test_malformed_inputs_fail(tmp_path, overrides):
    result, output = compute(tmp_path, **overrides)
    assert result.returncode != 0  # noqa: S101
    assert not output.exists()  # noqa: S101


def test_candidate_setup_computes_and_exports_the_key_after_upstream_inputs():
    setup = (Path(__file__).parents[1] / "release-candidate-dependencies" / "setup.sh").read_text()
    computed = setup.index('python3 "${script_directory}/row_reuse_key.py"')
    assert setup.index('"${resolved_inputs}" >"${upstream_inputs}"') < computed  # noqa: S101
    assert '--runtime-image "${RELEASE_CANDIDATE_RUNTIME_IMAGE:-}"' in setup  # noqa: S101
    assert "printf 'RELEASE_CANDIDATE_ROW_REUSE_KEY=%s\\n'" in setup  # noqa: S101
