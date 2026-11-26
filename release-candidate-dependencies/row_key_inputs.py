#!/usr/bin/env python3
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""The build inputs and upstream inputs a build row's reuse key hashes, and a candidate image row's inputs.

Dependency setup writes them for the rows that run, and the reuse plan computes them for every row before any build
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


def _build_policy(train: dict[str, Any]) -> dict[str, bool]:
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
    return build_policy


def _repositories(train: dict[str, Any]) -> dict[str, str]:
    repositories = train.get("build_implementation_repositories", _DEFAULT_REPOSITORIES)
    if (
        not isinstance(repositories, dict)
        or sorted(repositories) != sorted(_TOOLS)
        or not all(isinstance(value, str) and _REPOSITORY.fullmatch(value) for value in repositories.values())
        or not all(repositories[tool].endswith(f"/{tool}") for tool in _TOOLS)
    ):
        raise ValueError("release train has invalid build_implementation_repositories")
    return repositories


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
    return {
        "schema_version": 1,
        "train_inputs": {"lock_receipt_sha256": digests["locks"], "variant_receipt_sha256": digests["variants"]},
        "build_policy": _build_policy(train),
        "build_implementation_repositories": _repositories(train),
        "source_inputs": source_inputs,
    }


_IMAGE_REPOSITORIES = ("ci-conda", "ci-wheel")
_VARIANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_PINNED_BASE = re.compile(r"docker\.io/[a-z0-9._/-]+:[A-Za-z0-9._-]{1,128}@sha256:[0-9a-f]{64}\Z")
_REFRESH_KEY = re.compile(r"[A-Za-z0-9._-]{0,128}\Z")


def image_key(row: dict[str, Any]) -> str:
    """A candidate image row's key, ``<repository>:<variant>-<arch>``, as in the image map and the train's bases."""
    repository, name, arch = row.get("IMAGE_REPO"), row.get("IMAGE_NAME"), row.get("ARCH")
    if repository not in _IMAGE_REPOSITORIES or arch not in ("amd64", "arm64") or not isinstance(name, str):
        raise ValueError("an image row needs IMAGE_REPO ci-conda or ci-wheel, IMAGE_NAME and ARCH amd64 or arm64")
    variant = name.removeprefix(f"rapidsai/{repository}:")
    if variant == name or _VARIANT.fullmatch(variant) is None:
        raise ValueError(f"image name {name!r} is not rapidsai/{repository}:<variant>")
    return f"{repository}:{variant}-{arch}"


def image_inputs(train: dict[str, Any], row: dict[str, Any]) -> dict[str, Any] | None:
    """What a candidate image row is built from besides ci-imgs, its row and the tooling (plans/per-row-reuse.md, 4a).

    Its base images by digest (the train's ``candidate_image_bases``), the image refresh generation, the build policy
    and the tooling repositories. Not the package locks, variants or sources: the image consumes none of them. None for
    a train that records no base digests: its images build from tags and get no key.
    """
    bases = train.get("candidate_image_bases")
    if bases is None:
        return None
    key = image_key(row)
    entry = bases.get(key) if isinstance(bases, dict) else None
    if not isinstance(entry, dict) or not entry:
        raise ValueError(f"the release train records no base images for {key}")
    for argument, reference in entry.items():
        if not isinstance(argument, str) or not isinstance(reference, str) or not _PINNED_BASE.fullmatch(reference):
            raise ValueError(f"the release train's base image {argument} for {key} is not pinned by digest")
    policy = train.get("candidate_image_policy")
    refresh_key = policy.get("refresh_key", "") if isinstance(policy, dict) else ""
    if policy is not None and (not isinstance(refresh_key, str) or not _REFRESH_KEY.fullmatch(refresh_key)):
        raise ValueError("candidate_image_policy.refresh_key is invalid")
    return {
        "schema_version": 1,
        "base_images": dict(entry),
        "refresh_key": refresh_key,
        "build_policy": _build_policy(train),
        "build_implementation_repositories": _repositories(train),
    }


def image_refresh(train: dict[str, Any]) -> bool:
    """True when the train asks every candidate image to rebuild (``candidate_image_policy.mode: refresh``)."""
    policy = train.get("candidate_image_policy")
    return isinstance(policy, dict) and policy.get("mode") == "refresh"


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
    image = commands.add_parser("image-inputs", help="Print an image row's inputs, or nothing without base digests.")
    image.add_argument("--train", required=True, type=Path)
    image.add_argument("--image-row", required=True, help="The image matrix row, as JSON.")
    args = parser.parse_args(argv)
    try:
        if args.command == "build-inputs":
            _write(build_inputs(json.loads(args.train.read_text()), json.loads(args.source_inputs.read_text())))
        elif args.command == "image-inputs":
            row = json.loads(args.image_row)
            if not isinstance(row, dict):
                raise ValueError("the image row must be a JSON object")
            found = image_inputs(json.loads(args.train.read_text()), row)
            if found is not None:
                _write(found)
        else:
            _write(upstream_document(json.loads(args.selected.read_text())))
    except ValueError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    sys.exit(main())
