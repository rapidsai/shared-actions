# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
"""Dependency scripts must act for the producer a train-generated run builds, not the run repository."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DOWNLOADER = ROOT / "release-candidate-dependencies" / "download-from-candidate.sh"
SETUP = ROOT / "release-candidate-dependencies" / "setup.sh"
TRAIN = "f" * 64


def run_downloader(tmp_path, producer, **extra):
    log = tmp_path / "aws.log"
    aws = tmp_path / "aws"
    aws.write_text('#!/bin/bash\nprintf \'%s\\n\' "$*" >> "${AWS_LOG}"\nexit 1\n')
    aws.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "AWS_LOG": str(log),
        "GITHUB_REPOSITORY": "rapidsai/release-tracking",
        "GITHUB_RUN_ID": "99",
        "GITHUB_RUN_ATTEMPT": "1",
        "RELEASE_CANDIDATE_BUCKET": "bucket",
        "GITHUB_SHA": "c" * 40,
        "RELEASE_CANDIDATE_PRODUCER_REPOSITORY": producer,
        # A named producer must name its commit too; the run repository uses GITHUB_SHA.
        "RELEASE_CANDIDATE_PRODUCER_SHA": "d" * 40 if producer else "",
        "RELEASE_CANDIDATE_TRAIN_PREFIX": "train-state",
        "RELEASE_CANDIDATE_TRAIN_SHA256": TRAIN,
        "RAPIDS_UNZIP_DIR": str(tmp_path / "out"),
        **extra,
    }
    result = subprocess.run(  # noqa: S603 - runs the checked-in helper with a fake aws.
        [str(DOWNLOADER), "conda"], env=env, capture_output=True, text=True, check=False
    )
    return result, (log.read_text() if log.exists() else "")


def test_downloader_resolves_the_producer_run_prefix_not_the_train_repository(tmp_path):
    _, calls = run_downloader(tmp_path, "rapidsai/kvikio")
    assert f"s3://bucket/train-state/{TRAIN}/kvikio/99.1/" in calls
    assert "release-tracking" not in calls


def test_downloader_without_a_producer_keeps_using_the_run_repository(tmp_path):
    _, calls = run_downloader(tmp_path, "")
    assert f"s3://bucket/train-state/{TRAIN}/release-tracking/99.1/" in calls


@pytest.mark.parametrize("producer", ["kvikio", "rapidsai/kvikio/extra", "rapidsai/../x", "rapidsai/kv ikio"])
def test_downloader_rejects_unsafe_producer_before_any_request(tmp_path, producer):
    result, calls = run_downloader(tmp_path, producer)
    assert result.returncode != 0
    assert "owner/repository format" in result.stderr
    assert calls == ""


def test_setup_captures_the_producer_for_the_generated_downloader_and_never_the_run_repository():
    text = SETUP.read_text()
    assert "printf 'producer_repository=%q" in text
    assert 'repository="${producer_repository#*/}"' in text
    assert 'repository="${producer_repository##*/}"' in text
    assert "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}:${producer_repository##*/}" in text
    # Only the required-variable list, the producer default and two usage labels may name the run repository.
    uses = [line.strip().split("=")[0].split(" ")[0] for line in text.splitlines() if "GITHUB_REPOSITORY" in line]
    assert len(uses) == 4
    assert [line for line in text.splitlines() if line.strip() == '"${GITHUB_REPOSITORY}"'] != []


@pytest.mark.parametrize("action", ["release-candidate-dependencies", "release-catalog", "release-catalog-dispatch"])
def test_actions_pass_the_producer_to_their_scripts(action):
    text = (ROOT / action / "action.yml").read_text()
    assert "producer-repository:" in text
    assert "RELEASE_CANDIDATE_PRODUCER_REPOSITORY: ${{ inputs.producer-repository }}" in text
