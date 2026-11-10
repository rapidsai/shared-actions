# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
"""Versioned CI-image recipe identity for explicit retained-result adoption.

A retained image is a selected prior resolved environment, not a claim that an
online cold build would reproduce its bytes. The selected archive, original
train and execution evidence remain the authority for resolved base/packages.
This descriptor compares only the declared recipe inputs that must stay fixed
when adopting that archived result into a different train.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_REFRESH = re.compile(r"[A-Za-z0-9._-]{0,128}\Z")
_PACKAGE_KEYS = frozenset({"ecosystem", "name", "version", "build", "platform"})
_IMPLEMENTATIONS = frozenset({"gha-tools", "shared-actions", "shared-workflows"})
_DESCRIPTOR_KEYS = frozenset(
    {
        "schema_version",
        "resolution_policy",
        "source",
        "package",
        "build_implementation_revisions",
        "build_implementation_repositories",
        "source_inputs",
        "build_policy",
        "refresh_key",
    }
)


def image_reuse_policy(train: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return an explicit frozen image policy; legacy trains remain conservative."""
    if not isinstance(train, Mapping):
        raise ValueError("image policy requires a release-train mapping")
    policy = train.get("candidate_image_policy")
    if policy is None:
        return None
    if not isinstance(policy, Mapping) or set(policy) != {"schema_version", "mode", "refresh_key"}:
        raise ValueError("candidate image policy must contain schema_version, mode and refresh_key")
    if type(policy["schema_version"]) is not int or policy["schema_version"] != 1:
        raise ValueError("candidate image policy schema_version must be integer 1")
    if policy["mode"] not in ("retain", "refresh"):
        raise ValueError("candidate image policy mode must be retain or refresh")
    if not isinstance(policy["refresh_key"], str) or not _REFRESH.fullmatch(policy["refresh_key"]):
        raise ValueError("candidate image refresh_key must be at most 128 safe identifier characters")
    return dict(policy)


def _package_identity(package: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(package, Mapping) or set(package) != _PACKAGE_KEYS:
        raise ValueError("CI image identity requires ecosystem, name, version, build and platform")
    if any(not isinstance(value, str) or not value for value in package.values()):
        raise ValueError("CI image identity fields must be nonempty strings")
    name = package["name"]
    if package["ecosystem"] != "container" or name not in {
        "docker.io/rapidsai/ci-conda",
        "docker.io/rapidsai/ci-wheel",
        "docker.io/rapidsai/citestwheel",
    }:
        raise ValueError("retained image identity must name a supported CI image")
    if package["platform"] not in ("linux/amd64", "linux/arm64"):
        raise ValueError("CI image identity must select a supported Linux architecture")
    arch = package["platform"].removeprefix("linux/")
    if not re.fullmatch(r"[0-9]{2}\.[0-9]{2}", package["version"]):
        raise ValueError("CI image version must use YY.MM")
    if not re.fullmatch(
        re.escape(package["version"]) + r"-cuda[0-9.]+-[a-z0-9.]+-py[0-9]+\.[0-9]+t?-" + arch,
        package["build"],
    ):
        raise ValueError("CI image build must bind its version, CUDA, OS, Python and architecture")
    return dict(package)


def _revisions(value: Any, *, keys: frozenset[str] | None = None) -> dict[str, str]:
    if not isinstance(value, Mapping) or (keys is not None and set(value) != keys):
        raise ValueError("image input revision mapping is missing or has unsupported names")
    if any(
        not isinstance(name, str) or not isinstance(sha, str) or not _SHA.fullmatch(sha) for name, sha in value.items()
    ):
        raise ValueError("image input revisions must be full lowercase Git SHAs")
    return dict(value)


def _repositories(value: Any, *, keys: frozenset[str]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("image input repository mapping is missing or has unsupported names")
    if any(not isinstance(repo, str) or not _REPOSITORY.fullmatch(repo) for repo in value.values()):
        raise ValueError("image input repositories must use owner/repository format")
    return dict(value)


def image_source_inputs(train: Mapping[str, Any]) -> dict[str, Any]:
    """Scope dependency identity to inputs consumed by the image recipe.

    The independent image root consumes no train source/package dependencies.
    Its runtime gha-tools archive is already bound by the implementation map;
    bootstrap versions are committed in the recipe's versions.yaml. Train
    CMake, metadata and dependency-generator sources do not enter the image.
    The original complete record is still checked against its original train.
    """
    units = train.get("release_units")
    if not isinstance(units, list):
        raise ValueError("CI image inputs require frozen release units")
    root = next((unit for unit in units if isinstance(unit, Mapping) and unit.get("id") == "container:ci-images"), None)
    if root is None or root.get("dependencies", []) != []:
        raise ValueError("producer-scoped image reuse requires a CI-image root without train dependencies")
    return {"schema_version": 1, "sources": {}, "build_tools": {}}


def legacy_image_build_inputs(
    train: Mapping[str, Any], package: Mapping[str, Any], *, refresh_key: str = ""
) -> dict[str, Any]:
    """Describe prior declared inputs only after its original train was verified.

    This adapter does not authorize reuse. The caller must independently check
    the original train, completed execution, archive and complete variant set.
    """
    policy = {"schema_version": 1, "mode": "retain", "refresh_key": refresh_key}
    image_reuse_policy({"candidate_image_policy": policy})
    original_policy = image_reuse_policy(train)
    if refresh_key and (original_policy is None or original_policy["refresh_key"] != refresh_key):
        raise ValueError("a prior image cannot be assigned a different freshness generation")
    package = _package_identity(package)
    revisions = train.get("source_revisions", {})
    repositories = train.get("github_repositories", {})
    if not isinstance(revisions, Mapping) or not isinstance(repositories, Mapping):
        raise ValueError("CI image inputs require frozen source mappings")
    source = {"repository": repositories.get("ci-imgs"), "sha": revisions.get("ci-imgs")}
    _repositories({"ci-imgs": source["repository"]}, keys=frozenset({"ci-imgs"}))
    _revisions({"ci-imgs": source["sha"]})
    build_policy = train.get("candidate_build_policy")
    if (
        not isinstance(build_policy, Mapping)
        or set(build_policy) != {"sccache"}
        or type(build_policy["sccache"]) is not bool
    ):
        raise ValueError("CI image inputs require an exact compiler-cache policy")
    return validate_image_build_inputs(
        {
            "schema_version": 1,
            "resolution_policy": "retain-prior-resolved-environment",
            "source": source,
            "package": package,
            "build_implementation_revisions": _revisions(
                train.get("build_implementation_revisions"), keys=_IMPLEMENTATIONS
            ),
            "build_implementation_repositories": _repositories(
                train.get("build_implementation_repositories"), keys=_IMPLEMENTATIONS
            ),
            "source_inputs": image_source_inputs(train),
            "build_policy": dict(build_policy),
            "refresh_key": refresh_key,
        }
    )


def expected_image_build_inputs(train: Mapping[str, Any], package: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the recipe descriptor for a new policy-bearing image train."""
    policy = image_reuse_policy(train)
    if policy is None:
        return None
    return legacy_image_build_inputs(train, package, refresh_key=policy["refresh_key"])


def validate_image_build_inputs(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the complete declared-input descriptor without discarding fields."""
    if not isinstance(descriptor, Mapping) or set(descriptor) != _DESCRIPTOR_KEYS:
        raise ValueError("image build descriptor has missing or unsupported fields")
    if type(descriptor["schema_version"]) is not int or descriptor["schema_version"] != 1:
        raise ValueError("image build descriptor schema_version must be integer 1")
    if descriptor["resolution_policy"] != "retain-prior-resolved-environment":
        raise ValueError("unsupported image resolution policy")
    source = descriptor["source"]
    if not isinstance(source, Mapping) or set(source) != {"repository", "sha"}:
        raise ValueError("image descriptor requires its exact recipe source")
    _repositories({"source": source["repository"]}, keys=frozenset({"source"}))
    _revisions({"source": source["sha"]})
    _package_identity(descriptor["package"])
    _revisions(descriptor["build_implementation_revisions"], keys=_IMPLEMENTATIONS)
    _repositories(descriptor["build_implementation_repositories"], keys=_IMPLEMENTATIONS)
    image_reuse_policy(
        {"candidate_image_policy": {"schema_version": 1, "mode": "retain", "refresh_key": descriptor["refresh_key"]}}
    )
    build_policy = descriptor["build_policy"]
    if (
        not isinstance(build_policy, Mapping)
        or set(build_policy) != {"sccache"}
        or type(build_policy["sccache"]) is not bool
    ):
        raise ValueError("image descriptor requires an exact compiler-cache policy")
    inputs = descriptor["source_inputs"]
    if not isinstance(inputs, Mapping) or set(inputs) != {"schema_version", "sources", "build_tools"}:
        raise ValueError("image descriptor requires its complete source/tool input mapping")
    if type(inputs["schema_version"]) is not int or inputs["schema_version"] != 1 or inputs["sources"] != {}:
        raise ValueError("image source inputs must describe an independent root")
    if inputs["build_tools"] != {}:
        raise ValueError("image source inputs cannot invent unconsumed train build tools")
    return json.loads(json.dumps(descriptor, sort_keys=True))


def image_build_inputs_sha256(descriptor: Mapping[str, Any]) -> str:
    """Address one exact, validated declared-input descriptor."""
    value = validate_image_build_inputs(descriptor)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main() -> None:
    """Write the exact image descriptor from a frozen train and execution catalog."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    train = json.loads(args.train.read_text())
    catalog = json.loads(args.catalog.read_text())
    entries = catalog.get("entries")
    if (
        not isinstance(entries, list)
        or len(entries) != 1
        or entries[0].get("release_catalog_key") != "container:ci-images"
    ):
        raise ValueError("image descriptor requires exactly one CI-image catalog entry")
    descriptor = expected_image_build_inputs(train, entries[0]["package"])
    if descriptor is None:
        raise ValueError("image descriptor requires an explicit frozen image policy")
    if (
        catalog.get("source", {}).get("repository") != descriptor["source"]["repository"]
        or catalog.get("source", {}).get("sha") != descriptor["source"]["sha"]
    ):
        raise ValueError("image execution catalog source differs from its frozen recipe")
    args.output.write_text(json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
