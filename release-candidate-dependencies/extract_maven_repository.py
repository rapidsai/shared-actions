# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
"""Safely unpack a verified candidate Maven repository archive."""

from __future__ import annotations

import argparse
import shutil
import tarfile
from pathlib import Path, PurePosixPath


def extract_repository(archive: Path, destination: Path) -> None:
    """Extract regular files and directories, rejecting links and traversal."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive, mode="r:gz") as bundle:
        members = bundle.getmembers()
        names: set[str] = set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
                raise ValueError("Maven repository archive contains an unsafe path")
            if member.name in names:
                raise ValueError("Maven repository archive contains duplicate paths")
            names.add(member.name)
            if not (member.isdir() or member.isfile()):
                raise ValueError("Maven repository archive contains a link or special file")

        for member in members:
            target = (root / Path(*PurePosixPath(member.name).parts)).resolve()
            if root not in target.parents and target != root:
                raise ValueError("Maven repository archive path escapes its destination")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError("Maven repository archive contains an unreadable file")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o644)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    extract_repository(args.archive, args.destination)


if __name__ == "__main__":
    main()
