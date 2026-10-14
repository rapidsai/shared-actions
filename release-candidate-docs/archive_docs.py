# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
"""Create a deterministic documentation archive and catalog identity file."""

from __future__ import annotations

import argparse
import gzip
import json
import re
import tarfile
from pathlib import Path


def archive_docs(source: Path, destination: Path, identity: Path, version: str) -> None:
    """Archive a rendered docs tree with stable metadata and no symlinks."""
    if not re.fullmatch(r"[0-9]{2}\.[0-9]{2}", version):
        raise ValueError("documentation version must use YY.MM format")
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("documentation source must be a directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    identity.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output, gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as compressed:
        with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
            for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
                if path.is_symlink():
                    raise ValueError("documentation tree contains a symbolic link")
                if not path.is_file() and not path.is_dir():
                    raise ValueError("documentation tree contains a special file")
                relative = path.relative_to(source).as_posix()
                member = tarfile.TarInfo(relative + ("/" if path.is_dir() else ""))
                member.mtime = 0
                member.uid = 0
                member.gid = 0
                member.uname = ""
                member.gname = ""
                member.mode = 0o755 if path.is_dir() else 0o644
                if path.is_dir():
                    member.type = tarfile.DIRTYPE
                    archive.addfile(member)
                else:
                    member.size = path.stat().st_size
                    with path.open("rb") as content:
                        archive.addfile(member, content)
    identity.write_text(
        json.dumps(
            {"ecosystem": "docs", "name": "deployment", "platform": "noarch", "version": version},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    archive_docs(args.source, args.archive, args.identity, args.version)


if __name__ == "__main__":
    main()
