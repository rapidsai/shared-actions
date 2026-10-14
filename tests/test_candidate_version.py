# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# ruff: noqa: S101, S603, S607

"""Candidate version routing stays scoped to candidate setup."""

import json
import os
import subprocess  # nosec B404 - executes a checked-in shell helper with test inputs.
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
VERSION_OVERRIDE = ROOT / "release-candidate-dependencies" / "install-version-override.sh"
SOURCE_VERSION_SELECTOR = ROOT / "release-candidate-dependencies" / "select-cmake-source-version.sh"
SETUP_SCRIPT = ROOT / "release-candidate-dependencies" / "setup.sh"


def _run_override(
    tools_directory: Path,
    *,
    version: str | None,
    cmake_version: str | None = None,
    package_version: str | None = None,
):
    environment = os.environ.copy()
    environment.pop("RAPIDS_RELEASE_CANDIDATE_VERSION", None)
    environment.pop("RAPIDS_RELEASE_CANDIDATE_CMAKE_VERSION", None)
    if version is not None:
        environment["RAPIDS_RELEASE_CANDIDATE_VERSION"] = version
    if cmake_version is not None:
        environment["RAPIDS_RELEASE_CANDIDATE_CMAKE_VERSION"] = cmake_version
    if package_version is not None:
        environment["SETUPTOOLS_SCM_PRETEND_VERSION"] = package_version
    return subprocess.run(
        ["bash", str(VERSION_OVERRIDE), str(tools_directory)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_candidate_version_overrides_train_gha_tools_generator(tmp_path):
    train_tools = tmp_path / "gha-tools"
    candidate_tools = tmp_path / "candidate-tools"
    train_tools.mkdir()
    train_generator = train_tools / "rapids-generate-version"
    train_generator.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "26.10.00a28"\n')
    train_generator.chmod(0o755)

    result = _run_override(candidate_tools, version="26.10.00")

    assert result.returncode == 0, result.stderr
    environment = os.environ.copy()
    environment["PATH"] = f"{candidate_tools}:{train_tools}:{environment['PATH']}"
    generated = subprocess.run(["rapids-generate-version"], check=True, capture_output=True, text=True, env=environment)
    assert generated.stdout == "26.10.00\n"


@pytest.mark.parametrize(
    ("unit_id", "unit_versions", "expected"),
    [
        ("wheel:kvikio", {}, "26.12.00"),
        ("wheel:rapids-logger", {"conda:rapids-logger": "0.3.0"}, "0.3.0"),
    ],
)
def test_candidate_cmake_source_version_uses_frozen_conda_contract(tmp_path, unit_id, unit_versions, expected):
    train_file = tmp_path / "release-train.json"
    train_file.write_text(
        json.dumps(
            {
                "version_contract": {
                    "package_versions": {"wheel": "26.12.0", "conda": "26.12.00"},
                    "unit_package_versions": {"wheel:kvikio": "26.12.0", **unit_versions},
                }
            }
        )
    )

    result = subprocess.run(
        ["bash", str(SOURCE_VERSION_SELECTOR), str(train_file), unit_id],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_wheel_cmake_override_preserves_python_package_version(tmp_path):
    tools_directory = tmp_path / "candidate-tools"

    result = _run_override(
        tools_directory,
        version="26.12.0",
        cmake_version="26.12.00",
        package_version="26.12.0",
    )

    assert result.returncode == 0, result.stderr
    environment = os.environ.copy()
    environment["PATH"] = f"{tools_directory}:{environment['PATH']}"
    environment["SETUPTOOLS_SCM_PRETEND_VERSION"] = "26.12.0"
    generated = subprocess.run(["rapids-generate-version"], check=True, capture_output=True, text=True, env=environment)
    assert generated.stdout == "26.12.00\n"
    package_version = subprocess.run(
        ["bash", "-c", 'printf "%s\\n" "${SETUPTOOLS_SCM_PRETEND_VERSION}"'],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert package_version.stdout == "26.12.0\n"


@pytest.mark.parametrize("version", ["26.10.00a28", "26.10", "26.10.00\n"])
def test_invalid_candidate_version_fails_before_installing_override(tmp_path, version):
    tools_directory = tmp_path / "candidate-tools"

    result = _run_override(tools_directory, version=version)

    assert result.returncode != 0
    assert "must be a final numeric" in result.stderr
    assert not (tools_directory / "rapids-generate-version").exists()


def test_missing_candidate_version_leaves_train_generator_unchanged(tmp_path):
    train_tools = tmp_path / "gha-tools"
    candidate_tools = tmp_path / "candidate-tools"
    train_tools.mkdir()
    train_generator = train_tools / "rapids-generate-version"
    train_generator.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "26.10.00a28"\n')
    train_generator.chmod(0o755)

    result = _run_override(candidate_tools, version=None)

    assert result.returncode == 0, result.stderr
    assert not (candidate_tools / "rapids-generate-version").exists()
    environment = os.environ.copy()
    environment["PATH"] = f"{train_tools}:{environment['PATH']}"
    generated = subprocess.run(["rapids-generate-version"], check=True, capture_output=True, text=True, env=environment)
    assert generated.stdout == "26.10.00a28\n"


def test_candidate_setup_installs_override_before_adding_candidate_tools_to_path():
    source = SETUP_SCRIPT.read_text()

    install = source.index('install-version-override.sh" "${tools}"')
    source_version = source.index('RELEASE_CANDIDATE_ARTIFACT_FAMILY}" == "wheel"')
    path_add = source.index('printf \'%s\\n\' "${tools}" >>"${GITHUB_PATH}"')

    assert source_version < install
    assert install < path_add
