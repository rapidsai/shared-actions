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


DEVELOPMENT = {**TRAIN, "candidate_build_policy": {"sccache": True, "tooling_in_reuse_keys": False}}


def test_a_development_train_says_so_in_its_build_inputs():
    # Leaving tooling out of reuse keys (a per-release development setting) must give keys no normal train can have.
    assert inputs.build_inputs(DEVELOPMENT, SOURCE_INPUTS)["build_policy"] == {  # noqa: S101
        "sccache": True,
        "tooling_in_reuse_keys": False,
    }
    normal = {**TRAIN, "candidate_build_policy": {"sccache": True, "tooling_in_reuse_keys": True}}
    # The normal setting changes nothing, so every existing key stays as it is.
    assert inputs.build_inputs(normal, SOURCE_INPUTS) == inputs.build_inputs(TRAIN, SOURCE_INPUTS)  # noqa: S101


@pytest.mark.parametrize(
    "policy",
    [{"sccache": True, "tooling_in_reuse_keys": "no"}, {"sccache": True, "unknown": True}],
)
def test_a_build_policy_setting_that_is_unknown_or_not_boolean_fails(policy):
    with pytest.raises(ValueError, match=r"."):
        inputs.build_inputs({**TRAIN, "candidate_build_policy": policy}, SOURCE_INPUTS)


IMAGE_ROW = {
    "ARCH": "arm64",
    "IMAGE_REPO": "ci-wheel",
    "IMAGE_NAME": "rapidsai/ci-wheel:26.12-cuda13.4.3-rockylinux8-py3.12",
    "CUDA_VER": "13.4.3",
    "PYTHON_VER": "3.12",
}
IMAGE_KEY = "ci-wheel:26.12-cuda13.4.3-rockylinux8-py3.12-arm64"
BASE = "docker.io/nvidia/cuda:13.4.3-devel-rockylinux8@sha256:" + "e" * 64
IMAGE_TRAIN = {**TRAIN, "candidate_image_bases": {IMAGE_KEY: {"BASE_IMAGE": BASE}}}


def test_an_image_key_is_the_image_maps():
    assert inputs.image_key(IMAGE_ROW) == IMAGE_KEY  # noqa: S101


@pytest.mark.parametrize(
    "change",
    [{"IMAGE_REPO": "citestwheel"}, {"ARCH": "ppc64le"}, {"IMAGE_NAME": "rapidsai/ci-conda:26.12"}, {"IMAGE_NAME": 1}],
)
def test_an_image_row_that_names_no_candidate_image_fails(change):
    with pytest.raises(ValueError, match=r"."):
        inputs.image_key({**IMAGE_ROW, **change})


def test_image_inputs_are_its_bases_refresh_generation_policy_and_tooling_repositories():
    assert inputs.image_inputs(IMAGE_TRAIN, IMAGE_ROW) == {  # noqa: S101
        "schema_version": 1,
        "base_images": {"BASE_IMAGE": BASE},
        "refresh_key": "",
        "build_policy": {"sccache": True},
        "build_implementation_repositories": REPOSITORIES,
    }
    refreshed = {**IMAGE_TRAIN, "candidate_image_policy": {"schema_version": 1, "mode": "retain", "refresh_key": "r2"}}
    assert inputs.image_inputs(refreshed, IMAGE_ROW)["refresh_key"] == "r2"  # noqa: S101
    # Not the package locks or variants: the image consumes neither, so a lock change keeps every image.
    relocked = {**IMAGE_TRAIN, "input_receipts": {**TRAIN["input_receipts"], "locks": {"sha256": "9" * 64}}}
    assert inputs.image_inputs(relocked, IMAGE_ROW) == inputs.image_inputs(IMAGE_TRAIN, IMAGE_ROW)  # noqa: S101


def test_a_train_without_base_digests_has_no_image_inputs():
    assert inputs.image_inputs(TRAIN, IMAGE_ROW) is None  # noqa: S101


@pytest.mark.parametrize(
    "bases",
    [
        {},
        {IMAGE_KEY: {}},
        {IMAGE_KEY: {"BASE_IMAGE": "docker.io/nvidia/cuda:13.4.3-devel-rockylinux8"}},
        {IMAGE_KEY: {"BASE_IMAGE": "ghcr.io/x/y:z@sha256:" + "e" * 64}},
        {"ci-wheel:other-arm64": {"BASE_IMAGE": BASE}},
    ],
)
def test_bases_missing_or_not_pinned_by_digest_fail(bases):
    with pytest.raises(ValueError, match=r"."):
        inputs.image_inputs({**TRAIN, "candidate_image_bases": bases}, IMAGE_ROW)


def test_image_refresh_is_the_trains_image_policy():
    assert inputs.image_refresh(TRAIN) is False  # noqa: S101
    policy = {"schema_version": 1, "mode": "refresh", "refresh_key": ""}
    assert inputs.image_refresh({**TRAIN, "candidate_image_policy": policy}) is True  # noqa: S101


@pytest.mark.parametrize(("train", "expected"), [(IMAGE_TRAIN, True), (TRAIN, False)])
def test_cli_prints_image_inputs_or_nothing(tmp_path, train, expected):
    (tmp_path / "train.json").write_text(json.dumps(train))
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(_SCRIPT),
            "image-inputs",
            "--train",
            str(tmp_path / "train.json"),
            "--image-row",
            json.dumps(IMAGE_ROW),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert (json.loads(result.stdout) if expected else result.stdout) == (  # noqa: S101
        inputs.image_inputs(train, IMAGE_ROW) if expected else ""
    )
