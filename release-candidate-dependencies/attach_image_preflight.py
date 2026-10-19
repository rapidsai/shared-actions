#!/usr/bin/env python3
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Bind an authenticated CI-image preflight extension into candidate build inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_MAX_INPUT_BYTES = 4 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_CONSUMER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_IMAGE_BUILD = re.compile(
    r"(?P<version>[0-9]+\.[0-9]+)-cuda(?P<cuda>[0-9]+\.[0-9]+(?:\.[0-9]+)?)-"
    r"(?P<linux>[a-z0-9][a-z0-9.]*)-py(?P<python>[0-9]+\.[0-9]+)-(?P<arch>amd64|arm64)\Z"
)
_ARCH = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}
_PREFLIGHT_FIELDS = {
    "schema_version",
    "producer",
    "train_sha256",
    "request_sha256",
    "consumption_receipt_sha256",
    "selected_consumer_unit_ids",
    "consumers",
    "preflight_sha256",
}
_CONSUMER_FIELDS = {"consumer_id", "release_unit_id", "source_sha", "images", "build_input_extension"}
_EXTENSION_FIELDS = {
    "schema_version",
    "train_sha256",
    "request_sha256",
    "consumption_receipt_sha256",
    "consumer_id",
    "release_unit_id",
    "source_sha",
    "images",
    "sha256",
}
_IMAGE_FIELDS = {"identity", "destination_reference", "runnable_manifest_sha256"}
_IDENTITY_FIELDS = {"ecosystem", "name", "version", "build", "platform"}
_REPOSITORY = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?))*(?::[0-9]{1,5})?/"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*\Z"
)
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")


class ImagePreflightInputError(ValueError):
    """Raised when the optional image preflight does not match this build."""


def attach_image_preflight(
    base_inputs: Mapping[str, Any],
    train: Mapping[str, Any],
    preflight: Mapping[str, Any],
    *,
    consumer_id: str,
    runtime_image_reference: str,
    unit_id: str,
    family: str,
    repository: str,
    source_sha: str,
    arch: str,
    cuda_version: str,
    python_version: str,
) -> dict[str, Any]:
    """Return the existing BuildInput object plus one exactly bound image input."""
    if not isinstance(base_inputs, Mapping) or "ci_image_preflight" in base_inputs:
        raise ImagePreflightInputError("base candidate build inputs are invalid or already contain image preflight")
    train_sha = _release_train_sha256(train)
    if not isinstance(preflight, Mapping):
        raise ImagePreflightInputError("CI-image preflight must be a JSON object")
    if preflight.get("schema_version") != 1 or preflight.get("producer") != "release-platform-ci-image-preflight":
        raise ImagePreflightInputError("unsupported CI-image preflight schema")
    if set(preflight) != _PREFLIGHT_FIELDS:
        raise ImagePreflightInputError("CI-image preflight has missing or unsupported fields")
    if preflight.get("train_sha256") != train_sha:
        raise ImagePreflightInputError("CI-image preflight belongs to a different release train")
    _require_digest(preflight.get("request_sha256"), "preflight request SHA-256")
    _require_digest(preflight.get("consumption_receipt_sha256"), "consumption receipt SHA-256")
    _verify_hash(preflight, "preflight_sha256", "CI-image preflight")

    expected_unit_id = f"{family}:{repository}"
    if unit_id != expected_unit_id or family not in {"conda", "wheel", "maven", "docs"}:
        raise ImagePreflightInputError("candidate unit does not match the current artifact family and repository")
    if (
        not isinstance(source_sha, str)
        or not _SHA1.fullmatch(source_sha)
        or not isinstance(consumer_id, str)
        or not _CONSUMER_ID.fullmatch(consumer_id)
    ):
        raise ImagePreflightInputError("consumer or source identity is invalid")
    release_units = train.get("release_units")
    source_revisions = train.get("source_revisions")
    if not isinstance(release_units, list) or not isinstance(source_revisions, Mapping):
        raise ImagePreflightInputError("release train has no source-bound release units")
    unit_rows = [row for row in release_units if isinstance(row, Mapping) and row.get("id") == unit_id]
    if len(unit_rows) != 1:
        raise ImagePreflightInputError("candidate unit is missing or ambiguous in the release train")
    unit = unit_rows[0]
    if (
        unit.get("artifact_family") != family
        or unit.get("repository") != repository
        or unit.get("source_revision") != source_sha
        or source_revisions.get(repository) != source_sha
    ):
        raise ImagePreflightInputError("candidate source does not match the frozen release unit")
    expectations = train.get("artifact_expectations")
    frozen_images = expectations.get("container:ci-images") if isinstance(expectations, Mapping) else None
    if not isinstance(frozen_images, list) or not frozen_images:
        raise ImagePreflightInputError("release train has no frozen CI-image expectations")

    selected_units = preflight.get("selected_consumer_unit_ids")
    if (
        not isinstance(selected_units, list)
        or any(not isinstance(item, str) for item in selected_units)
        or len(set(selected_units)) != len(selected_units)
        or unit_id not in selected_units
    ):
        raise ImagePreflightInputError("preflight does not select this consumer unit")
    consumers = preflight.get("consumers")
    if not isinstance(consumers, list):
        raise ImagePreflightInputError("preflight consumers must be a list")
    matches = [item for item in consumers if isinstance(item, Mapping) and item.get("consumer_id") == consumer_id]
    if len(matches) != 1:
        raise ImagePreflightInputError("preflight must contain exactly one matching consumer")
    consumer = matches[0]
    if set(consumer) != _CONSUMER_FIELDS:
        raise ImagePreflightInputError("preflight consumer has missing or unsupported fields")
    extension = consumer.get("build_input_extension")
    if not isinstance(extension, Mapping) or set(extension) != _EXTENSION_FIELDS:
        raise ImagePreflightInputError("consumer BuildInput extension has missing or unsupported fields")
    if (
        extension.get("schema_version") != 1
        or extension.get("train_sha256") != train_sha
        or extension.get("request_sha256") != preflight["request_sha256"]
        or extension.get("consumption_receipt_sha256") != preflight["consumption_receipt_sha256"]
        or extension.get("consumer_id") != consumer_id
        or extension.get("release_unit_id") != unit_id
        or extension.get("source_sha") != source_sha
        or consumer.get("release_unit_id") != unit_id
        or consumer.get("source_sha") != source_sha
    ):
        raise ImagePreflightInputError("consumer BuildInput extension does not match this train, source, or unit")
    _verify_hash(extension, "sha256", "consumer BuildInput extension")
    if consumer.get("images") != extension.get("images"):
        raise ImagePreflightInputError("consumer image identities differ from its BuildInput extension")
    if not isinstance(extension.get("images"), list) or not extension["images"]:
        raise ImagePreflightInputError("consumer must have at least one image identity")

    wanted_arch = _ARCH.get(arch)
    if wanted_arch is None:
        raise ImagePreflightInputError("candidate image architecture is unsupported")
    image_matches = []
    for image in extension["images"]:
        if not isinstance(image, Mapping) or set(image) != _IMAGE_FIELDS:
            raise ImagePreflightInputError("consumer image has missing or unsupported fields")
        identity = image.get("identity")
        if not isinstance(identity, Mapping) or set(identity) != _IDENTITY_FIELDS:
            raise ImagePreflightInputError("consumer image identity has missing or unsupported fields")
        if any(not isinstance(identity.get(field), str) or not identity[field] for field in _IDENTITY_FIELDS):
            raise ImagePreflightInputError("consumer image identity contains invalid fields")
        if not any(identity == frozen_identity for frozen_identity in frozen_images):
            raise ImagePreflightInputError("consumer image identity is not in the frozen CI-image inventory")
        parsed_build = _IMAGE_BUILD.fullmatch(identity["build"])
        if (
            identity.get("ecosystem") != "container"
            or identity.get("version") != train.get("release_version")
            or parsed_build is None
            or identity.get("platform") != f"linux/{wanted_arch}"
            or parsed_build.group("version") != train.get("release_version")
            or parsed_build.group("arch") != wanted_arch
            or parsed_build.group("cuda") != cuda_version
            or parsed_build.group("python") != python_version
        ):
            continue
        destination_repository, _destination_tag = _tag_reference(image.get("destination_reference"))
        manifest_sha = image.get("runnable_manifest_sha256")
        _require_digest(manifest_sha, "runnable manifest SHA-256")
        digest_reference = f"{destination_repository}@sha256:{manifest_sha}"
        if digest_reference == runtime_image_reference:
            image_matches.append((identity, image, digest_reference))
    if len(image_matches) != 1:
        raise ImagePreflightInputError("runtime image must match exactly one selected digest-pinned matrix image")

    identity, image, digest_reference = image_matches[0]
    binding = {
        "preflight_sha256": preflight["preflight_sha256"],
        "build_input_extension": dict(extension),
        "runtime_image": {
            "identity": dict(identity),
            "reference": digest_reference,
            "runnable_manifest_sha256": image["runnable_manifest_sha256"],
        },
    }
    return {**dict(base_inputs), "ci_image_preflight": binding}


def _tag_reference(value: Any) -> tuple[str, str]:
    if not isinstance(value, str) or any(character.isspace() for character in value) or "@" in value:
        raise ImagePreflightInputError("destination image reference must be a tagged repository")
    repository, separator, tag = value.rpartition(":")
    if not separator or not _REPOSITORY.fullmatch(repository) or not _TAG.fullmatch(tag):
        raise ImagePreflightInputError("destination image reference must contain a repository and tag")
    return repository, tag


def _release_train_sha256(train: Mapping[str, Any]) -> str:
    if not isinstance(train, Mapping):
        raise ImagePreflightInputError("release train must be an object")
    recorded = train.get("train_sha256")
    payload = dict(train)
    payload.pop("train_sha256", None)
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    if recorded != digest:
        raise ImagePreflightInputError("release train canonical digest does not match its contents")
    return digest


def _verify_hash(value: Mapping[str, Any], field: str, label: str) -> None:
    digest = value.get(field)
    _require_digest(digest, f"{label} SHA-256")
    payload = dict(value)
    del payload[field]
    if hashlib.sha256(_canonical(payload)).hexdigest() != digest:
        raise ImagePreflightInputError(f"{label} canonical SHA-256 does not match its contents")


def _require_digest(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ImagePreflightInputError(f"{label} is invalid")


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as error:
        raise ImagePreflightInputError("candidate build inputs contain noncanonical JSON values") from error


def _read_json(path: Path) -> Any:
    try:
        content = path.read_bytes()
        if len(content) > _MAX_INPUT_BYTES:
            raise ImagePreflightInputError(f"input exceeds {_MAX_INPUT_BYTES} bytes: {path.name}")
        return json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
    except ImagePreflightInputError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImagePreflightInputError(f"unable to read valid UTF-8 JSON from {path.name}") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImagePreflightInputError(f"JSON object repeats key {key!r}")
        result[key] = value
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-inputs", type=Path, required=True)
    parser.add_argument("--release-train", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--consumer-id", required=True)
    parser.add_argument("--runtime-image-reference", required=True)
    parser.add_argument("--unit-id", required=True)
    parser.add_argument("--family", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--cuda-version", required=True)
    parser.add_argument("--python-version", required=True)
    args = parser.parse_args()
    try:
        result = attach_image_preflight(
            _read_json(args.base_inputs),
            _read_json(args.release_train),
            _read_json(args.preflight),
            consumer_id=args.consumer_id,
            runtime_image_reference=args.runtime_image_reference,
            unit_id=args.unit_id,
            family=args.family,
            repository=args.repository,
            source_sha=args.source_sha,
            arch=args.arch,
            cuda_version=args.cuda_version,
            python_version=args.python_version,
        )
    except ImagePreflightInputError as error:
        print(f"candidate CI-image BuildInput rejected: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
