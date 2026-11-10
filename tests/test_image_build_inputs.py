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
