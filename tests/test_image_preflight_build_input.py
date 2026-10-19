# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Tests for binding authenticated image preflight into candidate BuildInputs."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "release-candidate-dependencies"))

from attach_image_preflight import (
    ImagePreflightInputError,
    attach_image_preflight,
)


def _canonical_sha(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _fixture(manifest_sha: str = "b" * 64) -> tuple[dict, dict, dict, dict]:
    source_sha = "a" * 40
    train = {
        "release_version": "26.12",
        "release_units": [
            {
                "id": "conda:example",
                "artifact_family": "conda",
                "repository": "example",
                "source_revision": source_sha,
            }
        ],
        "source_revisions": {"example": source_sha},
    }
    identity = {
        "ecosystem": "container",
        "name": "docker.io/rapidsai/ci-conda",
        "version": "26.12",
        "build": "26.12-cuda12.9.2-rockylinux8-py3.12-amd64",
        "platform": "linux/amd64",
    }
    train["artifact_expectations"] = {"container:ci-images": [dict(identity)]}
    train["train_sha256"] = _canonical_sha(train)
    image = {
        "identity": identity,
        "destination_reference": f"registry.example/private/ci-conda:{identity['build']}",
        "runnable_manifest_sha256": manifest_sha,
    }
    extension = {
        "schema_version": 1,
        "train_sha256": train["train_sha256"],
        "request_sha256": "c" * 64,
        "consumption_receipt_sha256": "d" * 64,
        "consumer_id": "example-test",
        "release_unit_id": "conda:example",
        "source_sha": source_sha,
        "images": [image],
    }
    extension["sha256"] = _canonical_sha(extension)
    consumer = {
        "consumer_id": "example-test",
        "release_unit_id": "conda:example",
        "source_sha": source_sha,
        "images": [image],
        "build_input_extension": extension,
    }
    preflight = {
        "schema_version": 1,
        "producer": "release-platform-ci-image-preflight",
        "train_sha256": train["train_sha256"],
        "request_sha256": extension["request_sha256"],
        "consumption_receipt_sha256": extension["consumption_receipt_sha256"],
        "selected_consumer_unit_ids": ["conda:example"],
        "consumers": [consumer],
    }
    preflight["preflight_sha256"] = _canonical_sha(preflight)
    base = {"schema_version": 1, "source_inputs": {"sources": {"example": source_sha}}}
    return train, preflight, base, image


def _attach(train: dict, preflight: dict, base: dict, image: dict) -> dict:
    destination_repository = image["destination_reference"].rsplit(":", maxsplit=1)[0]
    return attach_image_preflight(
        base,
        train,
        preflight,
        consumer_id="example-test",
        runtime_image_reference=f"{destination_repository}@sha256:{image['runnable_manifest_sha256']}",
        unit_id="conda:example",
        family="conda",
        repository="example",
        source_sha="a" * 40,
        arch="x86_64",
        cuda_version="12.9.2",
        python_version="3.12",
    )


def test_attaches_the_exact_digest_pinned_image_to_build_inputs() -> None:
    train, preflight, base, image = _fixture()

    result = _attach(train, preflight, base, image)

    binding = result["ci_image_preflight"]
    assert binding["preflight_sha256"] == preflight["preflight_sha256"]
    assert binding["build_input_extension"] == preflight["consumers"][0]["build_input_extension"]
    assert binding["runtime_image"]["identity"]["name"] == "docker.io/rapidsai/ci-conda"
    assert binding["runtime_image"]["reference"].startswith("registry.example/private/ci-conda@sha256:")
    assert result["source_inputs"] == base["source_inputs"]


def test_distinct_valid_image_bytes_change_canonical_build_inputs() -> None:
    train, first_preflight, base, first_image = _fixture("b" * 64)
    first = _attach(train, first_preflight, base, first_image)
    _, second_preflight, _, second_image = _fixture("e" * 64)
    second = _attach(train, second_preflight, base, second_image)

    assert _canonical_sha(first) != _canonical_sha(second)
    assert _canonical_sha(base) != _canonical_sha(first)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_image_reference", "registry.example/private/ci-conda:26.12-cuda12.9.2-rockylinux8-py3.12-amd64"),
        ("runtime_image_reference", "registry.example/other-image@sha256:" + "b" * 64),
        ("consumer_id", "another-consumer"),
    ],
)
def test_rejects_unbound_runtime_or_consumer(field: str, value: str) -> None:
    train, preflight, base, image = _fixture()
    kwargs = {
        "consumer_id": "example-test",
        "runtime_image_reference": f"registry.example/private/ci-conda@sha256:{image['runnable_manifest_sha256']}",
        "unit_id": "conda:example",
        "family": "conda",
        "repository": "example",
        "source_sha": "a" * 40,
        "arch": "amd64",
        "cuda_version": "12.9.2",
        "python_version": "3.12",
    }
    kwargs[field] = value

    with pytest.raises(ImagePreflightInputError):
        attach_image_preflight(base, train, preflight, **kwargs)


def test_rejects_preflight_hash_tampering() -> None:
    train, preflight, base, image = _fixture()
    preflight["consumers"][0]["build_input_extension"]["images"][0]["runnable_manifest_sha256"] = "f" * 64

    with pytest.raises(ImagePreflightInputError, match="preflight canonical SHA-256"):
        _attach(train, preflight, base, image)


def test_rejects_image_identity_outside_frozen_inventory() -> None:
    train, preflight, base, image = _fixture()
    image["identity"]["name"] = "docker.io/other/ci-conda"
    extension = preflight["consumers"][0]["build_input_extension"]
    extension_payload = {key: value for key, value in extension.items() if key != "sha256"}
    extension["sha256"] = _canonical_sha(extension_payload)
    preflight_payload = {key: value for key, value in preflight.items() if key != "preflight_sha256"}
    preflight["preflight_sha256"] = _canonical_sha(preflight_payload)

    with pytest.raises(ImagePreflightInputError, match="not in the frozen CI-image inventory"):
        _attach(train, preflight, base, image)


def test_rejects_digest_pinned_reference_for_wrong_architecture() -> None:
    train, preflight, base, image = _fixture()
    runtime_reference = f"{image['identity']['name']}@sha256:{image['runnable_manifest_sha256']}"

    with pytest.raises(ImagePreflightInputError, match="exactly one"):
        attach_image_preflight(
            base,
            train,
            preflight,
            consumer_id="example-test",
            runtime_image_reference=runtime_reference,
            unit_id="conda:example",
            family="conda",
            repository="example",
            source_sha="a" * 40,
            arch="arm64",
            cuda_version="12.9.2",
            python_version="3.12",
        )
