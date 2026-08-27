# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

import importlib.util
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
