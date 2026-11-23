# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Select exactly one checksum-addressed release-train build lock."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import PurePosixPath
from typing import Any

SHA256 = re.compile(r"[0-9a-f]{64}")


def select_wheel_lock(document: dict[str, Any], platform: str, python_version: str) -> dict[str, Any]:
    """Return the unique wheel lock matching one CI matrix scope."""
    records = document.get("wheel", document.get("input_locks", {}).get("wheel", []))
    if not isinstance(records, list):
        raise ValueError("release train wheel locks must be a list")
    matches = [
        record
        for record in records
        if isinstance(record, dict) and record.get("scope") == {"platform": platform, "python": python_version}
    ]
    if len(matches) != 1:
        raise ValueError(
            f"release train must contain exactly one wheel lock for {platform}, Python {python_version}; "
            f"found {len(matches)}"
        )
    record = matches[0]
    path = record.get("path")
    sha256 = record.get("sha256")
    if not isinstance(path, str) or not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
        raise ValueError("release train contains an unsafe wheel lock path")
    if not isinstance(sha256, str) or SHA256.fullmatch(sha256) is None:
        raise ValueError("release train contains an invalid wheel lock checksum")
    return {"path": path, "sha256": sha256}


def main() -> None:
    """Select a wheel lock from a train or compact lock receipt."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--locks", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--python-version", required=True)
    arguments = parser.parse_args()
    with open(arguments.locks, encoding="utf-8") as stream:
        document = json.load(stream)
    print(json.dumps(select_wheel_lock(document, arguments.platform, arguments.python_version), sort_keys=True))


if __name__ == "__main__":
    main()
