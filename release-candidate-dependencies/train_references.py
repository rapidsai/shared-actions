#!/usr/bin/env python3
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Choose the train references a build row reads and fetch their catalogs, for setup.sh and the reuse plan.

A train holds a reference for every artifact any run of it built or reused, across every repository. A row can only
install packages of the units its own unit depends on (upstream_selection.py), so references of other repositories
are skipped before anything else is read: 26.12 release run 46094059402 spent about 150 of its 176 seconds of
dependency setup fetching, one at a time, catalogs of every repository in the train.

Each remaining reference is admitted or dropped by reference-binding.sh itself; of the references of one artifact bound
to this run the newest attempt is kept; catalog-bundle-key.sh names each kept reference's catalog. Those script calls
and the catalog fetches run in parallel. The catalogs are returned in the order setup's glob read them, each with its
artifact key and build input digest.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess  # nosec B404 - bash with this directory's scripts, and the AWS CLI, with fixed arguments
import sys
import tempfile
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from upstream_selection import family_dependencies

_DIRECTORY = Path(__file__).resolve().parent
_ARTIFACT_KEY = re.compile(r"artifacts/[^/]+/[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
WORKERS = 16


class TrainReferenceError(Exception):
    """A train reference setup must refuse."""


def needed_repositories(train: Mapping[str, Any], target_unit: str, family: str) -> set[str] | None:
    """The repositories whose packages a row of ``target_unit`` may install, or None when the train can't say."""
    units = {unit["id"]: unit for unit in train["release_units"]}
    repositories = set()
    for unit_id in family_dependencies(units, target_unit, family):
        repository = units[unit_id].get("repository")
        if not isinstance(repository, str) or not repository:
            return None
        repositories.add(repository.rsplit("/", 1)[-1])
    return repositories


def _script(name: str, arguments: list[str], environment: Mapping[str, str]) -> str:
    result = subprocess.run(  # noqa: S603 # nosec B603 - this directory's scripts with fixed arguments
        [shutil.which("bash") or "bash", str(_DIRECTORY / name), *arguments],
        env=dict(environment),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise TrainReferenceError(result.stderr.strip() or f"{name} failed")
    return result.stdout.strip()


def select_references(
    references: Path,
    *,
    repositories: set[str] | None,
    environment: Mapping[str, str],
    workers: int = WORKERS,
) -> list[Path]:
    """The references under ``references`` this row reads, in setup's order.

    ``repositories`` limits them to those repositories (None keeps every one). ``environment`` is what
    reference-binding.sh needs: GITHUB_RUN_ID, the bucket, the train prefix and digest, SCHEDULER_STATE and
    REUSE_BINDINGS.
    """
    paths = sorted(references.rglob("bundle-reference.*.json"), key=lambda path: path.parts)
    candidates = []
    for path in paths:
        reference = json.loads(path.read_text())
        if not isinstance(reference, dict) or reference.get("schema_version") != 4 or reference.get(
            "storage_layout"
        ) != 2:
            raise TrainReferenceError("candidate train contains a reference that does not use storage layout 2")
        artifact = str(reference.get("source_artifact") or "")
        if path.name != f"bundle-reference.{artifact}.json":
            raise TrainReferenceError("candidate train contains a reference whose filename does not match its source artifact")
        if repositories is not None and str(reference.get("repository") or "").rsplit("/", 1)[-1] not in repositories:
            continue
        candidates.append((path, reference))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        bindings = list(
            pool.map(lambda item: _script("reference-binding.sh", [str(item[0])], environment), candidates)
        )
    selected: dict[str, Path] = {}
    attempts: dict[str, int] = {}
    kept: list[Path] = []
    for (path, reference), binding in zip(candidates, bindings, strict=True):
        identity = f"{reference.get('repository')}/{reference.get('source_artifact')}"
        if binding == "drop":
            continue
        if binding == "reused":
            if identity in selected:
                raise TrainReferenceError(f"candidate train selects more than one reference for {identity}")
            selected[identity] = path
        elif binding == "bound":
            attempt = str(reference.get("source_run_attempt") or "")
            if not attempt.isdigit():
                raise TrainReferenceError("candidate train has an invalid selected run attempt")
            if identity in attempts and int(attempt) < attempts[identity]:
                continue
            if identity in selected:
                kept.remove(selected[identity])
            attempts[identity] = int(attempt)
            selected[identity] = path
        elif binding != "unbound":
            raise TrainReferenceError(f"unexpected reference binding decision: {binding}")
        if not _ARTIFACT_KEY.fullmatch(str(reference.get("artifact_key") or "")):
            raise TrainReferenceError("candidate train contains an unsafe artifact reference")
        if not _DIGEST.fullmatch(str(reference.get("build_input_digest") or "")):
            raise TrainReferenceError("candidate train contains a missing or unsafe producer build-input digest")
        kept.append(path)
    return kept


def catalog_records(
    references: Path,
    kept: list[Path],
    *,
    fetch: Callable[[str], bytes | None],
    environment: Mapping[str, str],
    workers: int = WORKERS,
) -> list[dict[str, Any]]:
    """Fetch the catalog of each kept reference, with its artifact key and build input digest, in setup's order."""

    def one(path: Path) -> tuple[tuple[str, ...], dict[str, Any]]:
        reference = json.loads(path.read_text())
        directory = path.parent.relative_to(references).as_posix()
        bundle_key = _script("catalog-bundle-key.sh", [str(path), directory], environment)
        data = fetch(bundle_key)
        if data is None:
            raise TrainReferenceError(f"the catalog of a train reference is missing: {bundle_key}")
        catalog = {
            **json.loads(data),
            "artifact_key": reference["artifact_key"],
            "build_input_digest": reference["build_input_digest"],
        }
        return (*path.parent.parts, bundle_key.rsplit("/", 1)[-1]), catalog

    with ThreadPoolExecutor(max_workers=workers) as pool:
        catalogs = list(pool.map(one, kept))
    return [catalog for _, catalog in sorted(catalogs, key=lambda item: item[0])]


def _aws_fetch(bucket: str) -> Callable[[str], bytes | None]:
    def fetch(key: str) -> bytes | None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "catalog.json"
            result = subprocess.run(  # noqa: S603 # nosec B603 - the AWS CLI with fixed arguments
                [shutil.which("aws") or "aws", "s3", "cp", "--only-show-errors", f"s3://{bucket}/{key}", str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise TrainReferenceError(f"unable to read the catalog {key}: {result.stderr.strip()}")
            return target.read_bytes()

    return fetch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--references", required=True, help="Directory of the train's downloaded references.")
    parser.add_argument("--train", required=True, help="The verified release-train.json.")
    parser.add_argument("--target-unit", required=True)
    parser.add_argument("--family", required=True)
    parser.add_argument("--output", required=True, help="Where to write the catalog records JSON list.")
    args = parser.parse_args(argv)
    environment = dict(os.environ)
    try:
        train = json.loads(Path(args.train).read_text())
        references = Path(args.references)
        repositories = needed_repositories(train, args.target_unit, args.family)
        kept = select_references(references, repositories=repositories, environment=environment)
        records = catalog_records(
            references, kept, fetch=_aws_fetch(environment["RELEASE_CANDIDATE_BUCKET"]), environment=environment
        )
        if not records and (repositories is None or repositories):
            units = {unit["id"]: unit for unit in train["release_units"]}
            if family_dependencies(units, args.target_unit, args.family):
                raise TrainReferenceError(
                    f"no completed candidate catalog envelopes found for {args.target_unit}'s declared dependencies"
                )
    except (TrainReferenceError, KeyError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    Path(args.output).write_text(json.dumps(records))
    scope = "every repository" if repositories is None else ", ".join(sorted(repositories)) or "no repository"
    print(f"read {len(records)} catalogs of {len(kept)} references from {scope}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
