#!/usr/bin/env python3
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Compute a build row's reuse key: every input that decides its output, and nothing tied to one run.

The key covers the producer commit, the row (unit, family, architecture, CUDA and Python), the build it runs (its
release entrypoint and that workflow's inputs), the train's tooling commits, the frozen build inputs dependency
setup assembled, the exact upstream artifacts the row consumed and the digest-pinned image the row runs in. A
repository's C++ and Python builds share every row field, so only the build tells them apart. It leaves out run and
attempt IDs, the build timestamp and date, which only stamp versions, the train digest, so a later train can reuse,
and the image map, since the row's own image is already in the key. Every other workflow input is in the key, so a
new input is covered unless it is excluded here on purpose. A row whose image is not a digest-pinned candidate
image, or that names no build, gets no key and is never indexed or reused.

A candidate CI image row (family ``container``, plans/per-row-reuse.md, 4a) runs in no candidate image and consumes
no upstream packages. Its key holds the ci-imgs commit, its matrix row (``image_row``; ``row.image`` names it, so the
upload's per-row history tells ci-conda and ci-wheel apart), the build, the tooling and row_key_inputs.py's image
inputs: its base images by digest, the refresh generation, the build policy and the tooling repositories. An image
whose bases are not all pinned by digest gets no key.

Writes the canonical record to --output and prints its SHA-256, the key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PINNED_BASE = re.compile(r"docker\.io/[a-z0-9._/-]+:[A-Za-z0-9._-]{1,128}@sha256:[0-9a-f]{64}\Z")
IMAGE_ROW_FIELDS = ("ARCH", "CUDA_VER", "PYTHON_VER", "IMAGE_REPO", "IMAGE_NAME")
IMAGE = re.compile(r"ghcr\.io/rapidsai/release-candidates/ci-(conda|wheel)@sha256:[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
TOOLS = ("gha-tools", "shared-actions", "shared-workflows")
ENTRYPOINT = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
DEVELOPMENT_TOOLING = "excluded for development"


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
    parser.add_argument("--row-entrypoint", default="", help="The release entrypoint workflow the row runs.")
    parser.add_argument(
        "--row-identity",
        default="",
        help="The release file's static with: values for the row's job, as JSON (candidate-row-identity).",
    )
    parser.add_argument(
        "--image-row", default="", help="A container row's image matrix row, as JSON; --build-inputs are its inputs."
    )
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
    # A per-release development setting (candidate_build_policy.tooling_in_reuse_keys: false) keys rows without the
    # tooling commits, so rebuilding the release tooling doesn't rebuild every row. build_inputs records the setting,
    # so such keys never equal a normal train's.
    tooling_in_keys = (build_inputs.get("build_policy") or {}).get("tooling_in_reuse_keys", True) is not False
    upstream = _load(args.upstream_inputs, "upstream inputs")
    dependencies = upstream.get("dependencies")
    if not isinstance(dependencies, list):
        raise SystemExit("upstream inputs must list their dependencies")

    if bool(args.row_entrypoint) != bool(args.row_identity):
        raise SystemExit("the row's entrypoint and identity must be given together")
    build = None
    if args.row_entrypoint:
        if ENTRYPOINT.fullmatch(args.row_entrypoint) is None:
            raise SystemExit("the row entrypoint must be a workflow name")
        try:
            identity = json.loads(args.row_identity)
        except json.JSONDecodeError as error:
            raise SystemExit("the row identity must be JSON") from error
        # The generator writes it from the job's static with: values, so it holds only scalars and nothing bound to
        # a run or a train; it is keyed exactly as given.
        if not isinstance(identity, dict) or not identity:
            raise SystemExit("the row identity must be a non-empty object")
        if not all(isinstance(value, (str, bool, int, float)) for value in identity.values()):
            raise SystemExit("the row identity's values must be scalars, as with: values are")
        build = {"entrypoint": args.row_entrypoint, "identity": identity}

    if args.image_row:
        return _image_key(args, revisions, build_inputs, tooling_in_keys, dependencies, build)
    if IMAGE.fullmatch(args.runtime_image) is None:
        print(
            f"no reuse key: {args.runtime_image or 'no image'} is not a digest-pinned candidate image", file=sys.stderr
        )
        return 0
    if build is None:
        print("no reuse key: the row names no build (entrypoint and inputs)", file=sys.stderr)
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
        "build": build,
        "implementation_revisions": revisions if tooling_in_keys else DEVELOPMENT_TOOLING,
        "build_inputs": build_inputs,
        "upstream": sorted(dependencies, key=lambda item: json.dumps(item, sort_keys=True)),
        "runtime_image": args.runtime_image,
    }
    return _write(record, args.output)


def _write(record: dict, output: str) -> int:
    data = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    Path(output).write_bytes(data)
    print(hashlib.sha256(data).hexdigest())
    return 0


def _image_key(
    args: argparse.Namespace,
    revisions: dict,
    inputs: dict,
    tooling_in_keys: bool,
    dependencies: list,
    build: dict | None,
) -> int:
    """A candidate CI image row's key: its row, ci-imgs commit, build, tooling and image inputs."""
    if args.family != "container":
        raise SystemExit("only a container row has an image row")
    if args.runtime_image:
        raise SystemExit("a candidate image row runs in no candidate image")
    if dependencies:
        raise SystemExit("a candidate image row consumes no upstream packages")
    try:
        row = json.loads(args.image_row)
    except json.JSONDecodeError as error:
        raise SystemExit("the image row must be JSON") from error
    if (
        not isinstance(row, dict)
        or not all(isinstance(row.get(field), str) and row[field] for field in IMAGE_ROW_FIELDS)
        or not all(isinstance(value, (str, bool, int, float)) for value in row.values())
    ):
        raise SystemExit(f"the image row needs {', '.join(IMAGE_ROW_FIELDS)} and only scalar values")
    if (row["ARCH"], row["CUDA_VER"], row["PYTHON_VER"]) != (args.arch, args.cuda_version, args.python_version):
        raise SystemExit("the image row's ARCH, CUDA_VER and PYTHON_VER must be the row's")
    variant = row["IMAGE_NAME"].removeprefix(f"rapidsai/{row['IMAGE_REPO']}:")
    if row["IMAGE_REPO"] not in ("ci-conda", "ci-wheel") or variant == row["IMAGE_NAME"]:
        raise SystemExit("the image row must name rapidsai/<ci-conda|ci-wheel>:<variant>")
    bases = inputs.get("base_images")
    if (
        not isinstance(bases, dict)
        or not bases
        or not all(isinstance(value, str) and PINNED_BASE.fullmatch(value) for value in bases.values())
    ):
        print("no reuse key: the image's base images are not all pinned by digest", file=sys.stderr)
        return 0
    if build is None:
        print("no reuse key: the row names no build (entrypoint and inputs)", file=sys.stderr)
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
            "image": f"{row['IMAGE_REPO']}:{variant}-{row['ARCH']}",
        },
        "image_row": row,
        "build": build,
        "implementation_revisions": revisions if tooling_in_keys else DEVELOPMENT_TOOLING,
        "build_inputs": inputs,
        "upstream": [],
    }
    return _write(record, args.output)


if __name__ == "__main__":
    sys.exit(main())
