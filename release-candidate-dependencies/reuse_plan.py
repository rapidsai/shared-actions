#!/usr/bin/env python3
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Decide every package build row of a release run before any build job starts (plans/per-row-reuse.md, 4b).

The generator writes a manifest of the release file's package jobs: each job's repository, unit, family, release
entrypoint, static identity (candidate-row-identity), the jobs it needs and its matrix. Jobs are decided in
dependency order. A job is eligible only when every job it needs, and every job producing a unit its unit consumes,
was reused in full: then every input of its rows is known, the upstream ones being the references just bound. Each
eligible row's key is computed with the same modules dependency setup uses (upstream_selection.py,
row_key_inputs.py, row_reuse_key.py), looked up and verified as the in-row check does (row_reuse.py), and bound on a
hit. Any other row is left to build; its in-row check can still reuse it once its upstream finished.

Writes to --github-output:
- ``matrices``: JSON object, job id -> the matrix of its rows that build (only jobs with such rows);
- ``reused-jobs``: JSON list of the jobs whose every row was reused, which the release file skips.
Writes a table of every row and its outcome to --summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess  # nosec B404 - jq, bash and this directory's scripts with fixed arguments
import sys
import tempfile
from pathlib import Path
from typing import Any

from row_key_inputs import build_inputs, upstream_document
from row_reuse import HEX64, S3, SAFE, bind_reuse, find_reuse
from upstream_selection import select_upstream

_DIRECTORY = Path(__file__).resolve().parent
_JOB = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_ENTRYPOINT = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_ROW_FIELDS = ("ARCH", "CUDA_VER", "PY_VER", "candidate_image_key")


class PlanError(Exception):
    """The plan cannot be made safely; the run must stop."""


def _validate_manifest(manifest: Any) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise PlanError("the reuse plan manifest must be a schema_version 1 object")
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise PlanError("the reuse plan manifest lists no jobs")
    ids = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise PlanError("a manifest job must be an object")
        job_id = job.get("id")
        if not isinstance(job_id, str) or _JOB.fullmatch(job_id) is None or job_id in ids:
            raise PlanError(f"manifest job id is invalid or repeated: {job_id!r}")
        ids.add(job_id)
        if not isinstance(job.get("repository"), str) or _REPOSITORY.fullmatch(job["repository"]) is None:
            raise PlanError(f"{job_id}: invalid repository")
        if not SAFE.fullmatch(job["repository"].rsplit("/", 1)[-1]):
            raise PlanError(f"{job_id}: unsafe repository name")
        if not isinstance(job.get("project"), str) or not SAFE.fullmatch(job["project"]):
            raise PlanError(f"{job_id}: invalid project")
        if job.get("family") not in {"conda", "wheel"}:
            raise PlanError(f"{job_id}: the reuse plan decides conda and wheel rows only")
        if not isinstance(job.get("unit"), str) or not job["unit"].startswith(job["family"] + ":"):
            raise PlanError(f"{job_id}: unit must belong to its family")
        if not isinstance(job.get("entrypoint"), str) or _ENTRYPOINT.fullmatch(job["entrypoint"]) is None:
            raise PlanError(f"{job_id}: invalid entrypoint")
        identity = job.get("identity")
        if (
            not isinstance(identity, dict)
            or not identity
            or not all(isinstance(value, (str, bool, int, float)) for value in identity.values())
        ):
            raise PlanError(f"{job_id}: identity must be a non-empty object of scalars")
        if not isinstance(job.get("needs"), list) or not all(isinstance(need, str) for need in job["needs"]):
            raise PlanError(f"{job_id}: needs must be a list of job ids")
        rows = (job.get("matrix") or {}).get("include") if isinstance(job.get("matrix"), dict) else None
        if not isinstance(rows, list) or not rows:
            raise PlanError(f"{job_id}: matrix must include at least one row")
        for row in rows:
            if not isinstance(row, dict) or not all(isinstance(row.get(field), str) for field in _ROW_FIELDS):
                raise PlanError(f"{job_id}: every row needs {', '.join(_ROW_FIELDS)}")
    for job in jobs:
        unknown = [need for need in job["needs"] if need not in ids]
        if unknown:
            raise PlanError(f"{job['id']}: needs unknown jobs {unknown}")
    return _in_dependency_order(jobs)


def _in_dependency_order(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {job["id"]: job for job in jobs}
    ordered: list[dict[str, Any]] = []
    state: dict[str, str] = {}

    def visit(job_id: str) -> None:
        if state.get(job_id) == "done":
            return
        if state.get(job_id) == "visiting":
            raise PlanError(f"the manifest's needs form a cycle through {job_id}")
        state[job_id] = "visiting"
        for need in by_id[job_id]["needs"]:
            visit(need)
        state[job_id] = "done"
        ordered.append(by_id[job_id])

    for job in jobs:
        visit(job["id"])
    return ordered


def _closure(train: dict[str, Any], unit: str) -> set[str]:
    units = {item["id"]: item for item in train["release_units"]}
    found: set[str] = set()
    pending = [unit]
    while pending:
        for dependency in units.get(pending.pop(), {}).get("dependencies") or []:
            if dependency in units and units[dependency].get("artifact_family") != "source" and dependency not in found:
                found.add(dependency)
                pending.append(dependency)
    return found


def _load_train(s3: S3, prefix: str, train_sha256: str, workspace: Path) -> tuple[dict[str, Any], Path]:
    data = s3.get(f"{prefix}/{train_sha256}/release-train.json")
    if data is None:
        raise PlanError("the release train is missing from the candidate store")
    path = workspace / "release-train.json"
    path.write_bytes(data)
    # The same canonical form load-train and setup check: jq -cSj 'del(.train_sha256)'.
    canonical = subprocess.run(  # noqa: S603 # nosec B603
        [shutil.which("jq") or "jq", "-cSj", "del(.train_sha256)", str(path)], capture_output=True, check=True
    ).stdout
    if hashlib.sha256(canonical).hexdigest() != train_sha256:
        raise PlanError("the stored release train does not match its digest")
    return json.loads(data), path


def _require_bound(s3: S3, prefix: str, train_sha256: str, run_id: str, jobs: list[dict[str, Any]]) -> None:
    """Every repository must be bound to this run, or setup would read references the plan can't see."""
    data = s3.get(f"{prefix}/{train_sha256}/scheduler-state.json")
    state = json.loads(data) if data is not None else {}
    runs = state.get("workflow_runs") if isinstance(state, dict) else None
    for repository in sorted({job["repository"].rsplit("/", 1)[-1] for job in jobs}):
        bound = runs.get(repository) if isinstance(runs, dict) else None
        if str(bound) != run_id:
            raise PlanError(f"{repository} is bound to run {bound!r}, not this run {run_id}")


def _source_inputs(train_path: Path, unit: str, workspace: Path) -> dict[str, Any]:
    output = workspace / f"source-inputs.{unit.replace(':', '-')}.json"
    subprocess.run(  # noqa: S603 # nosec B603
        [
            shutil.which("bash") or "bash",
            str(_DIRECTORY / "write-source-inputs.sh"),
            "--train",
            str(train_path),
            "--output",
            str(output),
            "--target-unit",
            unit,
        ],
        check=True,
    )
    return json.loads(output.read_text())


def _catalog_records(s3: S3, references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The catalog envelope of each bound reference, as setup reads it."""
    records = []
    for reference in references:
        data = s3.get(reference["bundle_key"])
        if data is None:
            raise PlanError(f"the catalog of a reference just bound is missing: {reference['bundle_key']}")
        records.append(
            {
                **json.loads(data),
                "artifact_key": reference["artifact_key"],
                "build_input_digest": reference["build_input_digest"],
            }
        )
    return records


def _row_key(  # noqa: PLR0913 - every argument is one of the key's inputs.
    job: dict[str, Any],
    row: dict[str, str],
    train: dict[str, Any],
    build: dict[str, Any],
    records: list[dict[str, Any]],
    image_map: dict[str, str],
    workspace: Path,
) -> tuple[str, Any] | None:
    """The row's reuse key and key record, computed as setup computes them, or None when it has none."""
    selected = select_upstream(
        train,
        records,
        target_unit=job["unit"],
        family=job["family"],
        arch=row["ARCH"],
        cuda_major=row["CUDA_VER"].split(".", 1)[0],
        python_version=row["PY_VER"],
    )
    directory = Path(tempfile.mkdtemp(dir=workspace))
    (directory / "build-inputs.json").write_text(json.dumps(build))
    (directory / "upstream-inputs.json").write_text(json.dumps(upstream_document(selected)))
    producer_sha = (train.get("source_revisions") or {}).get(job["project"])
    if not isinstance(producer_sha, str) or _SHA.fullmatch(producer_sha) is None:
        raise PlanError(f"{job['id']}: the train has no source revision for {job['project']}")
    revisions = train.get("build_implementation_revisions")
    result = subprocess.run(  # noqa: S603 # nosec B603
        [
            sys.executable,
            str(_DIRECTORY / "row_reuse_key.py"),
            "--producer-repository",
            job["repository"],
            "--producer-sha",
            producer_sha,
            "--unit",
            job["unit"],
            "--family",
            job["family"],
            "--arch",
            row["ARCH"],
            "--cuda-version",
            row["CUDA_VER"],
            "--python-version",
            row["PY_VER"],
            "--implementation-revisions",
            json.dumps(revisions, sort_keys=True, separators=(",", ":")),
            "--runtime-image",
            image_map.get(row["candidate_image_key"], ""),
            "--row-entrypoint",
            job["entrypoint"],
            "--row-identity",
            json.dumps(job["identity"], sort_keys=True),
            "--build-inputs",
            str(directory / "build-inputs.json"),
            "--upstream-inputs",
            str(directory / "upstream-inputs.json"),
            "--output",
            str(directory / "row-reuse-key.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PlanError(f"{job['id']}: cannot compute a reuse key: {result.stderr.strip()}")
    key = result.stdout.strip()
    if not key:
        return None
    record_bytes = (directory / "row-reuse-key.json").read_bytes()
    if not HEX64.fullmatch(key) or hashlib.sha256(record_bytes).hexdigest() != key:
        raise PlanError(f"{job['id']}: the reuse key does not match its key record")
    return key, json.loads(record_bytes)


def _label(row: dict[str, str]) -> str:
    return f"{row['CUDA_VER']}, {row['PY_VER']}, {row['ARCH']}"


def plan(  # noqa: PLR0913 - the run's identity, the store and the outputs.
    s3: S3,
    manifest: Any,
    *,
    train_sha256: str,
    run_id: str,
    run_attempt: str,
    image_map: dict[str, str],
    workspace: Path,
    train_prefix: str = "train-state",
    content_prefix: str = "artifacts",
) -> tuple[dict[str, Any], list[str], list[tuple[str, str, str]]]:
    """Decide every row; return (matrices of rows that build, fully reused job ids, (job, row, outcome) lines)."""
    if not HEX64.fullmatch(train_sha256) or not run_id.isdigit() or not run_attempt.isdigit():
        raise PlanError("invalid train digest, run id or run attempt")
    jobs = _validate_manifest(manifest)
    train, train_path = _load_train(s3, train_prefix, train_sha256, workspace)
    _require_bound(s3, train_prefix, train_sha256, run_id, jobs)

    unit_jobs: dict[str, list[str]] = {}
    for job in jobs:
        unit_jobs.setdefault(job["unit"], []).append(job["id"])
    bound: dict[str, list[dict[str, Any]]] = {}
    reused_jobs: list[str] = []
    matrices: dict[str, Any] = {}
    outcomes: list[tuple[str, str, str]] = []

    for job in jobs:
        upstream_jobs = set(job["needs"]) | {
            producer for unit in _closure(train, job["unit"]) for producer in unit_jobs.get(unit, [])
        }
        building = sorted(upstream for upstream in upstream_jobs if upstream not in reused_jobs)
        missed: list[dict[str, str]] = []
        if building:
            for row in job["matrix"]["include"]:
                missed.append(row)
                outcomes.append((job["id"], _label(row), f"builds: {', '.join(building)} builds in this run"))
        else:
            records = _catalog_records(s3, [ref for upstream in sorted(upstream_jobs) for ref in bound[upstream]])
            build = build_inputs(train, _source_inputs(train_path, job["unit"], workspace))
            bound[job["id"]] = []
            for row in job["matrix"]["include"]:
                computed = _row_key(job, row, train, build, records, image_map, workspace)
                if computed is None:
                    missed.append(row)
                    outcomes.append((job["id"], _label(row), "builds: no reuse key"))
                    continue
                key, key_record = computed
                print(f"{job['id']} {_label(row)}: reuse key {key}")
                args = argparse.Namespace(
                    key=key,
                    repository=job["repository"],
                    train_sha256=train_sha256,
                    run_id=run_id,
                    run_attempt=run_attempt,
                    train_prefix=train_prefix,
                    content_prefix=content_prefix,
                )
                chosen = find_reuse(s3, args, key_record)
                if chosen is None:
                    missed.append(row)
                    outcomes.append((job["id"], _label(row), "builds: no earlier complete build"))
                    continue
                bound[job["id"]].extend(bind_reuse(s3, args, chosen).values())
                outcomes.append((job["id"], _label(row), f"reused from run {chosen[0]}.{chosen[1]}"))
        if missed:
            matrices[job["id"]] = {"include": missed}
        else:
            reused_jobs.append(job["id"])
    return matrices, reused_jobs, outcomes


def _summary(outcomes: list[tuple[str, str, str]], reused_jobs: list[str]) -> str:
    lines = [
        "## Reuse plan",
        "",
        f"{len(reused_jobs)} jobs reused in full and skipped; their rows are listed here, not as jobs.",
        "",
        "| Job | Row | Outcome |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {job} | {row} | {outcome} |" for job, row, outcome in outcomes]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Plan the run and write its outputs and summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "image-map", "train-sha256", "run-id", "run-attempt", "bucket", "github-output"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--summary", required=True, help="File the summary table is appended to.")
    parser.add_argument("--train-prefix", default="train-state")
    parser.add_argument("--content-prefix", default="artifacts")
    args = parser.parse_args(argv)
    try:
        image_map = json.loads(args.image_map)
        if not isinstance(image_map, dict):
            raise PlanError("the image map must be a JSON object")
        with tempfile.TemporaryDirectory() as workspace:
            matrices, reused_jobs, outcomes = plan(
                S3(args.bucket),
                json.loads(Path(args.manifest).read_text()),
                train_sha256=args.train_sha256,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                image_map=image_map,
                workspace=Path(workspace),
                train_prefix=args.train_prefix,
                content_prefix=args.content_prefix,
            )
    except (PlanError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"reuse plan failed: {error}") from error
    with Path(args.github_output).open("a") as output:
        output.write(f"matrices={json.dumps(matrices, sort_keys=True, separators=(',', ':'))}\n")
        output.write(f"reused-jobs={json.dumps(reused_jobs, separators=(',', ':'))}\n")
    with Path(args.summary).open("a") as summary:
        summary.write(_summary(outcomes, reused_jobs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
