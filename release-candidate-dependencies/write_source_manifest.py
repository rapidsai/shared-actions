# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
"""Validate and expose the immutable source identities in a release train."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


def source_manifest(train: Any) -> dict[str, Any]:
    """Return validated repository-to-commit mappings from a frozen train."""
    if not isinstance(train, dict):
        raise ValueError("release train must be a JSON object")
    revisions = train.get("source_revisions")
    repositories = train.get("github_repositories")
    if not isinstance(revisions, dict) or not revisions:
        raise ValueError("release train source_revisions must be a non-empty object")
    if not isinstance(repositories, dict) or set(repositories) != set(revisions):
        raise ValueError("release train github_repositories must match source_revisions")
    for name, revision in revisions.items():
        if not isinstance(name, str) or not name or not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            raise ValueError("release train contains an invalid source revision")
        repository = repositories[name]
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
            raise ValueError("release train contains an invalid GitHub repository identity")
    return {
        "schema_version": 1,
        "source_revisions": dict(sorted(revisions.items())),
        "github_repositories": dict(sorted(repositories.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        document = source_manifest(json.loads(args.train.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))
    args.output.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
