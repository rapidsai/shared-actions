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
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from row_key_inputs import build_inputs, upstream_document
from row_reuse import HEX64, S3, SAFE, _completed, bind_reuse, find_reuse
from upstream_selection import select_upstream

_DIRECTORY = Path(__file__).resolve().parent
_JOB = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_ENTRYPOINT = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SAFE_REPOSITORY = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_ROW_FIELDS = ("ARCH", "CUDA_VER", "PY_VER", "candidate_image_key")
# Each row waits mostly on S3 round trips through the AWS CLI, so rows of a wave run side by side.
_WORKERS = 8


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


def _row_key(
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


# What names a row, as opposed to what it was built from. The upload files every key record under this digest
# (upload-candidate-s3.sh runs exactly this jq filter), so a row that misses can be compared with its previous build.
ROW_DIGEST_JQ = "{producer: .producer.repository, row: .row, entrypoint: .build.entrypoint}"
_HISTORY_RUN = re.compile(r"([0-9]+)-([0-9]+)\.json\Z")
_TOOL_ORDER = ("gha-tools", "shared-actions", "shared-workflows")


def row_digest(record: dict[str, Any]) -> str:
    """sha256 of ROW_DIGEST_JQ applied to a key record, as jq -cSj prints it."""
    named = {
        "producer": (record.get("producer") or {}).get("repository"),
        "row": record.get("row"),
        "entrypoint": (record.get("build") or {}).get("entrypoint"),
    }
    return hashlib.sha256(
        json.dumps(named, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _previous_build(
    s3: S3, job: dict[str, Any], record: dict[str, Any], context: dict[str, str]
) -> tuple[dict[str, Any], str, str] | None:
    """The newest completed earlier build of this row, as (key record, run, attempt), or None."""
    repository = job["repository"].rsplit("/", 1)[-1]
    prefix = f"{context['content_prefix']}/{repository}/row-reuse-history/{row_digest(record)}/"
    runs: dict[tuple[int, int], str] = {}
    for key in s3.keys(prefix):
        parts = key.removeprefix(prefix).split("/")
        match = _HISTORY_RUN.fullmatch(parts[-1]) if len(parts) == 2 else None
        if match is not None and SAFE.fullmatch(parts[0]) and match[1] != context["run_id"]:
            runs.setdefault((int(match[1]), int(match[2])), key)
    for (run, attempt), key in sorted(runs.items(), reverse=True):
        data = s3.get(key)
        try:
            entry = json.loads(data) if data is not None else None
        except json.JSONDecodeError:
            continue
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("key_record"), dict)
            and HEX64.fullmatch(str(entry.get("train_sha256")))
            and _completed(s3, context["train_prefix"], entry["train_sha256"], str(run), str(attempt))
        ):
            return entry["key_record"], str(run), str(attempt)
    return None


def _short(value: Any) -> str:
    text = str(value)
    if "@sha256:" in text:
        return "sha256:" + text.split("@sha256:", 1)[1][:7]
    return text[:7]


def _upstream_by_repository(record: dict[str, Any]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for dependency in record.get("upstream") or []:
        repository = str((dependency.get("producer") or {}).get("repository") or "")
        grouped.setdefault(repository, []).append(json.dumps(dependency, sort_keys=True))
    return {repository: sorted(entries) for repository, entries in grouped.items()}


def _cause(current: dict[str, Any], previous: dict[str, Any]) -> str | None:
    """The first input that differs from the row's previous build, in the order a reader would look for it."""
    now, then = current.get("implementation_revisions") or {}, previous.get("implementation_revisions") or {}
    for tool in (*_TOOL_ORDER, *sorted(set(now) | set(then))):
        if now.get(tool) != then.get(tool):
            return f"tooling: {tool} {_short(then.get(tool))} → {_short(now.get(tool))}"
    if current.get("runtime_image") != previous.get("runtime_image"):
        return f"CI image: {_short(previous.get('runtime_image'))} → {_short(current.get('runtime_image'))}"
    now_upstream, then_upstream = _upstream_by_repository(current), _upstream_by_repository(previous)
    for repository in sorted(set(now_upstream) | set(then_upstream)):
        if now_upstream.get(repository) != then_upstream.get(repository):
            return f"upstream: {repository} changed"
    now_producer, then_producer = current.get("producer") or {}, previous.get("producer") or {}
    if now_producer.get("sha") != then_producer.get("sha"):
        before, after = _short(then_producer.get("sha")), _short(now_producer.get("sha"))
        return f"source: {now_producer.get('repository')} {before} → {after}"
    now_inputs, then_inputs = current.get("build_inputs") or {}, previous.get("build_inputs") or {}
    now_train, then_train = now_inputs.get("train_inputs") or {}, then_inputs.get("train_inputs") or {}
    for field, label in (("lock_receipt_sha256", "locks"), ("variant_receipt_sha256", "variants")):
        if now_train.get(field) != then_train.get(field):
            return f"{label} changed"
    for field, label in (
        ("build_policy", "build policy"),
        ("source_inputs", "build sources or tools"),
        ("build_implementation_repositories", "tooling repositories"),
    ):
        if now_inputs.get(field) != then_inputs.get(field):
            return f"{label} changed"
    now_build, then_build = current.get("build") or {}, previous.get("build") or {}
    if now_build.get("entrypoint") != then_build.get("entrypoint"):
        return "release entrypoint changed"
    now_identity, then_identity = now_build.get("identity") or {}, then_build.get("identity") or {}
    for name in sorted(set(now_identity) | set(then_identity)):
        if now_identity.get(name) != then_identity.get(name):
            return f"release-file settings: {name}"
    return None


def _label(row: dict[str, str]) -> str:
    return f"{row['CUDA_VER']}, {row['PY_VER']}, {row['ARCH']}"


def plan(
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
    upstream_of = {
        job["id"]: (
            set(job["needs"])
            | {producer for unit in _closure(train, job["unit"]) for producer in unit_jobs.get(unit, [])}
        )
        - {job["id"]}
        for job in jobs
    }
    order = {job["id"]: position for position, job in enumerate(jobs)}
    bound: dict[str, list[dict[str, Any]]] = {}
    reused: set[str] = set()
    root: dict[str, str] = {}  # job -> the first thing that forced it to build, named for jobs below it
    matrices: dict[str, Any] = {}
    outcomes: list[tuple[str, str, str]] = []
    context = {
        "train_sha256": train_sha256,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "train_prefix": train_prefix,
        "content_prefix": content_prefix,
    }

    decided: set[str] = set()
    pending = list(jobs)
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        while pending:
            # A wave is every job whose upstream jobs are decided; its jobs and rows are independent of each other.
            wave = [job for job in pending if upstream_of[job["id"]] <= decided]
            if not wave:
                raise PlanError("the manifest's jobs cannot be ordered")
            rows: dict[str, list[Any]] = {}
            for job in wave:
                building = sorted(
                    (upstream for upstream in upstream_of[job["id"]] if upstream not in reused), key=order.__getitem__
                )
                if building:
                    # Only the first thing that forced the build: the root cause of the first upstream job that builds.
                    rows[job["id"]] = [(row, None, root[building[0]]) for row in job["matrix"]["include"]]
                    continue
                upstream = sorted(upstream_of[job["id"]], key=order.__getitem__)
                records = _catalog_records(s3, [ref for name in upstream for ref in bound[name]])
                build = build_inputs(train, _source_inputs(train_path, job["unit"], workspace))
                rows[job["id"]] = [
                    (
                        row,
                        pool.submit(_decide_row, s3, job, row, train, build, records, image_map, workspace, context),
                        None,
                    )
                    for row in job["matrix"]["include"]
                ]
            for job in wave:
                missed = []
                bound[job["id"]] = []
                for row, future, inherited in rows[job["id"]]:
                    if future is None:
                        missed.append(row)
                        outcomes.append((job["id"], _label(row), f"builds: {inherited}"))
                        root.setdefault(job["id"], inherited)
                        continue
                    references, outcome = future.result()
                    if references is None:
                        missed.append(row)
                        outcomes.append((job["id"], _label(row), f"builds: {outcome}"))
                        root.setdefault(job["id"], f"{job['id']} {_label(row)} builds ({outcome})")
                    else:
                        bound[job["id"]].extend(references)
                        outcomes.append((job["id"], _label(row), outcome))
                if missed:
                    matrices[job["id"]] = {"include": missed}
                else:
                    reused.add(job["id"])
            decided |= {job["id"] for job in wave}
            pending = [job for job in pending if job["id"] not in decided]
    return matrices, sorted(reused, key=order.__getitem__), outcomes


def _decide_row(
    s3: S3,
    job: dict[str, Any],
    row: dict[str, str],
    train: dict[str, Any],
    build: dict[str, Any],
    records: list[dict[str, Any]],
    image_map: dict[str, str],
    workspace: Path,
    context: dict[str, str],
) -> tuple[list[dict[str, Any]] | None, str]:
    """Compute one row's key, look it up and bind a hit; return (bound references, outcome) or (None, its cause)."""
    started = time.monotonic()
    computed = _row_key(job, row, train, build, records, image_map, workspace)
    if computed is None:
        return None, "no reuse key"
    key, key_record = computed
    args = argparse.Namespace(key=key, repository=job["repository"], **context)
    chosen = find_reuse(s3, args, key_record)
    elapsed = time.monotonic() - started
    print(f"{job['id']} {_label(row)}: reuse key {key} ({elapsed:.1f} s)", flush=True)
    if chosen is None:
        previous = _previous_build(s3, job, key_record, context)
        if previous is None:
            return None, "no earlier build of this row"
        record, run, attempt = previous
        return None, _cause(key_record, record) or f"the earlier build in run {run}.{attempt} could not be reused"
    references = list(bind_reuse(s3, args, chosen).values())
    return references, f"reused from run {chosen[0]}.{chosen[1]}"


_RUN_REFERENCE = re.compile(r"\brun ([0-9]+)\.([0-9]+)\b")
_SERVER = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]+)?\Z")


def _linked(outcome: str, runs_url: str) -> str:
    """Make each "run <id>.<attempt>" a link to that run attempt."""
    return _RUN_REFERENCE.sub(lambda match: f"[{match[0]}]({runs_url}/{match[1]}/attempts/{match[2]})", outcome)


def _summary(outcomes: list[tuple[str, str, str]], reused_jobs: list[str], runs_url: str) -> str:
    lines = [
        "## Reuse plan",
        "",
        f"{len(reused_jobs)} jobs reused in full and skipped; their rows are listed here, not as jobs.",
        "",
        "| Job | Row | Outcome |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {job} | {row} | {_linked(outcome, runs_url)} |" for job, row, outcome in outcomes]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Plan the run and write its outputs and summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "image-map", "train-sha256", "run-id", "run-attempt", "bucket", "github-output"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--summary", required=True, help="File the summary table is appended to.")
    parser.add_argument("--server-url", required=True, help="GITHUB_SERVER_URL, for links to earlier runs.")
    parser.add_argument("--repository", required=True, help="GITHUB_REPOSITORY, whose runs the plan reuses.")
    parser.add_argument("--train-prefix", default="train-state")
    parser.add_argument("--content-prefix", default="artifacts")
    args = parser.parse_args(argv)
    if _SERVER.fullmatch(args.server_url) is None or SAFE_REPOSITORY.fullmatch(args.repository) is None:
        raise SystemExit("reuse plan failed: --server-url and --repository must name a GitHub server and owner/name")
    runs_url = f"{args.server_url}/{args.repository}/actions/runs"
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
        summary.write(_summary(outcomes, reused_jobs, runs_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
