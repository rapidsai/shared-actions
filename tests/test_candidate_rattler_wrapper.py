# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]


@pytest.fixture
def candidate_wrapper(tmp_path):
    setup = (ROOT / "release-candidate-dependencies/setup.sh").read_text()
    # Execute the exact wrapper emitted by setup, without cloud provisioning.
    body = setup.split("cat >\"${tools}/rattler-build\" <<'EOF'\n", 1)[1].split("\nEOF\n", 1)[0]
    wrapper = tmp_path / "rattler-build"
    wrapper.write_text(body + "\n")
    original = tmp_path / "original-rattler-build"
    original.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
    original.chmod(0o755)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    python = binaries / "python3"
    python.write_text(f'#!/bin/bash\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    variants = tmp_path / "train-variants.yaml"
    variants.write_text("python:\n  - '3.12'\n")
    environment = {
        **os.environ,
        "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
        "RAPIDS_CANDIDATE_ORIGINAL_RATTLER_BUILD": str(original),
        "RAPIDS_CANDIDATE_RECIPE_PATCHER": str(ROOT / "release-candidate-dependencies/patch_recipe.py"),
        "RAPIDS_CANDIDATE_BUILD_LOCK_PACKAGE": "candidate-build-lock",
        "RAPIDS_CANDIDATE_HOST_LOCK_PACKAGE": "candidate-host-lock",
        "RAPIDS_CANDIDATE_RAPIDS_CMAKE_SHA": "a" * 40,
        "RAPIDS_CANDIDATE_RATTLER_VARIANT_CONFIG": str(variants),
    }
    return wrapper, environment


@pytest.mark.parametrize("recipe_is_directory", [True, False], ids=["directory", "file"])
@pytest.mark.parametrize(
    "build_number,expected_variant",
    [("${{ GIT_DESCRIBE_NUMBER }}", ["--variant", "GIT_DESCRIBE_NUMBER=0"]), (7, [])],
    ids=["git-build-number", "literal-build-number"],
)
def test_candidate_build_preserves_frozen_build_number_and_locks(
    tmp_path, candidate_wrapper, recipe_is_directory, build_number, expected_variant
):
    wrapper, environment = candidate_wrapper
    recipe_directory = tmp_path / "logger recipe"
    recipe_directory.mkdir()
    recipe = recipe_directory / "recipe.yaml"
    recipe.write_text(
        yaml.safe_dump({"package": {"name": "rapids-logger", "version": "0.3.0"}, "build": {"number": build_number}})
    )
    recipe_argument = recipe_directory if recipe_is_directory else recipe
    arguments = ["build", "--recipe", str(recipe_argument), "--output-dir", str(tmp_path / "output directory")]
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(wrapper), *arguments], env=environment, check=True, capture_output=True, text=True
    )
    forwarded = result.stdout.splitlines()
    patched_argument = Path(forwarded[2])
    patched_recipe = patched_argument / "recipe.yaml" if recipe_is_directory else patched_argument
    document = yaml.safe_load(patched_recipe.read_text())
    expected_arguments = [
        *arguments[:2],
        str(patched_argument),
        *arguments[3:],
        "--ignore-recipe-variants",
        "--variant-config",
        environment["RAPIDS_CANDIDATE_RATTLER_VARIANT_CONFIG"],
        *expected_variant,
    ]
    if forwarded != expected_arguments:
        pytest.fail(f"candidate renderer received different build inputs: {forwarded!r}")
    if document["build"]["number"] != build_number:
        pytest.fail("candidate recipe changed the producer's build-number expression")
    if document["requirements"] != {"build": ["candidate-build-lock"], "host": ["candidate-host-lock"]}:
        pytest.fail("candidate recipe lost its exact dependency locks")
    if patched_recipe == recipe or yaml.safe_load(recipe.read_text()).get("requirements"):
        pytest.fail("candidate build modified the original producer recipe")


def test_non_build_command_is_forwarded_without_recipe_patching(candidate_wrapper):
    wrapper, environment = candidate_wrapper
    arguments = ["--version"]
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(wrapper), *arguments], env=environment, check=True, capture_output=True, text=True
    )
    if result.stdout.splitlines() != arguments:
        pytest.fail("candidate wrapper changed a non-build invocation")
