# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Select the unique Conda build lock for one exact repository matrix scope."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import PurePosixPath
from typing import Any

SHA256 = re.compile(r"[0-9a-f]{64}")


def select_conda_lock(
    document: dict[str, Any], repository: str, platform: str, cuda_version: str, environment: str
) -> dict[str, str]:
    """Return the unique Conda lock matching repository, target and environment."""
    receipts = document.get("input_locks", {})
    records = document.get("conda")
    if records is None:
        records = receipts.get("conda", []) if isinstance(receipts, dict) else []
    if not isinstance(records, list):
        raise ValueError("release train Conda locks must be a list")

    matches = [
        record
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("scope"), dict)
        and record["scope"].get("repository") == repository
        and record["scope"].get("platform") == platform
        and record["scope"].get("cuda") == cuda_version
        and record["scope"].get("environment") == environment
    ]
    if len(matches) != 1:
        raise ValueError(
            "release train must contain exactly one Conda lock for "
            f"{repository} ({platform}, CUDA {cuda_version}, {environment}); found {len(matches)}"
        )

    record = matches[0]
    path = record.get("path")
    sha256 = record.get("sha256")
    if (
        not isinstance(path, str)
        or not path
        or PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
    ):
        raise ValueError("release train contains an unsafe Conda lock path")
    if not isinstance(sha256, str) or SHA256.fullmatch(sha256) is None:
        raise ValueError("release train contains an invalid Conda lock checksum")
    return {"path": path, "sha256": sha256}


def main() -> None:
    """Select a Conda lock from a train or compact lock receipt."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--locks", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--cuda-version", required=True)
    parser.add_argument("--environment", required=True, choices=("build", "host"))
    arguments = parser.parse_args()
    with open(arguments.locks, encoding="utf-8") as stream:
        document = json.load(stream)
    print(
        json.dumps(
            select_conda_lock(
                document,
                arguments.repository,
                arguments.platform,
                arguments.cuda_version,
                arguments.environment,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
