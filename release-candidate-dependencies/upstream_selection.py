# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Select the upstream candidate packages one build row installs.

Dependency setup and the reuse plan both call this, so they agree on exactly which upstream packages a row consumes
and compute the same reuse key from them (plans/per-row-reuse.md, 4b). It replaces the jq program setup.sh used,
and also applies the wheel filter setup applied while downloading, so the result is the exact set the row installs.

Package metadata determines compatibility. A producing job's matrix only describes how the package was made:
rapids-logger, for example, can be built in a CUDA 13 job but has no CUDA dependency and is valid for CUDA 12.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from select_wheel import supports_arch, supports_python

_CONDA_PLATFORMS = {"amd64": "linux-64", "x86_64": "linux-64", "arm64": "linux-aarch64", "aarch64": "linux-aarch64"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CUDA_MAJOR = re.compile(r"[0-9]+\Z")


def select_upstream(
    train: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    target_unit: str,
    family: str,
    arch: str,
    cuda_major: str,
    python_version: str,
) -> list[dict[str, Any]]:
    """Return the upstream packages a row of ``target_unit`` installs, each with its catalog sha256."""
    if arch not in _CONDA_PLATFORMS:
        raise ValueError(f"unsupported candidate architecture: {arch}")
    if _CUDA_MAJOR.fullmatch(cuda_major) is None:
        raise ValueError(f"CUDA major must be a number: {cuda_major!r}")
    conda_platform = _CONDA_PLATFORMS[arch]
    units = {unit["id"]: unit for unit in train["release_units"]}

    dependencies = family_dependencies(units, target_unit, family)

    chosen = []
    for record in records:
        for entry in record["entries"]:
            package = entry["package"]
            if entry["release_catalog_key"] not in dependencies:
                continue
            # Older envelopes have no cuda_major; treating that as compatible is a deliberate transition rule.
            if _jq_or(package.get("cuda_major"), cuda_major) != cuda_major:
                continue
            if family == "conda" and package.get("platform") not in {"noarch", conda_platform}:
                continue
            if (
                family in {"maven", "docs", "container"}
                and package.get("ecosystem") == "maven"
                and package.get("platform") != f"{arch}-cuda{cuda_major}"
            ):
                continue
            chosen.append(
                {
                    "release_catalog_key": entry["release_catalog_key"],
                    "source": record.get("source"),
                    "artifact_key": record.get("artifact_key"),
                    "producer_build_input_digest": record.get("build_input_digest"),
                    "path": entry["path"],
                    "package": package,
                    "sha256": entry.get("sha256"),
                }
            )

    # The same package may come from several producing jobs; prefer the one built for this row's Python.
    chosen = _stable_sort(
        chosen,
        lambda item: [
            item["package"].get("name"),
            item["package"].get("version"),
            0 if (item["source"] or {}).get("matrix", {}).get("python_version") == python_version else 1,
        ],
    )
    chosen = _first_of_each_group(
        chosen,
        lambda item: [
            item["package"].get("name"),
            item["package"].get("version"),
            item["package"].get("build"),
            item["package"].get("platform"),
            item["path"],
        ],
    )

    selected = []
    for item in chosen:
        wheel = Path(item["path"]).name
        # A wheel the row can't install (another Python or architecture) is never downloaded or keyed.
        if item["path"].endswith(".whl") and not (
            supports_python(wheel, python_version) and supports_arch(wheel, arch)
        ):
            continue
        if not isinstance(item["sha256"], str) or _SHA256.fullmatch(item["sha256"]) is None:
            raise ValueError(f"catalog entry has no valid sha256: {item['release_catalog_key']} {item['path']}")
        selected.append(item)
    return selected


def family_dependencies(units: dict[str, Any], target_unit: str, family: str) -> set[str]:
    """The units whose packages a row of ``target_unit`` in ``family`` may install: those of its family in its closure.

    train_references.py reads only these units' repositories, so this is the one place that says which they are.
    """
    family_prefixes = ("conda:", "maven:") if family in {"docs", "container", "maven"} else (family + ":",)
    return {dependency for dependency in _closure(units, target_unit) if dependency.startswith(family_prefixes)}


def _closure(units: dict[str, Any], unit_id: str) -> list[str]:
    found = set()
    for dependency in units.get(unit_id, {}).get("dependencies") or []:
        if dependency in units and units[dependency].get("artifact_family") != "source":
            found.add(dependency)
            found.update(_closure(units, dependency))
    return sorted(found)


def _jq_or(value: Any, default: Any) -> Any:
    return default if value is None or value is False else value


def _jq_order(value: Any) -> tuple:
    """Sort key giving jq's ordering: null < false < true < numbers < strings < arrays < objects."""
    if value is None:
        return (0,)
    if value is False:
        return (1,)
    if value is True:
        return (2,)
    if isinstance(value, (int, float)):
        return (3, value)
    if isinstance(value, str):
        return (4, value)
    if isinstance(value, list):
        return (5, tuple(_jq_order(item) for item in value))
    return (6, tuple(sorted((key, _jq_order(item)) for key, item in value.items())))


def _stable_sort(items: list, key) -> list:
    return sorted(items, key=lambda item: _jq_order(key(item)))


def _first_of_each_group(items: list, key) -> list:
    """jq group_by(key) | map(.[0]): sort stably by the key, keep the first of each run of equal keys."""
    result = []
    previous = object()
    for item in _stable_sort(items, key):
        current = _jq_order(key(item))
        if current != previous:
            result.append(item)
            previous = current
    return result


def main(argv: list[str] | None = None) -> int:
    """Print the selected upstream packages as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", required=True, type=Path)
    parser.add_argument("--records", required=True, type=Path, help="JSON list of catalog envelopes.")
    parser.add_argument("--target-unit", required=True)
    parser.add_argument("--family", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--cuda-major", required=True)
    parser.add_argument("--python-version", default="")
    args = parser.parse_args(argv)
    try:
        selected = select_upstream(
            json.loads(args.train.read_text()),
            json.loads(args.records.read_text()),
            target_unit=args.target_unit,
            family=args.family,
            arch=args.arch,
            cuda_major=args.cuda_major,
            python_version=args.python_version,
        )
    except ValueError as error:
        parser.error(str(error))
    json.dump(selected, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
