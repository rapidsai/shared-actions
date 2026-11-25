# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""The build inputs and upstream inputs a row's reuse key hashes, shared by dependency setup and the reuse plan."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_DIRECTORY = Path(__file__).parents[1] / "release-candidate-dependencies"
_SCRIPT = _DIRECTORY / "row_key_inputs.py"
_SPEC = importlib.util.spec_from_file_location("row_key_inputs", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"unable to load {_SCRIPT}")
inputs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inputs)

# setup.sh's programs before they moved into row_key_inputs.py, verbatim.
BUILD_INPUTS_JQ = """{schema_version: 1,
    train_inputs: {
      lock_receipt_sha256: $lock_receipt_sha256,
      variant_receipt_sha256: $variant_receipt_sha256
    },
    build_policy: {sccache: $sccache},
    build_implementation_repositories: $build_implementation_repositories,
    source_inputs: $source_inputs}"""
UPSTREAM_JQ = """{schema_version: 1,
    dependencies: (unique_by([.release_catalog_key, .artifact_key, .path, .sha256])
      | sort_by(.release_catalog_key, .artifact_key, .path, .sha256))}"""
REPOSITORIES = {
    "gha-tools": "rapidsai/gha-tools",
    "shared-actions": "rapidsai/shared-actions",
    "shared-workflows": "rapidsai/shared-workflows",
}
TRAIN = {
    "input_receipts": {
        "locks": {"path": "locks/index.json", "sha256": "1" * 64},
        "variants": {"path": "variants/collection.json", "sha256": "2" * 64},
    },
    "candidate_build_policy": {"sccache": True},
    "build_implementation_repositories": REPOSITORIES,
}
SOURCE_INPUTS = {"schema_version": 1, "sources": {"kvikio": "a" * 40}, "build_tools": {"rapids-cmake": "b" * 40}}


def _jq(*arguments, stdin=None):
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is not installed")
    result = subprocess.run([jq, *arguments], input=stdin, capture_output=True, text=True, check=True)  # noqa: S603
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "train",
    [
        TRAIN,
        {**TRAIN, "candidate_build_policy": {"sccache": False}},
        # Older trains name no repositories; setup defaulted to rapidsai's.
        {key: value for key, value in TRAIN.items() if key != "build_implementation_repositories"},
    ],
)
def test_build_inputs_match_the_setup_jq_they_replace(train):
    expected = _jq(
        "-cnS",
        "--arg",
        "lock_receipt_sha256",
        train["input_receipts"]["locks"]["sha256"],
        "--arg",
        "variant_receipt_sha256",
        train["input_receipts"]["variants"]["sha256"],
        "--argjson",
        "build_implementation_repositories",
        json.dumps(train.get("build_implementation_repositories", REPOSITORIES)),
        "--argjson",
        "source_inputs",
        json.dumps(SOURCE_INPUTS),
        "--argjson",
        "sccache",
        json.dumps(train["candidate_build_policy"]["sccache"]),
        BUILD_INPUTS_JQ,
    )
    assert inputs.build_inputs(train, SOURCE_INPUTS) == expected  # noqa: S101


@pytest.mark.parametrize(
    "change",
    [
        {"input_receipts": {"locks": {"sha256": "1" * 64}}},
        {"input_receipts": {**TRAIN["input_receipts"], "locks": {"path": "l", "sha256": "xyz"}}},
        {"candidate_build_policy": {"sccache": "yes"}},
        {"candidate_build_policy": None},
        {"build_implementation_repositories": {**REPOSITORIES, "extra": "rapidsai/extra"}},
        {"build_implementation_repositories": {**REPOSITORIES, "gha-tools": "someone/else"}},
    ],
)
def test_build_inputs_reject_what_setup_rejected(change):
    with pytest.raises(ValueError, match=r"."):
        inputs.build_inputs({**TRAIN, **change}, SOURCE_INPUTS)


def selected(key, artifact, path, sha256, *, repository="rapidsai/logger", digest="3"):
    return {
        "release_catalog_key": key,
        "source": {"repository": repository, "sha": "c" * 40},
        "artifact_key": f"artifacts/logger/{artifact * 64}",
        "producer_build_input_digest": digest * 64,
        "path": path,
        "package": {"name": path.split("/")[-1]},
        "sha256": sha256 * 64,
    }


SELECTED = [
    selected("conda:logger", "9", "noarch/b.conda", "5"),
    selected("conda:logger", "1", "linux-64/a.conda", "4"),
    selected("conda:logger", "1", "linux-64/a.conda", "4"),
    selected("conda:alpha", "2", "noarch/c.conda", "6", repository="rapidsai/alpha", digest="7"),
]


def _resolved(item):
    """What setup's record_resolved_input wrote for one downloaded selection."""
    return {
        "artifact_key": item["artifact_key"],
        "path": item["path"],
        "release_catalog_key": item["release_catalog_key"],
        "producer": {
            "repository": item["source"]["repository"],
            "sha": item["source"]["sha"],
            "build_input_digest": item["producer_build_input_digest"],
        },
        "sha256": item["sha256"],
        "package": item["package"],
    }


def test_upstream_document_matches_the_setup_jq_it_replaces():
    expected = _jq("-sS", UPSTREAM_JQ, stdin="\n".join(json.dumps(_resolved(item)) for item in SELECTED))
    assert inputs.upstream_document(SELECTED) == expected  # noqa: S101


def test_an_empty_selection_is_an_empty_upstream_document():
    assert inputs.upstream_document([]) == {"schema_version": 1, "dependencies": []}  # noqa: S101


def test_cli_writes_both_documents(tmp_path):
    (tmp_path / "train.json").write_text(json.dumps(TRAIN))
    (tmp_path / "source.json").write_text(json.dumps(SOURCE_INPUTS))
    (tmp_path / "selected.json").write_text(json.dumps(SELECTED))
    for arguments, expected in (
        (
            ["build-inputs", "--train", str(tmp_path / "train.json"), "--source-inputs", str(tmp_path / "source.json")],
            inputs.build_inputs(TRAIN, SOURCE_INPUTS),
        ),
        (["upstream", "--selected", str(tmp_path / "selected.json")], inputs.upstream_document(SELECTED)),
    ):
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(_SCRIPT), *arguments], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr  # noqa: S101
        assert json.loads(result.stdout) == expected  # noqa: S101
