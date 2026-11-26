# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
"""The image producer and controller share a versioned canonical fixture."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "release-candidate-dependencies/image_build_inputs.py"
SPEC = importlib.util.spec_from_file_location("image_build_inputs", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot import the image build-input descriptor")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_canonical_controller_fixture_matches_the_producer_descriptor():
    fixture = json.loads((ROOT / "tests/fixtures/image-build-inputs-v1.json").read_text())
    actual = MODULE.expected_image_build_inputs(fixture["train"], fixture["package"])
    if actual != fixture["descriptor"]:
        pytest.fail("producer descriptor differs from the canonical controller fixture")
    if MODULE.image_build_inputs_sha256(actual) != fixture["descriptor_sha256"]:
        pytest.fail("producer descriptor digest differs from the canonical controller fixture")


@pytest.mark.parametrize(
    "change",
    [
        lambda t: t["candidate_image_policy"].update(mode="ignore"),
        lambda t: t["candidate_image_policy"].update(refresh_key="../unsafe"),
        lambda t: t["build_implementation_revisions"].update({"gha-tools": "main"}),
        lambda t: t["source_revisions"].update({"ci-imgs": "main"}),
        lambda t: t["release_units"][0].update(dependencies=["conda:kvikio"]),
    ],
)
def test_invalid_or_unfrozen_declared_image_inputs_fail_closed(change):
    fixture = json.loads((ROOT / "tests/fixtures/image-build-inputs-v1.json").read_text())
    change(fixture["train"])
    with pytest.raises(ValueError):
        MODULE.expected_image_build_inputs(fixture["train"], fixture["package"])


def development(train):
    train = json.loads(json.dumps(train))
    train["candidate_build_policy"]["tooling_in_reuse_keys"] = False
    return train


def test_a_development_train_leaves_the_tooling_commits_out_of_the_image_descriptor():
    # Every package row's key holds its image digest, so images must stop rebuilding on a tooling change too, or the
    # development setting would save nothing.
    fixture = json.loads((ROOT / "tests/fixtures/image-build-inputs-v1.json").read_text())
    train = development(fixture["train"])
    descriptor = MODULE.expected_image_build_inputs(train, fixture["package"])
    assert descriptor["build_implementation_revisions"] == "excluded for development"  # noqa: S101
    assert descriptor["build_policy"] == {**fixture["train"]["candidate_build_policy"], "tooling_in_reuse_keys": False}  # noqa: S101
    train["build_implementation_revisions"]["shared-actions"] = "f" * 40
    moved = MODULE.expected_image_build_inputs(train, fixture["package"])
    assert MODULE.image_build_inputs_sha256(moved) == MODULE.image_build_inputs_sha256(descriptor)  # noqa: S101
    assert MODULE.image_build_inputs_sha256(descriptor) != fixture["descriptor_sha256"]  # noqa: S101
    assert MODULE.validate_image_build_inputs(descriptor) == descriptor  # noqa: S101


@pytest.mark.parametrize(
    "change",
    [
        # The placeholder only with the setting, and real commits only without it.
        lambda d: d["build_policy"].pop("tooling_in_reuse_keys"),
        lambda d: d.update(build_implementation_revisions={"gha-tools": "1" * 40}),
        lambda d: d["build_policy"].update(tooling_in_reuse_keys="no"),
        lambda d: d["build_policy"].update(other=True),
    ],
)
def test_an_inconsistent_development_descriptor_fails(change):
    fixture = json.loads((ROOT / "tests/fixtures/image-build-inputs-v1.json").read_text())
    descriptor = MODULE.expected_image_build_inputs(development(fixture["train"]), fixture["package"])
    change(descriptor)
    with pytest.raises(ValueError):
        MODULE.validate_image_build_inputs(descriptor)
