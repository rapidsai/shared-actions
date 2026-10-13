# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# ruff: noqa: S101, S603, S607

"""Candidate version routing stays scoped to candidate setup."""

import os
import subprocess  # nosec B404 - executes a checked-in shell helper with test inputs.
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
VERSION_OVERRIDE = ROOT / "release-candidate-dependencies" / "install-version-override.sh"
SETUP_SCRIPT = ROOT / "release-candidate-dependencies" / "setup.sh"


def _run_override(tools_directory: Path, *, version: str | None):
    environment = os.environ.copy()
    environment.pop("RAPIDS_RELEASE_CANDIDATE_VERSION", None)
    if version is not None:
        environment["RAPIDS_RELEASE_CANDIDATE_VERSION"] = version
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
    path_add = source.index('printf \'%s\\n\' "${tools}" >>"${GITHUB_PATH}"')

    assert install < path_add
