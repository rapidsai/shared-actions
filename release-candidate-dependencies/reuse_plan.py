#!/usr/bin/env python3
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Decide every build row of a release run before any build job starts (plans/per-row-reuse.md, 4a and 4b).

The generator writes a manifest of the release file's build jobs: each job's repository, unit, family, release
entrypoint, static identity (candidate-row-identity), the jobs it needs and its matrix.

CI image rows (family ``container``) are the first wave. Each is keyed from its ci-imgs commit, matrix row, tooling
and base images by digest (row_key_inputs.py's image inputs), looked up and bound like a package row. A reused image
goes into the image map straight from its earlier run's image record; a train asking for an image refresh rebuilds
every image. A package row whose image builds in this run cannot be keyed yet: it builds, naming that image.

Package jobs are then decided in dependency order. A job is eligible only when every job it needs, and every job
producing a unit its unit consumes, was reused in full: then every input of its rows is known, the upstream ones being
the references just bound. Each eligible row's key is computed with the same modules dependency setup uses
(upstream_selection.py, row_key_inputs.py, row_reuse_key.py), looked up and verified as the in-row check does
(row_reuse.py), and bound on a hit. Any other row is left to build; its in-row check can still reuse it once its
upstream finished.

Writes to --github-output:
- ``matrices``: JSON object, job id -> the matrix of its rows that build (only jobs with such rows);
- ``reused-jobs``: JSON list of the jobs whose every row was reused, which the release file skips;
- ``image-map``: JSON object, the given image map plus every image this plan reused (image key -> GHCR digest).
Writes a table of every row and its outcome to --summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404 - jq, bash and this directory's scripts with fixed arguments
import sys
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from row_key_inputs import build_inputs, image_inputs, image_key, image_refresh, upstream_document
from row_reuse import HEX64, S3, SAFE, Miss, _canonical, _completed, _json, _verify, bind_reuse, find_reuse
from row_reuse_key import DEVELOPMENT_TOOLING
from s3_boto import Boto3S3
from upstream_selection import select_upstream

_DIRECTORY = Path(__file__).resolve().parent
_JOB = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_ENTRYPOINT = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SAFE_REPOSITORY = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_ROW_FIELDS = ("ARCH", "CUDA_VER", "PY_VER", "candidate_image_key")
_IMAGE_ROW_FIELDS = ("ARCH", "CUDA_VER", "PYTHON_VER", "IMAGE_REPO", "IMAGE_NAME")
_CANDIDATE_IMAGE = re.compile(r"ghcr\.io/rapidsai/release-candidates/ci-(conda|wheel)@sha256:[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


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
        if job.get("family") not in {"conda", "wheel", "container"}:
            raise PlanError(f"{job_id}: the reuse plan decides conda, wheel and CI image rows only")
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
        fields = _IMAGE_ROW_FIELDS if job["family"] == "container" else _ROW_FIELDS
        for row in rows:
            if not isinstance(row, dict) or not all(isinstance(row.get(field), str) for field in fields):
                raise PlanError(f"{job_id}: every row needs {', '.join(fields)}")
        if job["family"] == "container":
            if job["needs"]:
                raise PlanError(f"{job_id}: CI image rows are the first wave and need no job")
            if not all(isinstance(value, (str, bool, int, float)) for row in rows for value in row.values()):
                raise PlanError(f"{job_id}: image rows hold only scalar values")
            try:
                keys = [image_key(row) for row in rows]
            except ValueError as error:
                raise PlanError(f"{job_id}: {error}") from error
            if len(set(keys)) != len(keys):
                raise PlanError(f"{job_id}: image rows repeat an image")
    images = {job["id"] for job in jobs if job["family"] == "container"}
    for job in jobs:
        unknown = [need for need in job["needs"] if need not in ids]
        if unknown:
            raise PlanError(f"{job['id']}: needs unknown jobs {unknown}")
        if images & set(job["needs"]):
            raise PlanError(f"{job['id']}: a package job reaches its image through candidate_image_key, not needs")
    keys = [image_key(row) for job in jobs if job["id"] in images for row in job["matrix"]["include"]]
    if len(set(keys)) != len(keys):
        raise PlanError("two image jobs build the same image")
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


def _workers() -> int:
    """Two rows per core: a row's AWS CLI calls are mostly process start-up, so rows compete for cores."""
    return 2 * (os.cpu_count() or 1)


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


def _image_row_key(
    job: dict[str, Any], row: dict[str, str], train: dict[str, Any], workspace: Path
) -> tuple[str, Any] | None:
    """A CI image row's reuse key and key record, computed as setup computes them, or None when it has none."""
    try:
        inputs = image_inputs(train, row)
    except ValueError as error:
        raise PlanError(f"{job['id']}: {error}") from error
    if inputs is None:
        return None
    directory = Path(tempfile.mkdtemp(dir=workspace))
    (directory / "image-inputs.json").write_text(json.dumps(inputs))
    (directory / "upstream-inputs.json").write_text(json.dumps(upstream_document([])))
    producer_sha = (train.get("source_revisions") or {}).get(job["project"])
    if not isinstance(producer_sha, str) or _SHA.fullmatch(producer_sha) is None:
        raise PlanError(f"{job['id']}: the train has no source revision for {job['project']}")
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
            row["PYTHON_VER"],
            "--implementation-revisions",
            json.dumps(train.get("build_implementation_revisions"), sort_keys=True, separators=(",", ":")),
            "--row-entrypoint",
            job["entrypoint"],
            "--row-identity",
            json.dumps(job["identity"], sort_keys=True),
            "--image-row",
            json.dumps(row, sort_keys=True),
            "--build-inputs",
            str(directory / "image-inputs.json"),
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
        raise PlanError(f"{job['id']}: cannot compute an image reuse key: {result.stderr.strip()}")
    key = result.stdout.strip()
    if not key:
        return None
    record_bytes = (directory / "row-reuse-key.json").read_bytes()
    if not HEX64.fullmatch(key) or hashlib.sha256(record_bytes).hexdigest() != key:
        raise PlanError(f"{job['id']}: the image reuse key does not match its key record")
    return key, json.loads(record_bytes)


def _reused_image(s3: S3, row: dict[str, str], references: dict[str, dict[str, Any]]) -> str:
    """The digest-pinned GHCR image an earlier run's image row recorded, read from its verified artifact index."""
    label = image_key(row)
    if len(references) != 1:
        raise PlanError(f"{label}: an image row indexes exactly one artifact, not {len(references)}")
    reference = next(iter(references.values()))
    index = json.loads(s3.get(reference["artifact_index_key"]) or b"null")
    entries = [
        entry
        for entry in (index.get("entries") if isinstance(index, dict) else None) or []
        if isinstance(entry, dict) and str(entry.get("path", "")).endswith(".oci-image.json")
    ]
    if len(entries) != 1:
        raise PlanError(f"{label}: the earlier run's artifact index names {len(entries)} image records, not one")
    data = s3.get(f"{reference['artifact_key']}/{entries[0]['path']}")
    if data is None or hashlib.sha256(data).hexdigest() != entries[0].get("sha256"):
        raise PlanError(f"{label}: the earlier run's image record is missing or changed")
    record = json.loads(data)
    repository = f"rapidsai/release-candidates/{row['IMAGE_REPO']}"
    if (
        not isinstance(record, dict)
        or record.get("registry") != "ghcr.io"
        or record.get("repository") != repository
        or record.get("platform") != f"linux/{row['ARCH']}"
        or not _DIGEST.fullmatch(str(record.get("digest")))
    ):
        raise PlanError(f"{label}: the earlier run's image record is not a {repository} linux/{row['ARCH']} image")
    image = f"ghcr.io/{repository}@{record['digest']}"
    if _CANDIDATE_IMAGE.fullmatch(image) is None:
        raise PlanError(f"{label}: {image} is not a digest-pinned candidate image")
    return image


def _decide_image_row(
    s3: S3, job: dict[str, Any], row: dict[str, str], train: dict[str, Any], workspace: Path, context: dict[str, str]
) -> tuple[str | None, str, dict[str, Any]]:
    """Key, look up and bind one image row; return (its GHCR image, outcome, hit) or (None, why it builds, {})."""
    if image_refresh(train):
        return None, "image refresh requested (candidate_image_policy.mode: refresh)", {}
    computed = _image_row_key(job, row, train, workspace)
    if computed is None:
        return None, "no reuse key: the train records no base image digests", {}
    key, key_record = computed
    args = argparse.Namespace(key=key, repository=job["repository"], **context)
    chosen = find_reuse(s3, args, key_record)
    print(f"{job['id']} {_label(row)}: reuse key {key}", flush=True)
    if chosen is None:
        previous = _previous_build(s3, job, key_record, context)
        if previous is None:
            return None, "no earlier build of this row", {}
        record, run, attempt = previous
        return None, _cause(key_record, record) or f"the earlier build in run {run}.{attempt} could not be reused", {}
    # Read the image before binding, so a bad record stops the plan without binding anything.
    image = _reused_image(s3, row, chosen[2])
    bind_reuse(s3, args, chosen)
    return image, f"reused from run {chosen[0]}.{chosen[1]}", _hit(key, key_record, chosen, image=image)


def _hit(
    key: str, key_record: Any, chosen: tuple[str, str, dict[str, Any]], *, image: str | None = None
) -> dict[str, Any]:
    """What a train-time plan records of a reused row, for the release run to re-check and bind."""
    hit = {"key": key, "key_record": key_record, "run": chosen[0], "attempt": chosen[1], "references": chosen[2]}
    if image is not None:
        hit["image"] = image
    return hit


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


def _cause(current: dict[str, Any], previous: dict[str, Any], upstream: str | None = None) -> str | None:
    """The first input that differs from the row's previous build, in the order a reader would look for it.

    ``upstream`` names an upstream row that builds in this run: the row's upstream packages aren't known yet, so that
    row is the upstream cause, after tooling and the CI image.
    """
    now, then = current.get("implementation_revisions") or {}, previous.get("implementation_revisions") or {}
    if isinstance(now, str) != isinstance(then, str):
        return f"tooling: development keys switched {'on' if isinstance(now, str) else 'off'}"
    if isinstance(now, str):
        now, then = {}, {}
    for tool in (*_TOOL_ORDER, *sorted(set(now) | set(then))):
        if now.get(tool) != then.get(tool):
            return f"tooling: {tool} {_short(then.get(tool))} → {_short(now.get(tool))}"
    if current.get("runtime_image") != previous.get("runtime_image"):
        return f"CI image: {_short(previous.get('runtime_image'))} → {_short(current.get('runtime_image'))}"
    now_bases = (current.get("build_inputs") or {}).get("base_images") or {}
    then_bases = (previous.get("build_inputs") or {}).get("base_images") or {}
    for argument in sorted(set(now_bases) | set(then_bases)):
        now_base, then_base = now_bases.get(argument), then_bases.get(argument)
        if now_base != then_base:
            now_tag, then_tag = str(now_base).split("@", 1)[0], str(then_base).split("@", 1)[0]
            if now_tag != then_tag:
                return f"base image: {argument} {then_tag} → {now_tag}"
            return f"base image: {argument} {_short(then_base)} → {_short(now_base)}"
    if upstream is not None:
        return upstream
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
        ("refresh_key", "image refresh key"),
    ):
        if now_inputs.get(field) != then_inputs.get(field):
            return f"{label} changed"
    now_row, then_row = current.get("image_row") or {}, previous.get("image_row") or {}
    for name in sorted(set(now_row) | set(then_row)):
        if now_row.get(name) != then_row.get(name):
            return f"ci-imgs matrix row: {name}"
    now_build, then_build = current.get("build") or {}, previous.get("build") or {}
    if now_build.get("entrypoint") != then_build.get("entrypoint"):
        return "release entrypoint changed"
    now_identity, then_identity = now_build.get("identity") or {}, then_build.get("identity") or {}
    for name in sorted(set(now_identity) | set(then_identity)):
        if now_identity.get(name) != then_identity.get(name):
            return f"release-file settings: {name}"
    if now_inputs != then_inputs:
        return "build inputs changed"
    return None


def _label(row: dict[str, str]) -> str:
    if "IMAGE_REPO" in row:
        return f"{row['IMAGE_REPO']} {row['CUDA_VER']}, {row['PYTHON_VER']}, {row['ARCH']}"
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
    decisions: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str], list[tuple[str, str, str]], dict[str, str]]:
    """Decide every row.

    Returns (matrices of rows that build, fully reused job ids, (job, row, outcome) lines, the image map: the given
    one plus every image this plan reused). Each row's decision is appended to ``decisions`` when given: job, row,
    wave (images 0, then each package wave), outcome and, for a hit, what ``_hit`` records.

    A ``PlanningStore`` plans a train before it has a run: nothing is written, and no repository is bound yet.
    """
    if not HEX64.fullmatch(train_sha256) or not run_id.isdigit() or not run_attempt.isdigit():
        raise PlanError("invalid train digest, run id or run attempt")
    every_job = _validate_manifest(manifest)
    train, train_path = _load_train(s3, train_prefix, train_sha256, workspace)
    if not isinstance(s3, PlanningStore):
        _require_bound(s3, train_prefix, train_sha256, run_id, every_job)
    recorded: list[dict[str, Any]] = decisions if decisions is not None else []
    image_jobs = [job for job in every_job if job["family"] == "container"]
    jobs = [job for job in every_job if job["family"] != "container"]

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
    missed_rows: dict[str, list[dict[str, str]]] = {}  # job -> its rows that build, for the jobs below it to name
    matrices: dict[str, Any] = {}
    outcomes: list[tuple[str, str, str]] = []
    context = {
        "train_sha256": train_sha256,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "train_prefix": train_prefix,
        "content_prefix": content_prefix,
    }

    image_map = dict(image_map)
    building_images: set[str] = set()
    decided: set[str] = set()
    pending = list(jobs)
    wave_number = 0
    with ThreadPoolExecutor(max_workers=_workers()) as pool:
        # The first wave: every CI image row. A package row's key holds its image's digest, so images come first.
        image_rows = {
            job["id"]: [
                (row, pool.submit(_decide_image_row, s3, job, row, train, workspace, context))
                for row in job["matrix"]["include"]
            ]
            for job in image_jobs
        }
        for job in image_jobs:
            missed = []
            for row, future in image_rows[job["id"]]:
                image, outcome, hit = future.result()
                key = image_key(row)
                if image is None:
                    missed.append(row)
                    building_images.add(key)
                    outcomes.append((job["id"], _label(row), f"builds: {outcome}"))
                    recorded.append(_decision(job, row, 0, f"builds: {outcome}"))
                    continue
                if image_map.setdefault(key, image) != image:
                    raise PlanError(f"{key}: the given image map names {image_map[key]}, not the reused {image}")
                outcomes.append((job["id"], _label(row), outcome))
                recorded.append(_decision(job, row, 0, outcome, hit))
            if missed:
                matrices[job["id"]] = {"include": missed}
            else:
                reused.add(job["id"])
        while pending:
            # A wave is every job whose upstream jobs are decided; its jobs and rows are independent of each other.
            wave = [job for job in pending if upstream_of[job["id"]] <= decided]
            if not wave:
                raise PlanError("the manifest's jobs cannot be ordered")
            wave_number += 1
            rows: dict[str, list[Any]] = {}
            for job in wave:
                building = sorted(
                    (upstream for upstream in upstream_of[job["id"]] if upstream not in reused), key=order.__getitem__
                )
                build = build_inputs(train, _source_inputs(train_path, job["unit"], workspace))
                waiting = [row for row in job["matrix"]["include"] if row["candidate_image_key"] in building_images]
                if waiting:
                    # Its image has no digest until it is built: the row can't be keyed, so its image is its cause.
                    rows[job["id"]] = [
                        (row, _done(f"CI image: {row['candidate_image_key']} builds"), True) for row in waiting
                    ]
                rest = [row for row in job["matrix"]["include"] if row not in waiting]
                if building:
                    # The row can't be looked up, but its own previous build says whether something of its own changed;
                    # failing that, the cause is the matching row of the first upstream job that builds.
                    rows[job["id"]] = rows.get(job["id"], []) + [
                        (
                            row,
                            pool.submit(
                                _cascaded_row,
                                s3,
                                job,
                                row,
                                train,
                                build,
                                image_map,
                                workspace,
                                context,
                                _upstream_text(building[0], missed_rows[building[0]], row),
                            ),
                            True,
                        )
                        for row in rest
                    ]
                    continue
                upstream = sorted(upstream_of[job["id"]], key=order.__getitem__)
                records = _catalog_records(s3, [ref for name in upstream for ref in bound[name]])
                rows[job["id"]] = rows.get(job["id"], []) + [
                    (
                        row,
                        pool.submit(_decide_row, s3, job, row, train, build, records, image_map, workspace, context),
                        False,
                    )
                    for row in rest
                ]
            for job in wave:
                missed = []
                bound[job["id"]] = []
                position = {id(row): index for index, row in enumerate(job["matrix"]["include"])}
                for row, future, cascaded in sorted(rows[job["id"]], key=lambda item: position[id(item[0])]):
                    if cascaded:
                        missed.append(row)
                        outcomes.append((job["id"], _label(row), f"builds: {future.result()}"))
                        recorded.append(_decision(job, row, wave_number, f"builds: {future.result()}"))
                        continue
                    references, outcome, hit = future.result()
                    if references is None:
                        missed.append(row)
                        outcomes.append((job["id"], _label(row), f"builds: {outcome}"))
                        recorded.append(_decision(job, row, wave_number, f"builds: {outcome}"))
                    else:
                        bound[job["id"]].extend(references)
                        outcomes.append((job["id"], _label(row), outcome))
                        recorded.append(_decision(job, row, wave_number, outcome, hit))
                missed_rows[job["id"]] = missed
                if missed:
                    matrices[job["id"]] = {"include": missed}
                else:
                    reused.add(job["id"])
            decided |= {job["id"] for job in wave}
            pending = [job for job in pending if job["id"] not in decided]
    image_order = {job["id"]: position for position, job in enumerate(image_jobs)}
    reused_jobs = sorted(reused & set(image_order), key=image_order.__getitem__) + sorted(
        reused - set(image_order), key=order.__getitem__
    )
    return matrices, reused_jobs, outcomes, image_map


def _decision(
    job: dict[str, Any], row: dict[str, str], wave: int, outcome: str, hit: dict[str, Any] | None = None
) -> dict[str, Any]:
    decision = {"job": job["id"], "row": row, "wave": wave, "outcome": outcome}
    if hit:
        decision["hit"] = hit
    return decision


def _done(value: str) -> Future:
    """An already-decided outcome, in the same form as the rows the pool decides."""
    future: Future = Future()
    future.set_result(value)
    return future


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
) -> tuple[list[dict[str, Any]] | None, str, dict[str, Any]]:
    """Compute one row's key, look it up and bind a hit; return (references, outcome, hit) or (None, its cause, {})."""
    started = time.monotonic()
    computed = _row_key(job, row, train, build, records, image_map, workspace)
    if computed is None:
        return None, "no reuse key", {}
    key, key_record = computed
    args = argparse.Namespace(key=key, repository=job["repository"], **context)
    chosen = find_reuse(s3, args, key_record)
    elapsed = time.monotonic() - started
    print(f"{job['id']} {_label(row)}: reuse key {key} ({elapsed:.1f} s)", flush=True)
    if chosen is None:
        previous = _previous_build(s3, job, key_record, context)
        if previous is None:
            return None, "no earlier build of this row", {}
        record, run, attempt = previous
        return None, _cause(key_record, record) or f"the earlier build in run {run}.{attempt} could not be reused", {}
    references = list(bind_reuse(s3, args, chosen).values())
    return references, f"reused from run {chosen[0]}.{chosen[1]}", _hit(key, key_record, chosen)


_RUN_REFERENCE = re.compile(r"\brun ([0-9]+)\.([0-9]+)\b")
_SERVER = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]+)?\Z")


def _linked(outcome: str, runs_url: str) -> str:
    """Make each "run <id>.<attempt>" a link to that run attempt."""
    return _RUN_REFERENCE.sub(lambda match: f"[{match[0]}]({runs_url}/{match[1]}/attempts/{match[2]})", outcome)


def _upstream_text(job_id: str, missed: list[dict[str, str]], row: dict[str, str]) -> str:
    """Name the upstream job's building row that matches this row: same arch and CUDA, else same arch, else its first.

    A CUDA-agnostic upstream (rapids-logger builds only CUDA 13.4.3 and serves both majors) matches by arch alone.
    """
    match = (
        next((other for other in missed if (other["ARCH"], other["CUDA_VER"]) == (row["ARCH"], row["CUDA_VER"])), None)
        or next((other for other in missed if other["ARCH"] == row["ARCH"]), None)
        or missed[0]
    )
    return f"upstream: {job_id} {_label(match)} builds"


def _cascaded_row(
    s3: S3,
    job: dict[str, Any],
    row: dict[str, str],
    train: dict[str, Any],
    build: dict[str, Any],
    image_map: dict[str, str],
    workspace: Path,
    context: dict[str, str],
    upstream: str,
) -> str:
    """Why a row whose upstream builds in this run builds: a change of its own if any, else that upstream row."""
    computed = _row_key(job, row, train, build, [], image_map, workspace)
    if computed is None:
        return "no reuse key"
    previous = _previous_build(s3, job, computed[1], context)
    if previous is None:
        return "no earlier build of this row"
    return _cause(computed[1], previous[0], upstream) or upstream


def tooling_in_keys(train: dict[str, Any]) -> bool:
    """False when the train keys rows without the tooling commits (a per-release development setting)."""
    return (train.get("candidate_build_policy") or {}).get("tooling_in_reuse_keys", True) is not False


def _summary(
    outcomes: list[tuple[str, str, str]],
    reused_jobs: list[str],
    runs_url: str,
    *,
    development: bool = False,
    note: str = "",
) -> str:
    notice = (
        [
            "**Development: the tooling commits are not in this train's reuse keys**"
            " (`candidate_build_policy.tooling_in_reuse_keys: false`). Not for a release.",
            "",
        ]
        if development
        else []
    )
    lines = [
        *notice,
        "## Reuse plan",
        "",
        f"{len(reused_jobs)} jobs reused in full and skipped; their rows are listed here, not as jobs.",
        "",
        *([note, ""] if note else []),
        "| Job | Row | Outcome |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {job} | {row} | {_linked(outcome, runs_url)} |" for job, row, outcome in outcomes]
    return "\n".join(lines) + "\n"


# plans/per-row-reuse.md, 4c: Create/Update Train plans the train's rows (``train`` mode) and records the plan beside
# the train; the release run re-checks and binds the recorded hits (``apply`` mode) instead of computing keys.

_PLANNING_RUN = ("0", "0")  # A train being planned has no run; no real run has id 0, so nothing of a run matches it.


class PlanningStore:
    """The candidate store as a train-time plan sees it: reads go to the store, writes stay in memory.

    Deciding a row binds its hit for the run, and a later wave checks its same-run inputs against those bindings
    (row_reuse._current_digest), so planning keeps them here, where later waves read them, and writes nothing.
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        self.bucket = store.bucket
        self.completed = store.completed
        self.completed_lock = store.completed_lock
        self._held: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def keys(self, prefix: str) -> list[str]:
        with self._lock:
            held = [key for key in self._held if key.startswith(prefix)]
        return sorted(set(self.store.keys(prefix)) | set(held))

    def head(self, key: str) -> dict[str, Any] | None:
        # Only stored objects are checked by checksum; what planning holds are bindings and references it wrote.
        return self.store.head(key)

    def get(self, key: str) -> bytes | None:
        with self._lock:
            if key in self._held:
                return self._held[key]
        return self.store.get(key)

    def put_once(self, key: str, data: bytes) -> None:
        with self._lock:
            if self._held.setdefault(key, data) != data:
                raise PlanError(f"planning wrote {key} twice with different content")


def manifest_digest(manifest: Any) -> str:
    """sha256 of the manifest's canonical JSON: the release file a plan was made for."""
    return hashlib.sha256(_canonical(manifest)).hexdigest()


def plan_train(
    store: Any,
    manifest: Any,
    *,
    train_sha256: str,
    planning_run_id: str,
    planning_run_attempt: str,
    workspace: Path,
    image_map: dict[str, str] | None = None,
    train_prefix: str = "train-state",
    content_prefix: str = "artifacts",
) -> tuple[dict[str, Any], list[tuple[str, str, str]], list[str]]:
    """Plan every row of a train before its release run, and record the plan beside the train.

    Returns (the recorded plan, (job, row, outcome) lines, fully reused jobs). The plan is written once, under the
    planning run, to ``<train prefix>/<train>/reuse-plans/<run>-<attempt>.json``; the release run takes the newest.
    """
    if not planning_run_id.isdigit() or not planning_run_attempt.isdigit():
        raise PlanError("invalid planning run id or attempt")
    decisions: list[dict[str, Any]] = []
    _, reused_jobs, outcomes, _ = plan(
        PlanningStore(store),
        manifest,
        train_sha256=train_sha256,
        run_id=_PLANNING_RUN[0],
        run_attempt=_PLANNING_RUN[1],
        image_map=image_map or {},
        workspace=workspace,
        train_prefix=train_prefix,
        content_prefix=content_prefix,
        decisions=decisions,
    )
    record = {
        "schema_version": 1,
        "train_sha256": train_sha256,
        "manifest_sha256": manifest_digest(manifest),
        "planning_run": {"run_id": planning_run_id, "run_attempt": planning_run_attempt},
        "rows": decisions,
    }
    store.put_once(
        f"{train_prefix}/{train_sha256}/reuse-plans/{planning_run_id}-{planning_run_attempt}.json", _canonical(record)
    )
    return record, outcomes, reused_jobs


def _latest_plan(store: Any, train_prefix: str, train_sha256: str) -> dict[str, Any]:
    prefix = f"{train_prefix}/{train_sha256}/reuse-plans/"
    plans = {}
    for key in store.keys(prefix):
        match = _HISTORY_RUN.fullmatch(key.removeprefix(prefix))
        if match is not None:
            plans[(int(match[1]), int(match[2]))] = key
    if not plans:
        raise PlanError(
            "this train has no reuse plan; run Create/Update Train, which plans the train's rows before its release run"
        )
    data = store.get(plans[max(plans)])
    try:
        record = json.loads(data or b"null")
    except json.JSONDecodeError as error:
        raise PlanError(f"the reuse plan {plans[max(plans)]} is not JSON") from error
    if not isinstance(record, dict) or record.get("schema_version") != 1 or not isinstance(record.get("rows"), list):
        raise PlanError(f"the reuse plan {plans[max(plans)]} is not a schema_version 1 plan")
    return record


def _expected_key_record(job: dict[str, Any], row: dict[str, str], train: dict[str, Any]) -> dict[str, Any]:
    """The parts of a row's key record that the train and the release file fix; a recorded hit must have them."""
    producer_sha = (train.get("source_revisions") or {}).get(job["project"])
    expected: dict[str, Any] = {
        "producer": {"repository": job["repository"], "sha": producer_sha},
        "build": {"entrypoint": job["entrypoint"], "identity": job["identity"]},
        "implementation_revisions": (
            train.get("build_implementation_revisions") if tooling_in_keys(train) else DEVELOPMENT_TOOLING
        ),
    }
    if job["family"] == "container":
        expected["row"] = {
            "unit": job["unit"],
            "family": "container",
            "arch": row["ARCH"],
            "cuda_version": row["CUDA_VER"],
            "python_version": row["PYTHON_VER"],
        }
        expected["image_row"] = row
    else:
        expected["row"] = {
            "unit": job["unit"],
            "family": job["family"],
            "arch": row["ARCH"],
            "cuda_version": row["CUDA_VER"],
            "python_version": row["PY_VER"],
        }
    return expected


def _recheck(
    store: Any,
    job: dict[str, Any],
    decision: dict[str, Any],
    train: dict[str, Any],
    image_map: dict[str, str],
    context: dict[str, str],
) -> str | None:
    """Re-check one recorded hit as find_reuse would, then bind it; return its image for an image row.

    The key isn't computed again: the plan recorded it with its key record, which must still name this train's
    producer commit, tooling, the release file's build of this row and, for a package row, the image this run uses.
    Every check against the store (completion record, unchanged checksums and object versions, same-run inputs
    against this run's bindings) runs again. A hit that no longer holds stops the run: nothing is rebuilt on a stale
    plan, and Create/Update Train plans again.
    """
    row, hit = decision["row"], decision.get("hit")
    label = f"{job['id']} {_label(row)}"
    if (
        not isinstance(hit, dict)
        or not HEX64.fullmatch(str(hit.get("key")))
        or not str(hit.get("run")).isdigit()
        or not str(hit.get("attempt")).isdigit()
        or not isinstance(hit.get("key_record"), dict)
        or not isinstance(hit.get("references"), dict)
        or not hit["references"]
        or not all(SAFE.fullmatch(str(artifact)) for artifact in hit["references"])
    ):
        raise PlanError(f"{label}: the reuse plan's hit is malformed")
    record = hit["key_record"]
    for field, value in _expected_key_record(job, row, train).items():
        # The row's own fields must match; an image row's key record also names the image (row.image).
        actual = record.get(field)
        if field == "row" and isinstance(actual, dict):
            actual = {name: actual.get(name) for name in value}
        if actual != value:
            raise PlanError(f"{label}: the planned key record's {field} is not this train's and release file's")
    if job["family"] != "container" and record.get("runtime_image") != image_map.get(row["candidate_image_key"]):
        raise PlanError(f"{label}: the planned key record names another CI image than this run's")
    run, attempt, references = str(hit["run"]), str(hit["attempt"]), hit["references"]
    repository_name = job["repository"].rsplit("/", 1)[-1]
    prefix = f"{context['content_prefix']}/{repository_name}/row-reuse-index/{hit['key']}/"
    indexed = {
        key.removeprefix(prefix).split("/")[0]
        for key in store.keys(prefix)
        if key.endswith(f"/{run}-{attempt}.json") and key.removeprefix(prefix).count("/") == 1
    }
    stale = f"{label}: the planned reuse of run {run}.{attempt} no longer holds"
    advice = "Run Create/Update Train to plan the train again."
    if indexed != set(references):
        raise PlanError(f"{stale}: it indexed {sorted(indexed)}, the plan {sorted(references)}. {advice}")
    args = argparse.Namespace(key=hit["key"], repository=job["repository"], **context)
    try:
        reused = {
            artifact: _verify(
                store, args, record, artifact, _json(store, f"{prefix}{artifact}/{run}-{attempt}.json"), run, attempt
            )
            for artifact in sorted(references)
        }
    except Miss as miss:
        raise PlanError(f"{stale}: {miss}. {advice}") from miss
    if reused != references:
        raise PlanError(f"{stale}: its objects changed since the plan. {advice}")
    image = None
    if job["family"] == "container":
        image = _reused_image(store, row, reused)
        if image != hit.get("image"):
            raise PlanError(f"{stale}: its image record names {image}, the plan {hit.get('image')}. {advice}")
    bind_reuse(store, args, (run, attempt, reused))
    return image


def apply_plan(
    store: Any,
    manifest: Any,
    *,
    train_sha256: str,
    run_id: str,
    run_attempt: str,
    workspace: Path,
    image_map: dict[str, str] | None = None,
    train_prefix: str = "train-state",
    content_prefix: str = "artifacts",
) -> tuple[dict[str, Any], list[str], list[tuple[str, str, str]], dict[str, str], dict[str, str]]:
    """Re-check and bind the train's newest reuse plan in this release run, wave by wave.

    ``image_map`` names images the manifest doesn't build, as for ``plan``; it must be the one the train was planned
    with. Returns what ``plan`` returns, and the planning run.
    """
    if not HEX64.fullmatch(train_sha256) or not run_id.isdigit() or not run_attempt.isdigit():
        raise PlanError("invalid train digest, run id or run attempt")
    every_job = _validate_manifest(manifest)
    train, _ = _load_train(store, train_prefix, train_sha256, workspace)
    _require_bound(store, train_prefix, train_sha256, run_id, every_job)
    recorded = _latest_plan(store, train_prefix, train_sha256)
    if recorded.get("train_sha256") != train_sha256:
        raise PlanError("the reuse plan was made for another train")
    if recorded.get("manifest_sha256") != manifest_digest(manifest):
        raise PlanError("the reuse plan was made for another release file; run Create/Update Train to plan this one")
    planning_run = recorded.get("planning_run")
    if not isinstance(planning_run, dict) or not all(
        str(planning_run.get(field, "")).isdigit() for field in ("run_id", "run_attempt")
    ):
        raise PlanError("the reuse plan names no planning run")
    jobs = {job["id"]: job for job in every_job}
    decided: dict[tuple[str, str], dict[str, Any]] = {}
    for decision in recorded["rows"]:
        job = jobs.get(decision.get("job")) if isinstance(decision, dict) else None
        if job is None or decision.get("row") not in job["matrix"]["include"]:
            raise PlanError("the reuse plan decides a row the release file doesn't have")
        if not isinstance(decision.get("wave"), int) or not isinstance(decision.get("outcome"), str):
            raise PlanError(f"{job['id']}: the reuse plan's decision has no wave or outcome")
        identity = (job["id"], json.dumps(decision["row"], sort_keys=True))
        if identity in decided:
            raise PlanError(f"{job['id']}: the reuse plan decides a row twice")
        decided[identity] = decision
    rows = [(job, row) for job in every_job for row in job["matrix"]["include"]]
    if len(decided) != len(rows):
        raise PlanError("the reuse plan does not decide every row of the release file")
    context = {
        "train_sha256": train_sha256,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "train_prefix": train_prefix,
        "content_prefix": content_prefix,
    }
    image_map = dict(image_map or {})
    hits = [decision for decision in decided.values() if "hit" in decision]
    with ThreadPoolExecutor(max_workers=_workers()) as pool:
        # Wave by wave, as planned: a row's same-run inputs are checked against the bindings of the waves before it.
        for wave in sorted({decision["wave"] for decision in hits}):
            futures = [
                (decision, pool.submit(_recheck, store, jobs[decision["job"]], decision, train, image_map, context))
                for decision in hits
                if decision["wave"] == wave
            ]
            for decision, future in futures:
                image = future.result()
                if image is not None and image_map.setdefault(image_key(decision["row"]), image) != image:
                    raise PlanError(f"{image_key(decision['row'])}: the given image map names another image")
    matrices: dict[str, Any] = {}
    reused: list[str] = []
    outcomes: list[tuple[str, str, str]] = []
    ordered = [job for job in every_job if job["family"] == "container"] + [
        job for job in every_job if job["family"] != "container"
    ]
    for job in ordered:
        missed = []
        for row in job["matrix"]["include"]:
            decision = decided[(job["id"], json.dumps(row, sort_keys=True))]
            outcomes.append((job["id"], _label(row), decision["outcome"]))
            if "hit" not in decision:
                missed.append(row)
        if missed:
            matrices[job["id"]] = {"include": missed}
        else:
            reused.append(job["id"])
    return matrices, reused, outcomes, image_map, {key: str(value) for key, value in planning_run.items()}


def main(argv: list[str] | None = None) -> int:
    """Plan the run, plan a train or apply a train's plan, and write the outputs and summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("run", "train", "apply"),
        default="run",
        help="run: decide every row in this release run; train: plan a train before its run and record the plan; "
        "apply: re-check and bind the train's recorded plan in this release run.",
    )
    for name in ("manifest", "train-sha256", "bucket"):
        parser.add_argument(f"--{name}", required=True)
    for name in ("image-map", "run-id", "run-attempt", "github-output", "plan-output"):
        parser.add_argument(f"--{name}")
    parser.add_argument("--summary", required=True, help="File the summary table is appended to.")
    parser.add_argument("--server-url", required=True, help="GITHUB_SERVER_URL, for links to earlier runs.")
    parser.add_argument("--repository", required=True, help="GITHUB_REPOSITORY, whose runs the plan reuses.")
    parser.add_argument("--train-prefix", default="train-state")
    parser.add_argument("--content-prefix", default="artifacts")
    args = parser.parse_args(argv)
    needed = {
        "run": ("image_map", "run_id", "run_attempt", "github_output"),
        "train": ("run_id", "run_attempt", "plan_output"),
        "apply": ("run_id", "run_attempt", "github_output"),
    }[args.mode]
    missing = [f"--{name.replace('_', '-')}" for name in needed if getattr(args, name) is None]
    if missing:
        raise SystemExit(f"reuse plan failed: --mode {args.mode} needs {', '.join(missing)}")
    if _SERVER.fullmatch(args.server_url) is None or SAFE_REPOSITORY.fullmatch(args.repository) is None:
        raise SystemExit("reuse plan failed: --server-url and --repository must name a GitHub server and owner/name")
    runs_url = f"{args.server_url}/{args.repository}/actions/runs"
    prefixes = {"train_prefix": args.train_prefix, "content_prefix": args.content_prefix}
    note = ""
    try:
        manifest = json.loads(Path(args.manifest).read_text())
        given_map = json.loads(args.image_map) if args.image_map else {}
        if not isinstance(given_map, dict):
            raise PlanError("the image map must be a JSON object")
        with tempfile.TemporaryDirectory() as workspace:
            # In-process S3: the AWS CLI's process start was most of each call (s3_boto.py).
            store = Boto3S3(args.bucket)
            development = not tooling_in_keys(
                _load_train(store, args.train_prefix, args.train_sha256, Path(workspace))[0]
            )
            if args.mode == "train":
                record, outcomes, reused_jobs = plan_train(
                    store,
                    manifest,
                    train_sha256=args.train_sha256,
                    planning_run_id=args.run_id,
                    planning_run_attempt=args.run_attempt,
                    workspace=Path(workspace),
                    image_map=given_map,
                    **prefixes,
                )
                note = (
                    "Planned with this train, before its release run; the run re-checks every reused row and binds "
                    "it. A row that builds can still be reused by its own check once its upstream finishes."
                )
            elif args.mode == "apply":
                matrices, reused_jobs, outcomes, image_map, planning = apply_plan(
                    store,
                    manifest,
                    train_sha256=args.train_sha256,
                    run_id=args.run_id,
                    run_attempt=args.run_attempt,
                    workspace=Path(workspace),
                    image_map=given_map,
                    **prefixes,
                )
                note = (
                    f"Planned by Create/Update Train run {planning['run_id']}.{planning['run_attempt']}; "
                    "every reused row was re-checked and bound in this run."
                )
            else:
                matrices, reused_jobs, outcomes, image_map = plan(
                    store,
                    manifest,
                    train_sha256=args.train_sha256,
                    run_id=args.run_id,
                    run_attempt=args.run_attempt,
                    image_map=given_map,
                    workspace=Path(workspace),
                    **prefixes,
                )
    except (PlanError, ValueError, json.JSONDecodeError, BotoCoreError, ClientError) as error:
        raise SystemExit(f"reuse plan failed: {error}") from error
    if args.mode == "train":
        Path(args.plan_output).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    else:
        with Path(args.github_output).open("a") as output:
            output.write(f"matrices={json.dumps(matrices, sort_keys=True, separators=(',', ':'))}\n")
            output.write(f"reused-jobs={json.dumps(reused_jobs, separators=(',', ':'))}\n")
            output.write(f"image-map={json.dumps(image_map, sort_keys=True, separators=(',', ':'))}\n")
    with Path(args.summary).open("a") as summary:
        summary.write(_summary(outcomes, reused_jobs, runs_url, development=development, note=note))
    return 0


if __name__ == "__main__":
    sys.exit(main())
