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
