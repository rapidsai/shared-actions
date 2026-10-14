# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
"""Regression coverage for private Maven candidate archive extraction."""

from __future__ import annotations

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

_EXTRACTOR = Path(__file__).parents[1] / "release-candidate-dependencies" / "extract_maven_repository.py"
_SPEC = importlib.util.spec_from_file_location("extract_maven_repository", _EXTRACTOR)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"unable to load Maven archive extractor: {_EXTRACTOR}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
extract_repository = _MODULE.extract_repository


def write_archive(path: Path, members: list[tuple[str, bytes | None, str]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content, kind in members:
            member = tarfile.TarInfo(name)
            if kind == "directory":
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            elif kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = "../../outside"
                archive.addfile(member)
            else:
                if content is None:
                    pytest.fail(f"file member {name} must have content")
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))


def test_extracts_maven_repository_files(tmp_path: Path) -> None:
    archive = tmp_path / "maven.tar.gz"
    destination = tmp_path / "maven-repository"
    write_archive(
        archive,
        [
            ("com/nvidia/cuvs/cuvs-java/26.12.0", None, "directory"),
            ("com/nvidia/cuvs/cuvs-java/26.12.0/cuvs-java-26.12.0.jar", b"jar bytes", "file"),
            ("com/nvidia/cuvs/cuvs-java/26.12.0/cuvs-java-26.12.0.pom", b"pom bytes", "file"),
        ],
    )

    extract_repository(archive, destination)

    jar_path = destination / "com/nvidia/cuvs/cuvs-java/26.12.0/cuvs-java-26.12.0.jar"
    pom_path = destination / "com/nvidia/cuvs/cuvs-java/26.12.0/cuvs-java-26.12.0.pom"
    if jar_path.read_bytes() != b"jar bytes" or pom_path.read_bytes() != b"pom bytes":
        pytest.fail("the Maven archive did not preserve its repository files")


@pytest.mark.parametrize(
    "member",
    [
        ("../outside.txt", b"escape", "file"),
        ("/outside.txt", b"absolute", "file"),
        ("repo/link", None, "symlink"),
    ],
)
def test_rejects_unsafe_archive_members(tmp_path: Path, member: tuple[str, bytes | None, str]) -> None:
    archive = tmp_path / "maven.tar.gz"
    destination = tmp_path / "maven-repository"
    write_archive(archive, [member])

    with pytest.raises(ValueError):
        extract_repository(archive, destination)

    if (tmp_path / "outside.txt").exists():
        pytest.fail("unsafe Maven archive member escaped the destination")


def test_rejects_duplicate_archive_paths(tmp_path: Path) -> None:
    archive = tmp_path / "maven.tar.gz"
    write_archive(archive, [("repo/file", b"one", "file"), ("repo/file", b"two", "file")])

    with pytest.raises(ValueError, match="duplicate paths"):
        extract_repository(archive, tmp_path / "maven-repository")
