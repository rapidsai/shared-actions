# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

import argparse
import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[1] / "release-candidate-dependencies" / "select_wheel.py"
_SPEC = importlib.util.spec_from_file_location("select_wheel", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"unable to load wheel selector: {_SCRIPT}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
supports_python = _MODULE.supports_python

_PATCHER = Path(__file__).parents[1] / "release-candidate-dependencies" / "patch_recipe.py"
_PATCHER_SPEC = importlib.util.spec_from_file_location("patch_recipe", _PATCHER)
if _PATCHER_SPEC is None or _PATCHER_SPEC.loader is None:
    raise RuntimeError(f"unable to load recipe patcher: {_PATCHER}")
_PATCHER_MODULE = importlib.util.module_from_spec(_PATCHER_SPEC)
_PATCHER_SPEC.loader.exec_module(_PATCHER_MODULE)


@pytest.mark.parametrize(
    ("wheel", "python_version"),
    [
        ("rapids_logger-0.3.0-py3-none-any.whl", "3.10"),
        ("package-1.0.0-cp310-abi3-manylinux_2_17_x86_64.whl", "3.12"),
        ("package-1.0.0-cp312-cp312-manylinux_2_17_x86_64.whl", "3.12"),
    ],
)
def test_wheel_tags_allow_compatible_python_versions(wheel, python_version):
    if not supports_python(wheel, python_version):
        pytest.fail(f"expected {wheel} to support Python {python_version}")


@pytest.mark.parametrize(
    ("wheel", "python_version"),
    [
        ("package-1.0.0-cp310-cp310-manylinux_2_17_x86_64.whl", "3.12"),
        ("package-1.0.0-cp312-abi3-manylinux_2_17_x86_64.whl", "3.10"),
        ("not-a-wheel", "3.12"),
    ],
)
def test_wheel_tags_reject_incompatible_python_versions(wheel, python_version):
    if supports_python(wheel, python_version):
        pytest.fail(f"expected {wheel} not to support Python {python_version}")


def test_recipe_patcher_adds_lock_metapackages_to_root_and_outputs():
    recipe = {
        "requirements": {"build": ["cmake"], "host": ["zlib"]},
        "outputs": [{"package": {"name": "example"}, "requirements": {"host": ["python"]}}],
    }

    _PATCHER_MODULE._add_requirements(recipe, "candidate-build-lock", "candidate-host-lock")
    _PATCHER_MODULE._add_requirements(recipe["outputs"][0], "candidate-build-lock", "candidate-host-lock")

    expected_root = {
        "build": ["cmake", "candidate-build-lock"],
        "host": ["zlib", "candidate-host-lock"],
    }
    expected_output = {
        "build": ["candidate-build-lock"],
        "host": ["python", "candidate-host-lock"],
    }
    if recipe["requirements"] != expected_root:
        pytest.fail("root recipe requirements did not receive both lock metapackages")
    if recipe["outputs"][0]["requirements"] != expected_output:
        pytest.fail("output requirements did not receive both lock metapackages")


def test_recipe_patcher_injects_locks_only_into_multi_output_build_targets():
    recipe = {
        "requirements": {"build": ["cmake"], "host": ["zlib"]},
        "outputs": [
            {"package": {"name": "library"}, "requirements": {"host": ["python"]}},
            {"package": {"name": "tests"}, "requirements": {"build": ["ninja"]}},
        ],
    }

    _PATCHER_MODULE._prepare_recipe_documents(recipe, "candidate-build-lock", "candidate-host-lock", "deadbeef", set())

    if "requirements" in recipe:
        pytest.fail("multi-output recipe retained a forbidden top-level requirements field")
    expected_library_requirements = {
        "build": ["cmake", "candidate-build-lock"],
        "host": ["zlib", "python", "candidate-host-lock"],
    }
    if recipe["outputs"][0]["requirements"] != expected_library_requirements:
        pytest.fail("library output did not inherit shared and candidate requirements")
    expected_test_requirements = {
        "build": ["cmake", "ninja", "candidate-build-lock"],
        "host": ["zlib", "candidate-host-lock"],
    }
    if recipe["outputs"][1]["requirements"] != expected_test_requirements:
        pytest.fail("test output did not inherit shared and candidate requirements")


def test_recipe_patcher_injects_locks_into_single_output_root():
    recipe = {
        "build": {"script": "cmake -S . -B build"},
        "requirements": {"build": ["cmake"], "host": ["zlib"]},
        "tests": [{"script": "cmake -S tests -B build"}],
    }

    _PATCHER_MODULE._prepare_recipe_documents(recipe, "candidate-build-lock", "candidate-host-lock", "deadbeef", set())

    expected_requirements = {
        "build": ["cmake", "candidate-build-lock"],
        "host": ["zlib", "candidate-host-lock"],
    }
    if recipe["requirements"] != expected_requirements:
        pytest.fail("single-output root did not receive candidate requirements")
    if recipe["tests"][0]["requirements"] != {
        "build": ["candidate-build-lock"],
        "run": ["candidate-host-lock"],
    }:
        pytest.fail("package test did not receive the candidate CMake activation lock")
    if "-Drapids-cmake-sha=deadbeef" not in recipe["build"]["script"]:
        pytest.fail("package build script did not pin RAPIDS CMake")
    if "-Drapids-cmake-sha=deadbeef" not in recipe["tests"][0]["script"]:
        pytest.fail("package test script did not pin RAPIDS CMake")


def test_recipe_patcher_injects_test_lock_into_each_multi_output_test():
    recipe = {
        "outputs": [
            {"package": {"name": "library"}, "tests": [{"script": "ctest"}]},
            {"package": {"name": "bindings"}, "tests": [{"python": {"imports": ["bindings"]}}]},
        ]
    }

    _PATCHER_MODULE._prepare_recipe_documents(recipe, "candidate-build-lock", "candidate-host-lock", "deadbeef", set())

    for output in recipe["outputs"]:
        if output["tests"][0]["requirements"] != {
            "build": ["candidate-build-lock"],
            "run": ["candidate-host-lock"],
        }:
            pytest.fail("multi-output package test did not receive the candidate CMake activation lock")


def test_recipe_patcher_adapts_exact_variant_spelling_from_local_config(tmp_path):
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text("package:\n  name: example\n  version: 1\n")
    (tmp_path / "conda_build_config.yaml").write_text("libcurl_version:\n  - ==8.13.0\n")

    keys = _PATCHER_MODULE._local_exact_variant_keys(recipe)

    expected = "libcurl ==${{ libcurl_version }}"
    if _PATCHER_MODULE._add_exact_operator("libcurl ${{ libcurl_version }}", keys) != expected:
        pytest.fail("recipe patcher did not add the local exact MatchSpec operator")
    if _PATCHER_MODULE._add_exact_operator(expected, keys) != expected:
        pytest.fail("recipe patcher duplicated the local exact MatchSpec operator")


def test_recipe_patcher_accepts_recipe_directory(tmp_path):
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    expected = recipe / "recipe.yaml"
    expected.write_text("package:\n  name: example\n  version: 1\n")

    resolved = _PATCHER_MODULE._resolve_recipe(recipe, argparse.ArgumentParser())

    if resolved != expected:
        pytest.fail("recipe patcher did not resolve a recipe directory to recipe.yaml")


def test_recipe_patcher_copies_recipe_directory_with_recipe_yaml(tmp_path):
    recipe = tmp_path / "recipes" / "example"
    recipe.mkdir(parents=True)
    source_recipe = recipe / "recipe.yaml"
    source_recipe.write_text("package:\n  name: example\n  version: 1\n")
    (recipe / "build.sh").write_text("#!/usr/bin/env bash\n")

    destination = _PATCHER_MODULE._copy_recipe_directory(
        source_recipe,
        "candidate-build-lock",
        "candidate-host-lock",
    )

    if not destination.name.startswith(".example.release-candidate-"):
        pytest.fail("recipe patcher did not use a deterministic disposable directory")
    if not (destination / "recipe.yaml").is_file():
        pytest.fail("disposable recipe directory did not retain recipe.yaml")
    if not (destination / "build.sh").is_file():
        pytest.fail("disposable recipe directory did not retain recipe-local files")


@pytest.mark.parametrize(
    ("architecture", "platform"),
    [("amd64", "linux-64"), ("x86_64", "linux-64"), ("arm64", "linux-aarch64"), ("aarch64", "linux-aarch64")],
)
def test_candidate_conda_platform_normalizes_ci_architecture_names(architecture, platform):
    script = Path(__file__).parents[1] / "release-candidate-dependencies" / "platform.sh"
    # The command runs the repository-local helper with a fixed shell program.
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", 'source "$1"; conda_platform_for_arch "$2"', "bash", str(script), architecture],
        check=True,
        capture_output=True,
        text=True,
    )

    if result.stdout.strip() != platform:
        pytest.fail(f"expected {architecture} to normalize to {platform}")


def test_candidate_conda_platform_rejects_unknown_architecture():
    script = Path(__file__).parents[1] / "release-candidate-dependencies" / "platform.sh"
    # The command runs the repository-local helper with a fixed shell program.
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", 'source "$1"; conda_platform_for_arch "$2"', "bash", str(script), "not-an-arch"],
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode == 0 or "unsupported candidate architecture" not in result.stderr:
        pytest.fail("unknown candidate architecture was accepted")


def test_candidate_cmake_wrapper_pins_rapids_cmake_for_configure(tmp_path):
    output = tmp_path / "arguments.txt"
    original = tmp_path / "cmake"
    original.write_text('#!/bin/bash\nprintf \'%s\\n\' "$@" >"${TEST_OUTPUT}"\n')
    original.chmod(0o755)
    wrapper = Path(__file__).parents[1] / "release-candidate-dependencies" / "cmake.sh"
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(wrapper), "-S", ".", "-B", "build"],
        check=True,
        capture_output=True,
        text=True,
        env={
            "RAPIDS_CANDIDATE_ORIGINAL_CMAKE": str(original),
            "RAPIDS_CANDIDATE_RAPIDS_CMAKE_SHA": "a" * 40,
            "TEST_OUTPUT": str(output),
        },
    )

    if result.stdout:
        pytest.fail("candidate CMake wrapper wrote unexpected standard output")
    expected_arguments = [
        "-Drapids-cmake-sha=" + "a" * 40,
        "-S",
        ".",
        "-B",
        "build",
    ]
    if output.read_text().splitlines() != expected_arguments:
        pytest.fail("candidate CMake wrapper did not pin RAPIDS CMake during configuration")


def test_candidate_cmake_wrapper_does_not_modify_build_mode(tmp_path):
    output = tmp_path / "arguments.txt"
    original = tmp_path / "cmake"
    original.write_text('#!/bin/bash\nprintf \'%s\\n\' "$@" >"${TEST_OUTPUT}"\n')
    original.chmod(0o755)
    wrapper = Path(__file__).parents[1] / "release-candidate-dependencies" / "cmake.sh"
    subprocess.run(  # noqa: S603
        ["/bin/bash", str(wrapper), "--build", "build"],
        check=True,
        capture_output=True,
        text=True,
        env={
            "RAPIDS_CANDIDATE_ORIGINAL_CMAKE": str(original),
            "RAPIDS_CANDIDATE_RAPIDS_CMAKE_SHA": "a" * 40,
            "TEST_OUTPUT": str(output),
        },
    )

    if output.read_text().splitlines() != ["--build", "build"]:
        pytest.fail("candidate CMake wrapper modified a build-mode invocation")
