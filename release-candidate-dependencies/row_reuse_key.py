#!/usr/bin/env python3
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Compute a build row's reuse key: every input that decides its output, and nothing tied to one run.

The key covers the producer commit, the row (unit, family, architecture, CUDA and Python), the train's tooling
commits, the frozen build inputs dependency setup assembled, the exact upstream artifacts the row consumed and the
digest-pinned image the row runs in. It leaves out run and attempt IDs and the build timestamp, which only stamps
versions. A row whose image is not a digest-pinned candidate image gets no key and is never indexed or reused.

Writes the canonical record to --output and prints its SHA-256, the key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

IMAGE = re.compile(r"ghcr\.io/rapidsai/release-candidates/ci-(conda|wheel)@sha256:[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
TOOLS = ("gha-tools", "shared-actions", "shared-workflows")


def _load(path: str, description: str) -> dict:
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"{description} is unreadable: {error}") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise SystemExit(f"{description} must be a schema_version 1 object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("producer-repository", "producer-sha", "unit", "family", "arch", "cuda-version", "python-version"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--implementation-revisions", required=True, help="The train's tooling commits, as JSON.")
    parser.add_argument("--runtime-image", default="", help="The image this row runs in.")
    parser.add_argument("--build-inputs", required=True)
    parser.add_argument("--upstream-inputs", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    if SHA.fullmatch(args.producer_sha) is None:
        raise SystemExit("producer SHA must be a full commit")
    try:
        revisions = json.loads(args.implementation_revisions)
    except json.JSONDecodeError as error:
        raise SystemExit("implementation revisions must be JSON") from error
    if (
        not isinstance(revisions, dict)
        or set(revisions) != set(TOOLS)
        or not all(isinstance(value, str) and SHA.fullmatch(value) for value in revisions.values())
    ):
        raise SystemExit("implementation revisions must name exactly the three tooling commits")
    build_inputs = _load(args.build_inputs, "candidate build inputs")
    upstream = _load(args.upstream_inputs, "upstream inputs")
    dependencies = upstream.get("dependencies")
    if not isinstance(dependencies, list):
        raise SystemExit("upstream inputs must list their dependencies")

    if IMAGE.fullmatch(args.runtime_image) is None:
        print(
            f"no reuse key: {args.runtime_image or 'no image'} is not a digest-pinned candidate image", file=sys.stderr
        )
        return 0
    record = {
        "schema_version": 1,
        "producer": {"repository": args.producer_repository, "sha": args.producer_sha},
        "row": {
            "unit": args.unit,
            "family": args.family,
            "arch": args.arch,
            "cuda_version": args.cuda_version,
            "python_version": args.python_version,
        },
        "implementation_revisions": revisions,
        "build_inputs": build_inputs,
        "upstream": sorted(dependencies, key=lambda item: json.dumps(item, sort_keys=True)),
        "runtime_image": args.runtime_image,
    }
    data = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    Path(args.output).write_bytes(data)
    print(hashlib.sha256(data).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
