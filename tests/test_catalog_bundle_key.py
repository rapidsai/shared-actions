# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Which catalog bundle a train reference may point at, including references a train reused from an earlier one."""

import json
import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[1] / "release-candidate-dependencies" / "catalog-bundle-key.sh"
_SETUP = Path(__file__).parents[1] / "release-candidate-dependencies" / "setup.sh"
TRAIN = "a" * 64
EARLIER = "c" * 64
DIGEST = "b" * 64
ARTIFACT = "ci-images-1-arm64"
DIRECTORY = f"ci-imgs/45418711352.1/{DIGEST}"


def _reference(**fields):
    reference = {
        "schema_version": 4,
        "train_sha256": TRAIN,
        "source_artifact": ARTIFACT,
        "bundle_key": f"train-state/{TRAIN}/{DIRECTORY}/release-catalog-entries.{ARTIFACT}.json",
    }
    reference.update(fields)
    return {key: value for key, value in reference.items() if value is not None}


def _reused(**fields):
    reused = {
        "bundle_key": f"train-state/{EARLIER}/{DIRECTORY}/release-catalog-entries.{ARTIFACT}.json",
        "reuse": {"source_train_sha256": EARLIER, "artifact_objects": {"x": "y"}},
    }
    reused.update(fields)
    return _reference(**reused)


def _run(tmp_path, reference, directory=DIRECTORY):
    path = tmp_path / "bundle-reference.json"
    path.write_text(json.dumps(reference))
    env = {**os.environ, "RELEASE_CANDIDATE_TRAIN_PREFIX": "train-state", "RELEASE_CANDIDATE_TRAIN_SHA256": TRAIN}
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(_SCRIPT), str(path), directory], env=env, capture_output=True, text=True, check=False
    )


def test_a_reference_built_for_this_train_keeps_its_bundle(tmp_path):
    result = _run(tmp_path, _reference())
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert result.stdout == f"train-state/{TRAIN}/{DIRECTORY}/release-catalog-entries.{ARTIFACT}.json\n"  # noqa: S101


def test_a_reused_reference_points_at_the_same_place_in_the_earlier_train(tmp_path):
    # Release run 45428761858 failed here: reuse keeps the catalog beside its original provenance.
    result = _run(tmp_path, _reused())
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert result.stdout == f"train-state/{EARLIER}/{DIRECTORY}/release-catalog-entries.{ARTIFACT}.json\n"  # noqa: S101


@pytest.mark.parametrize(
    "reference",
    [
        # Another train's bundle without a reuse record.
        _reference(bundle_key=f"train-state/{EARLIER}/{DIRECTORY}/release-catalog-entries.{ARTIFACT}.json"),
        # A reuse record naming a different train than the bundle.
        _reused(reuse={"source_train_sha256": "d" * 64}),
        # A reuse record without a full train digest.
        _reused(reuse={"source_train_sha256": "c" * 63}),
        _reused(reuse={}),
        # A reused bundle elsewhere in the earlier train: another run, digest or artifact.
        _reused(bundle_key=f"train-state/{EARLIER}/ci-imgs/1.1/{DIGEST}/release-catalog-entries.{ARTIFACT}.json"),
        _reused(bundle_key=f"train-state/{EARLIER}/{DIRECTORY}/release-catalog-entries.ci-images-2-arm64.json"),
        _reused(bundle_key=f"train-state/{EARLIER}/{DIRECTORY}/../x/release-catalog-entries.{ARTIFACT}.json"),
        # A reused reference that the current train does not own.
        _reused(train_sha256=EARLIER),
        # Unsafe keys for this train.
        _reference(bundle_key=f"train-state/{TRAIN}/{DIRECTORY}/../release-catalog-entries.{ARTIFACT}.json"),
        _reference(bundle_key=f"train-state/{TRAIN}/{DIRECTORY}/catalog.json"),
        _reference(bundle_key=None),
    ],
)
def test_unsafe_or_mismatched_bundles_fail_without_output(tmp_path, reference):
    result = _run(tmp_path, reference)
    assert result.returncode != 0  # noqa: S101
    assert "unsafe catalog bundle reference" in result.stderr  # noqa: S101
    assert result.stdout == ""  # noqa: S101


@pytest.mark.parametrize("directory", ["", "../ci-imgs/1.1", f"/{DIRECTORY}"])
def test_a_reused_reference_needs_a_safe_relative_location(tmp_path, directory):
    result = _run(tmp_path, _reused(), directory=directory)
    assert result.returncode != 0  # noqa: S101
    assert result.stdout == ""  # noqa: S101


def test_candidate_setup_uses_the_shared_bundle_check():
    setup = _SETUP.read_text()
    assert 'bash "${script_directory}/catalog-bundle-key.sh"' in setup  # noqa: S101
    assert '"${bundle_key}" != "${train_key_prefix}"/*' not in setup  # noqa: S101
