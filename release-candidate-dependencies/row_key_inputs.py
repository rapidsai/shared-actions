#!/usr/bin/env python3
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""The build inputs and upstream inputs a build row's reuse key hashes.

Dependency setup writes both for the rows that run, and the reuse plan computes them for every row before any build
job starts (plans/per-row-reuse.md, 4b). Both call this module, so a row gets the same key either way.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_TOOLS = ("gha-tools", "shared-actions", "shared-workflows")
_DEFAULT_REPOSITORIES = {tool: f"rapidsai/{tool}" for tool in _TOOLS}


def build_inputs(train: dict[str, Any], source_inputs: dict[str, Any]) -> dict[str, Any]:
    """The train-wide inputs of one unit's build: receipts, build policy, tooling repositories and its sources."""
    receipts = train.get("input_receipts")
    if not isinstance(receipts, dict):
        raise ValueError("release train must contain checksum-addressed input receipts; regenerate the train")
    digests = {}
    for name in ("locks", "variants"):
        receipt = receipts.get(name)
        digest = receipt.get("sha256") if isinstance(receipt, dict) else None
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ValueError(f"release train contains an unsafe {name} receipt reference")
        digests[name] = digest
    policy = train.get("candidate_build_policy")
    sccache = policy.get("sccache") if isinstance(policy, dict) else None
    if not isinstance(sccache, bool):
        raise ValueError("release train must declare candidate_build_policy.sccache as true or false")
    unknown = sorted(set(policy) - {"sccache", "tooling_in_reuse_keys"})
    if unknown:
        raise ValueError(f"release train has unknown candidate_build_policy settings: {', '.join(unknown)}")
    tooling_in_keys = policy.get("tooling_in_reuse_keys", True)
    if not isinstance(tooling_in_keys, bool):
        raise ValueError("candidate_build_policy.tooling_in_reuse_keys must be true or false")
    build_policy: dict[str, bool] = {"sccache": sccache}
    if not tooling_in_keys:
        # A development setting: row_reuse_key.py then keys rows without the tooling commits. Recording it here keeps
        # those keys apart from every normal train's; the normal setting is left out so existing keys don't change.
        build_policy["tooling_in_reuse_keys"] = False
    repositories = train.get("build_implementation_repositories", _DEFAULT_REPOSITORIES)
    if (
        not isinstance(repositories, dict)
        or sorted(repositories) != sorted(_TOOLS)
        or not all(isinstance(value, str) and _REPOSITORY.fullmatch(value) for value in repositories.values())
        or not all(repositories[tool].endswith(f"/{tool}") for tool in _TOOLS)
    ):
        raise ValueError("release train has invalid build_implementation_repositories")
    return {
        "schema_version": 1,
        "train_inputs": {"lock_receipt_sha256": digests["locks"], "variant_receipt_sha256": digests["variants"]},
        "build_policy": build_policy,
        "build_implementation_repositories": repositories,
        "source_inputs": source_inputs,
    }


def upstream_document(selected: list[dict[str, Any]]) -> dict[str, Any]:
    """The upstream packages a row installs, from upstream_selection.py's result, in a canonical order."""
    resolved = {}
    for item in selected:
        source = item.get("source") or {}
        entry = {
            "artifact_key": item["artifact_key"],
            "path": item["path"],
            "release_catalog_key": item.get("release_catalog_key") or "",
            "producer": {
                "repository": source.get("repository") or "",
                "sha": source.get("sha") or "",
                "build_input_digest": item.get("producer_build_input_digest") or "",
            },
            "sha256": item["sha256"],
            "package": item["package"],
        }
        identity = (entry["release_catalog_key"], entry["artifact_key"], entry["path"], entry["sha256"])
        resolved.setdefault(identity, entry)
    return {"schema_version": 1, "dependencies": [resolved[identity] for identity in sorted(resolved)]}


def _write(value: Any) -> None:
    json.dump(value, sys.stdout, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    """Print the build inputs or the upstream inputs as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-inputs")
    build.add_argument("--train", required=True, type=Path)
    build.add_argument("--source-inputs", required=True, type=Path)
    upstream = commands.add_parser("upstream")
    upstream.add_argument("--selected", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "build-inputs":
            _write(build_inputs(json.loads(args.train.read_text()), json.loads(args.source_inputs.read_text())))
        else:
            _write(upstream_document(json.loads(args.selected.read_text())))
    except ValueError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    sys.exit(main())
