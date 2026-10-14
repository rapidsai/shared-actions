# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Tests for train-authorized system dependency pin application."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_HELPER = Path(__file__).parents[1] / "release-candidate-dependencies" / "apply_dependency_pins.py"
_SPEC = importlib.util.spec_from_file_location("apply_dependency_pins", _HELPER)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("could not load dependency pin helper")
_PINS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_PINS)


def _write_receipt(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_legacy_receipt_is_bytewise_noop(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    source = checkout / "dependencies.yaml"
    source.write_bytes(b"packages:\n  - ucx==1.17.0\n")
    before = source.read_bytes()
    _write_receipt(receipt, {"schema_version": 1})

    assert _PINS.read_pins(receipt) == {}
    assert _PINS.apply_pins(checkout, {}) == []
    assert source.read_bytes() == before


@pytest.mark.parametrize(
    "dependency_pins",
    [None, ["ucx"], {"ucx": "1.21"}, {"ucx": 1.21}, {"unknown": "1.21.0"}],
)
def test_malformed_pin_fails_before_any_checkout_edit(tmp_path: Path, dependency_pins: object) -> None:
    receipt = tmp_path / "receipt.json"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    source = checkout / "dependencies.yaml"
    source.write_text("packages:\n  - ucx==1.17.0\n", encoding="utf-8")
    before = source.read_bytes()
    _write_receipt(receipt, {"dependency_pins": dependency_pins})

    result = subprocess.run(
        [sys.executable, str(_HELPER), "--receipt", str(receipt), "--checkout", str(checkout)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "dependency pin application failed" in result.stderr
    assert source.read_bytes() == before


def test_raft_conda_and_wheel_constraints_follow_central_pin(tmp_path: Path) -> None:
    checkout = tmp_path / "raft"
    checkout.mkdir()
    dependencies = checkout / "dependencies.yaml"
    dependencies.write_text(
        """depends_on_ucx_build:
  specific:
    - output_types: [conda]
      packages:
        - ucx==1.17.0
    - output_types: [requirements, pyproject]
      packages:
        - libucx-cu12>=1.17.0,<1.17.1
        - libucx-cu13>=1.19.0,<1.19.1
        - libucx>=1.19.0,<1.19.1
""",
        encoding="utf-8",
    )
    (checkout / "python").mkdir()
    pyproject = checkout / "python" / "pyproject.toml"
    pyproject.write_text(
        '[project]\ndependencies = ["libucx>=1.19.0,<1.19.1", "numpy>=2"]\n',
        encoding="utf-8",
    )

    changed = _PINS.apply_pins(checkout, {"ucx": "1.21.0"})

    assert (
        dependencies.read_text(encoding="utf-8")
        == """depends_on_ucx_build:
  specific:
    - output_types: [conda]
      packages:
        - ucx==1.21.0
    - output_types: [requirements, pyproject]
      packages:
        - libucx-cu12>=1.21.0,<1.21.1
        - libucx-cu13>=1.21.0,<1.21.1
        - libucx>=1.21.0,<1.21.1
"""
    )
    assert '[project]\ndependencies = ["libucx>=1.21.0,<1.21.1", "numpy>=2"]' in pyproject.read_text(encoding="utf-8")
    assert pyproject in changed


def test_ucxx_recipe_runtime_build_cache_and_outputs_are_consistent(tmp_path: Path) -> None:
    checkout = tmp_path / "ucxx"
    recipe_dir = checkout / "conda" / "recipes" / "libucxx"
    recipe_dir.mkdir(parents=True)
    config = recipe_dir / "conda_build_config.yaml"
    config.write_text('ucx_min:\n  - "1.19"\n\nucx_max:\n  - "1.22"\n', encoding="utf-8")
    recipe = recipe_dir / "recipe.yaml"
    recipe.write_text(
        """cache:
  build:
    script: |
      ucx_info
      export UCX_HOME=/opt/ucx
requirements:
  build:
    - ucx =${{ ucx_min }}
outputs:
  - package:
      name: libucxx
    requirements:
      host:
        - ucx =${{ ucx_min }}
        - ${{ pin_compatible("ucx", upper_bound="x") }}
      run:
        - ucx >=${{ ucx_min }},<${{ ucx_max }}
      run_exports:
        - ucx >=1.19.0,<1.22
  - package:
      name: libucxx-tests
    requirements:
      test:
        - ucx ==1.19.0
""",
        encoding="utf-8",
    )

    _PINS.apply_pins(checkout, {"ucx": "1.21.0"})

    assert config.read_text(encoding="utf-8") == 'ucx_min:\n  - "1.21.0"\n\nucx_max:\n  - "1.21.1"\n'
    contents = recipe.read_text(encoding="utf-8")
    assert contents.count("ucx==1.21.0") == 5
    assert "run_exports:\n        - ucx==1.21.0" in contents
    assert "ucx ==1.19.0" not in contents
    assert 'pin_compatible("ucx", upper_bound="x")' in contents
    assert "      ucx_info\n      export UCX_HOME=/opt/ucx\n" in contents


def test_pin_is_idempotent_and_does_not_add_missing_dependency_group(tmp_path: Path) -> None:
    checkout = tmp_path / "consumer"
    checkout.mkdir()
    source = checkout / "dependencies.yaml"
    source.write_text("depends_on_cuda:\n  packages:\n    - cuda-version>=12\n", encoding="utf-8")
    before = source.read_bytes()

    assert _PINS.apply_pins(checkout, {"ucx": "1.21.0"}) == []
    assert source.read_bytes() == before

    source.write_text("depends_on_ucx:\n  packages:\n    - libucx>=1.21.0,<1.21.1\n", encoding="utf-8")
    first = source.read_bytes()
    _PINS.apply_pins(checkout, {"ucx": "1.21.0"})
    assert source.read_bytes() == first


def test_unversioned_pin_preserves_extras_markers_and_unrelated_ucxx(tmp_path: Path) -> None:
    checkout = tmp_path / "consumer"
    checkout.mkdir()
    pyproject = checkout / "pyproject.toml"
    pyproject.write_text(
        """[project]
name = "ucx"
homepage = "https://example.invalid/path/ucx"
dependencies = [
    "libucx[ucx]>=1.19.0,<1.22; python_version < '3.14'",
    "ucx",
    "ucxx>=0.1",
]
""",
        encoding="utf-8",
    )

    _PINS.apply_pins(checkout, {"ucx": "1.21.0"})

    assert pyproject.read_text(encoding="utf-8") == (
        """[project]
name = "ucx"
homepage = "https://example.invalid/path/ucx"
dependencies = [
    "libucx[ucx]>=1.21.0,<1.21.1; python_version < '3.14'",
    "ucx==1.21.0",
    "ucxx>=0.1",
]
"""
    )

    env_dir = checkout / "conda" / "environments"
    env_dir.mkdir(parents=True)
    environment = env_dir / "all_cuda.yaml"
    environment.write_text(
        "name: build\ndependencies: [ucx>=1.19.0,<1.22]\n",
        encoding="utf-8",
    )
    _PINS.apply_pins(checkout, {"ucx": "1.21.0"})
    assert "dependencies: [ucx==1.21.0]" in environment.read_text(encoding="utf-8")

    dependencies = checkout / "dependencies.yaml"
    dependencies.write_text(
        "depends_on_ucx:\n  packages: [ucx==1.19.0, libucx>=1.19.0,<1.19.1]\n",
        encoding="utf-8",
    )
    _PINS.apply_pins(checkout, {"ucx": "1.21.0"})
    assert "packages: [ucx==1.21.0, libucx>=1.21.0,<1.21.1]" in dependencies.read_text(encoding="utf-8")

    config = checkout / "conda" / "recipes" / "ucx" / "conda_build_config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text('ucx_min: ["1.19"]\nucx_version: [1.19.0]\n', encoding="utf-8")
    _PINS.apply_pins(checkout, {"ucx": "1.21.0"})
    assert config.read_text(encoding="utf-8") == 'ucx_min: ["1.21.0"]\nucx_version: ["1.21.0"]\n'


def test_direct_url_requirement_fails_before_any_checkout_edit(tmp_path: Path) -> None:
    checkout = tmp_path / "consumer"
    checkout.mkdir()
    pyproject = checkout / "pyproject.toml"
    pyproject.write_text(
        '[project]\ndependencies = ["libucx @ https://example.invalid/libucx.whl"]\n',
        encoding="utf-8",
    )
    before = pyproject.read_bytes()

    with pytest.raises(_PINS.PinError, match="direct URL"):
        _PINS.apply_pins(checkout, {"ucx": "1.21.0"})

    assert pyproject.read_bytes() == before


def test_target_dependency_symlink_outside_checkout_fails_before_edits(tmp_path: Path) -> None:
    checkout = tmp_path / "consumer"
    checkout.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("packages:\n  - ucx==1.17.0\n", encoding="utf-8")
    symlink = checkout / "dependencies.yaml"
    symlink.symlink_to(outside)

    with pytest.raises(_PINS.PinError, match="outside candidate checkout"):
        _PINS.apply_pins(checkout, {"ucx": "1.21.0"})

    assert outside.read_text(encoding="utf-8") == "packages:\n  - ucx==1.17.0\n"
