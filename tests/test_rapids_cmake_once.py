# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""rapids-cmake is fetched once per train and handed to every build row, never downloaded by CMake.

GitHub answered 504 for a row's rapids-cmake archive three times in two 26.12 release runs (45922055714,
45926793154). Create/Update Train now stages the pinned commit beside the train (stage-rapids-cmake.sh), and
dependency setup extracts it for every CMake configure (prepare-rapids-cmake.sh), retrying any download it still makes.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess  # nosec B404
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_row_reuse import FAKE_AWS

DIRECTORY = Path(__file__).parents[1] / "release-candidate-dependencies"
STAGE = DIRECTORY / "stage-rapids-cmake.sh"
PREPARE = DIRECTORY / "prepare-rapids-cmake.sh"
BASH = shutil.which("bash") or "bash"
GIT = shutil.which("git") or "git"
TRAIN = "e" * 64
BUCKET = "candidate-store"


def git(*arguments, cwd):
    return subprocess.run(  # noqa: S603 # nosec B603
        [GIT, *arguments], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def rapids_cmake(tmp_path):
    """A local rapids-cmake repository with one commit; returns (url, sha)."""
    repository = tmp_path / "rapids-cmake"
    (repository / "rapids-cmake").mkdir(parents=True)
    (repository / "CMakeLists.txt").write_text("project(rapids-cmake)\n")
    (repository / "rapids-cmake" / "rapids-cmake.cmake").write_text("# rapids-cmake\n")
    git("init", "--quiet", cwd=repository)
    git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=repository)
    git("add", ".", cwd=repository)
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "c",
        cwd=repository,
    )
    return f"file://{repository}", git("rev-parse", "HEAD", cwd=repository)


@pytest.fixture
def store(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "aws"
    fake.parent.mkdir()
    fake.write_text(FAKE_AWS)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_S3", str(tmp_path / "s3"))
    monkeypatch.setenv("AWS_CALLS", str(tmp_path / "aws-calls"))
    root = tmp_path / "s3" / BUCKET
    root.mkdir(parents=True)
    return root


def key(sha):
    return f"train-state/{TRAIN}/build-tools/rapids-cmake/{sha}.tar.gz"


def stage(tmp_path, url, sha, **environment):
    train = tmp_path / "release-train.json"
    train.write_text(json.dumps({"train_sha256": TRAIN, "build_tool_revisions": {"rapids-cmake": sha}}))
    return subprocess.run(  # noqa: S603 # nosec B603
        [BASH, str(STAGE), str(train), BUCKET, "train-state"],
        env={
            **os.environ,
            "RAPIDS_CMAKE_REPOSITORY_URL": url,
            "RAPIDS_CMAKE_FETCH_DELAY_SECONDS": "0",
            **environment,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def prepare(tmp_path, sha, require, **environment):
    output = tmp_path / "rapids-cmake-source"
    result = subprocess.run(  # noqa: S603 # nosec B603
        [BASH, str(PREPARE), sha, str(output), require],
        env={
            **os.environ,
            "RELEASE_CANDIDATE_BUCKET": BUCKET,
            "RELEASE_CANDIDATE_TRAIN_PREFIX": "train-state",
            "RELEASE_CANDIDATE_TRAIN_SHA256": TRAIN,
            **environment,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, output


def test_the_train_stages_the_pinned_commit_once_as_a_reproducible_tarball(tmp_path, store, rapids_cmake):
    url, sha = rapids_cmake
    result = stage(tmp_path, url, sha)
    assert result.returncode == 0, result.stderr  # noqa: S101
    staged = store / key(sha)
    with tarfile.open(staged) as archive:
        names = archive.getnames()
    assert f"rapids-cmake-{sha}/CMakeLists.txt" in names  # noqa: S101
    assert all(name.startswith(f"rapids-cmake-{sha}") for name in names)  # noqa: S101
    first = hashlib.sha256(staged.read_bytes()).hexdigest()
    # Staged again from nothing, the same commit gives the same bytes.
    staged.unlink()
    assert stage(tmp_path, url, sha).returncode == 0  # noqa: S101
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == first  # noqa: S101


def test_an_already_staged_commit_is_not_fetched_again(tmp_path, store, rapids_cmake):
    _, sha = rapids_cmake
    staged = store / key(sha)
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"staged earlier")
    result = stage(tmp_path, "file:///nowhere", sha)
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert "already staged" in result.stdout  # noqa: S101
    assert staged.read_bytes() == b"staged earlier"  # noqa: S101


def test_a_commit_that_cannot_be_fetched_fails_after_every_attempt_and_stages_nothing(tmp_path, store, rapids_cmake):
    _, sha = rapids_cmake
    result = stage(tmp_path, "file:///nowhere", sha, RAPIDS_CMAKE_FETCH_ATTEMPTS="3")
    assert result.returncode != 0  # noqa: S101
    assert result.stderr.count("fetching rapids-cmake") == 3  # noqa: S101
    assert "in 3 attempts" in result.stderr  # noqa: S101
    assert not (store / key(sha)).exists()  # noqa: S101


def test_a_train_without_a_full_rapids_cmake_commit_is_refused(tmp_path, store, rapids_cmake):
    url, _ = rapids_cmake
    result = stage(tmp_path, url, "main")
    assert result.returncode != 0  # noqa: S101
    assert "no full rapids-cmake commit" in result.stderr  # noqa: S101


def test_a_release_row_extracts_the_staged_tree(tmp_path, store, rapids_cmake):
    url, sha = rapids_cmake
    assert stage(tmp_path, url, sha).returncode == 0  # noqa: S101
    result, output = prepare(tmp_path, sha, "true")
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert (output / "CMakeLists.txt").read_text() == "project(rapids-cmake)\n"  # noqa: S101
    assert (output / "rapids-cmake" / "rapids-cmake.cmake").is_file()  # noqa: S101
    assert f"from s3://{BUCKET}/{key(sha)}" in result.stdout  # noqa: S101


def test_a_release_row_without_a_staged_tree_fails_and_downloads_nothing(tmp_path, store, rapids_cmake):
    _, sha = rapids_cmake
    curl = tmp_path / "bin" / "curl"
    curl.write_text(f"#!/bin/bash\ntouch {tmp_path}/curl-called\nexit 1\n")
    curl.chmod(0o755)
    result, output = prepare(tmp_path, sha, "true")
    assert result.returncode != 0  # noqa: S101
    assert "Run Update Train" in result.stderr  # noqa: S101
    assert not output.exists()  # noqa: S101
    assert not (tmp_path / "curl-called").exists()  # noqa: S101


def _github_archive(sha, top=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in (("CMakeLists.txt", b"project(rapids-cmake)\n"), ("rapids-cmake/x.cmake", b"#\n")):
            info = tarfile.TarInfo(f"{top or f'rapids-cmake-{sha}'}/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _fake_curl(tmp_path, archive: bytes):
    source = tmp_path / "github.tar.gz"
    source.write_bytes(archive)
    curl = tmp_path / "bin" / "curl"
    curl.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$@" > {tmp_path}/curl-arguments\n'
        'while [[ $# -gt 0 ]]; do if [[ "$1" == --output ]]; then output="$2"; fi; shift; done\n'
        f'cp {source} "$output"\n'
    )
    curl.chmod(0o755)


def test_another_row_downloads_the_archive_with_retries_when_nothing_is_staged(tmp_path, store, rapids_cmake):
    _, sha = rapids_cmake
    _fake_curl(tmp_path, _github_archive(sha))
    result, output = prepare(tmp_path, sha, "false")
    assert result.returncode == 0, result.stderr  # noqa: S101
    arguments = (tmp_path / "curl-arguments").read_text().splitlines()
    assert arguments[arguments.index("--retry") + 1] == "5"  # noqa: S101
    assert "--retry-all-errors" in arguments  # noqa: S101
    assert arguments[-1] == f"https://github.com/rapidsai/rapids-cmake/archive/{sha}.tar.gz"  # noqa: S101
    assert (output / "CMakeLists.txt").is_file()  # noqa: S101


def test_an_archive_of_another_commit_is_refused_before_anything_is_extracted(tmp_path, store, rapids_cmake):
    _, sha = rapids_cmake
    _fake_curl(tmp_path, _github_archive(sha, top="rapids-cmake-" + "0" * 40))
    result, output = prepare(tmp_path, sha, "false")
    assert result.returncode != 0  # noqa: S101
    assert f"is not rapids-cmake-{sha}/" in result.stderr  # noqa: S101
    assert not output.exists()  # noqa: S101


def test_an_archive_entry_that_climbs_out_of_the_tree_is_refused(tmp_path, store, rapids_cmake):
    _, sha = rapids_cmake
    _fake_curl(tmp_path, _github_archive(sha, top=f"rapids-cmake-{sha}/.."))
    result, output = prepare(tmp_path, sha, "false")
    assert result.returncode != 0  # noqa: S101
    assert "/../" in result.stderr  # noqa: S101
    assert not output.exists()  # noqa: S101
    assert not (tmp_path / "CMakeLists.txt").exists()  # noqa: S101
