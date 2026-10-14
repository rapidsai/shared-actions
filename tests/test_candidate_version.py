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
UNIT_VERSION_SELECTOR = ROOT / "release-candidate-dependencies" / "select-unit-version.sh"
UCXX_VERSION_PREPARATION_TEST = ROOT / "tests" / "test_prepare_ucxx_wheel_version.sh"
SETUP_SCRIPT = ROOT / "release-candidate-dependencies" / "setup.sh"


def _run_override(
    tools_directory: Path,
    *,
    version: str | None,
    cmake_version: str | None = None,
    package_version: str | None = None,
    unit_version: str | None = None,
):
    environment = os.environ.copy()
    environment.pop("RAPIDS_RELEASE_CANDIDATE_VERSION", None)
    environment.pop("RAPIDS_RELEASE_CANDIDATE_CMAKE_VERSION", None)
    environment.pop("RAPIDS_RELEASE_CANDIDATE_UNIT_VERSION", None)
    if version is not None:
        environment["RAPIDS_RELEASE_CANDIDATE_VERSION"] = version
    if cmake_version is not None:
        environment["RAPIDS_RELEASE_CANDIDATE_CMAKE_VERSION"] = cmake_version
    if package_version is not None:
        environment["SETUPTOOLS_SCM_PRETEND_VERSION"] = package_version
    if unit_version is not None:
        environment["RAPIDS_RELEASE_CANDIDATE_UNIT_VERSION"] = unit_version
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
        ("wheel:ucxx", {"conda:ucxx": "0.53"}, "0.53.0"),
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


@pytest.mark.parametrize(
    ("unit_id", "expected"),
    [("conda:ucxx", "0.53"), ("wheel:ucxx", "0.53"), ("maven:cuvs-java", "26.12.0")],
)
def test_candidate_unit_version_selector_preserves_exact_train_package_version(tmp_path, unit_id, expected):
    train_file = tmp_path / "release-train.json"
    train_file.write_text(
        json.dumps(
            {
                "version_contract": {
                    "package_versions": {"conda": "26.12.00", "wheel": "26.12.0", "maven": "26.12.0"},
                    "unit_package_versions": {"conda:ucxx": "0.53", "wheel:ucxx": "0.53"},
                },
                "release_units": [
                    {"id": "conda:ucxx", "version_profile": "conda"},
                    {"id": "wheel:ucxx", "version_profile": "wheel"},
                    {"id": "maven:cuvs-java", "version_profile": "maven"},
                ],
            }
        )
    )

    result = subprocess.run(
        ["bash", str(UNIT_VERSION_SELECTOR), str(train_file), unit_id],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_candidate_unit_version_selector_allows_units_without_package_versions(tmp_path):
    train_file = tmp_path / "release-train.json"
    train_file.write_text(
        json.dumps(
            {
                "version_contract": {"package_versions": {"conda": "26.12.00"}},
                "release_units": [
                    {"id": "docs:deployment", "version_profile": "docs"},
                    {"id": "source:ucxx", "version_profile": "source"},
                    {"id": "container:rapids-docker", "version_profile": "container"},
                ],
            }
        )
    )

    for unit_id in ("docs:deployment", "source:ucxx", "container:rapids-docker"):
        result = subprocess.run(
            ["bash", str(UNIT_VERSION_SELECTOR), str(train_file), unit_id],
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""


@pytest.mark.parametrize(
    ("unit_id", "version_profile"),
    [("conda:ucxx", "conda"), ("wheel:ucxx", "wheel"), ("maven:cuvs-java", "maven")],
)
def test_candidate_unit_version_selector_rejects_missing_package_version(tmp_path, unit_id, version_profile):
    train_file = tmp_path / "release-train.json"
    train_file.write_text(
        json.dumps(
            {
                "version_contract": {"package_versions": {}},
                "release_units": [{"id": unit_id, "version_profile": version_profile}],
            }
        )
    )

    result = subprocess.run(
        ["bash", str(UNIT_VERSION_SELECTOR), str(train_file), unit_id],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "missing the frozen package version" in result.stderr


def test_candidate_unit_version_selector_rejects_a_missing_unit(tmp_path):
    train_file = tmp_path / "release-train.json"
    train_file.write_text(json.dumps({"version_contract": {"package_versions": {}}, "release_units": []}))

    result = subprocess.run(
        ["bash", str(UNIT_VERSION_SELECTOR), str(train_file), "wheel:ucxx"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "exactly one candidate unit" in result.stderr


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


def test_two_component_unit_version_remains_exact_for_package_generator(tmp_path):
    tools_directory = tmp_path / "candidate-tools"

    result = _run_override(tools_directory, version="26.12", unit_version="0.53")

    assert result.returncode == 0, result.stderr
    environment = os.environ.copy()
    environment["PATH"] = f"{tools_directory}:{environment['PATH']}"
    generated = subprocess.run(["rapids-generate-version"], check=True, capture_output=True, text=True, env=environment)
    assert generated.stdout == "0.53\n"


def test_two_component_unit_version_uses_normalized_cmake_override(tmp_path):
    tools_directory = tmp_path / "candidate-tools"

    result = _run_override(
        tools_directory,
        version="26.12",
        unit_version="0.53",
        cmake_version="0.53.0",
    )

    assert result.returncode == 0, result.stderr
    environment = os.environ.copy()
    environment["PATH"] = f"{tools_directory}:{environment['PATH']}"
    generated = subprocess.run(["rapids-generate-version"], check=True, capture_output=True, text=True, env=environment)
    assert generated.stdout == "0.53.0\n"


@pytest.mark.parametrize("unit_version", ["0.53rc1", "0.53.0.1", "0.x"])
def test_invalid_frozen_unit_version_fails_closed(tmp_path, unit_version):
    tools_directory = tmp_path / "candidate-tools"

    result = _run_override(tools_directory, version="26.12", unit_version=unit_version)

    assert result.returncode != 0
    assert "candidate version must be final numeric" in result.stderr
    assert not (tools_directory / "rapids-generate-version").exists()


@pytest.mark.parametrize("version", ["26.10.00a28", "26.x", "26.10.00\n"])
def test_invalid_candidate_version_fails_before_installing_override(tmp_path, version):
    tools_directory = tmp_path / "candidate-tools"

    result = _run_override(tools_directory, version=version)

    assert result.returncode != 0
    assert "must be final numeric" in result.stderr
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


def test_candidate_setup_exports_global_release_and_unit_versions_separately():
    source = SETUP_SCRIPT.read_text()

    assert "select-unit-version.sh" in source
    assert "printf 'RAPIDS_RELEASE_CANDIDATE_VERSION=%s\\n'" in source
    assert "printf 'RAPIDS_RELEASE_CANDIDATE_UNIT_VERSION=%s\\n'" in source


def test_ucxx_wheel_package_versions_are_materialized_for_exact_catalog_identity():
    result = subprocess.run(
        ["bash", str(UCXX_VERSION_PREPARATION_TEST)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
