# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""The reuse plan: every package row of a release run decided before any build job starts (per-row-reuse.md, 4b)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

_DIRECTORY = Path(__file__).parents[1] / "release-candidate-dependencies"
sys.path.insert(0, str(_DIRECTORY))
sys.path.insert(0, str(Path(__file__).parent))

import reuse_plan  # noqa: E402
from row_reuse import S3  # noqa: E402
from test_row_reuse import FAKE_AWS  # noqa: E402

SCRIPT = _DIRECTORY / "reuse_plan.py"
RUN = "500"
EARLIER = "c" * 64
LOGGER_SHA = "1" * 40
KVIKIO_SHA = "2" * 40
TOOLS = {"gha-tools": "3" * 40, "shared-actions": "4" * 40, "shared-workflows": "5" * 40}
IMAGES = {
    f"ci-conda:13-{arch}": "ghcr.io/rapidsai/release-candidates/ci-conda@sha256:" + digit * 64
    for arch, digit in (("amd64", "6"), ("arm64", "7"))
}
TRAIN = {
    "release_version": "26.12",
    "release_units": [
        {
            "id": "source:rapids-logger",
            "artifact_family": "source",
            "repository": "rapids-logger",
            "source_revision": LOGGER_SHA,
            "dependencies": [],
        },
        {"id": "conda:rapids-logger", "artifact_family": "conda", "dependencies": ["source:rapids-logger"]},
        {
            "id": "source:kvikio",
            "artifact_family": "source",
            "repository": "kvikio",
            "source_revision": KVIKIO_SHA,
            "dependencies": [],
        },
        {"id": "conda:kvikio", "artifact_family": "conda", "dependencies": ["source:kvikio", "conda:rapids-logger"]},
    ],
    "source_revisions": {"rapids-logger": LOGGER_SHA, "kvikio": KVIKIO_SHA},
    "github_repositories": {"rapids-logger": "rapidsai/rapids-logger", "kvikio": "rapidsai/kvikio"},
    "build_tool_revisions": {
        "dependency-file-generator": "8" * 40,
        "rapids-build-backend": "9" * 40,
        "rapids-cmake": "a" * 40,
    },
    "build_implementation_revisions": TOOLS,
    "input_receipts": {
        "locks": {"path": "locks/index.json", "sha256": "b" * 64},
        "variants": {"path": "variants/collection.json", "sha256": "d" * 64},
    },
    "candidate_build_policy": {"sccache": True},
}


def rows(*arches):
    return [
        {
            "ARCH": arch,
            "PY_VER": "3.12",
            "CUDA_VER": "13.4.3",
            "LINUX_VER": "rockylinux8",
            "candidate_image_key": f"ci-conda:13-{arch}",
        }
        for arch in arches
    ]


LOGGER_JOB = {
    "id": "rapids-logger--conda-cpp-build",
    "repository": "rapidsai/rapids-logger",
    "project": "rapids-logger",
    "unit": "conda:rapids-logger",
    "family": "conda",
    "entrypoint": "conda-cpp-build-release",
    "identity": {"script": "ci/build_conda.sh", "repo": "rapidsai/rapids-logger"},
    "needs": [],
    "matrix": {"include": rows("amd64", "arm64")},
}
KVIKIO_JOB = {
    "id": "kvikio--cpp-build",
    "repository": "rapidsai/kvikio",
    "project": "kvikio",
    "unit": "conda:kvikio",
    "family": "conda",
    "entrypoint": "conda-cpp-build-release",
    "identity": {"script": "ci/build_cpp.sh", "repo": "rapidsai/kvikio", "node_type": "cpu8"},
    "needs": ["rapids-logger--conda-cpp-build"],
    "matrix": {"include": rows("amd64", "arm64")},
}
MANIFEST = {"schema_version": 1, "jobs": [KVIKIO_JOB, LOGGER_JOB]}


def train_digest(train):
    canonical = subprocess.run(  # noqa: S603 - jq with a fixed program
        [shutil.which("jq") or "jq", "-cSj", "del(.train_sha256)"],
        input=json.dumps(train).encode(),
        capture_output=True,
        check=True,
    ).stdout
    return hashlib.sha256(canonical).hexdigest()


CURRENT = train_digest(TRAIN)


@pytest.fixture
def store(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "aws"
    fake.parent.mkdir()
    fake.write_text(FAKE_AWS)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_S3", str(tmp_path / "s3"))
    monkeypatch.setenv("AWS_CALLS", str(tmp_path / "aws-calls"))
    root = tmp_path / "s3" / "candidate-store"
    put(root, f"train-state/{CURRENT}/release-train.json", json.dumps({**TRAIN, "train_sha256": CURRENT}).encode())
    put(
        root,
        f"train-state/{CURRENT}/scheduler-state.json",
        json.dumps({"workflow_runs": {"rapids-logger": RUN, "kvikio": RUN}}).encode(),
    )
    return root


def put(root: Path, key: str, data: bytes) -> str:
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def get(root: Path, key: str):
    path = root / key
    return json.loads(path.read_text()) if path.is_file() else None


def key_for(tmp_path, job, row, references, s3):
    """The key the plan (and setup) computes for a row consuming these upstream references."""
    workspace = tmp_path / "key-work"
    workspace.mkdir(exist_ok=True)
    train_path = workspace / "release-train.json"
    train_path.write_text(json.dumps({**TRAIN, "train_sha256": CURRENT}))
    build = reuse_plan.build_inputs(TRAIN, reuse_plan._source_inputs(train_path, job["unit"], workspace))
    records = reuse_plan._catalog_records(s3, references)
    return reuse_plan._row_key(job, row, TRAIN, build, records, IMAGES, workspace)


def index_row(root, tmp_path, job, row, *, references=(), run="400", attempt="1", complete=True):
    """An earlier run on another train that built the row, indexed it under its key, and (optionally) completed."""
    s3 = S3("candidate-store")
    key, key_record = key_for(tmp_path, job, row, list(references), s3)
    repository = job["repository"].rsplit("/", 1)[-1]
    platform = {"amd64": "linux-64", "arm64": "linux-aarch64"}[row["ARCH"]]
    artifact = f"{repository}_conda_cpp_{row['ARCH']}"
    digest = hashlib.sha256(f"{repository}/{row['ARCH']}/{run}.{attempt}".encode()).hexdigest()
    artifact_key = f"artifacts/{repository}/{digest}"
    package_path = f"{platform}/{repository}-26.12.00-h0.conda"
    payload_sha = put(root, f"{artifact_key}/{package_path}", f"{artifact} bytes".encode())
    index_sha = put(
        root,
        f"{artifact_key}/artifact-index.json",
        json.dumps(
            {
                "schema_version": 3,
                "build_input_digest": digest,
                "source_artifact": artifact,
                "entries": [{"path": package_path, "sha256": payload_sha}],
            }
        ).encode(),
    )
    record_sha = put(root, f"{artifact_key}/build-record.json", b'{"record": true}')
    bundle_key = f"train-state/{EARLIER}/{repository}/{run}.{attempt}/{digest}/release-catalog-entries.{artifact}.json"
    catalog_sha = put(
        root,
        bundle_key,
        json.dumps(
            {
                "source": {
                    "repository": job["repository"],
                    "sha": TRAIN["source_revisions"][job["project"]],
                    "matrix": {"python_version": row["PY_VER"]},
                },
                "entries": [
                    {
                        "release_catalog_key": job["unit"],
                        "path": package_path,
                        "sha256": payload_sha,
                        "package": {"name": repository, "version": "26.12.00", "build": "h0", "platform": platform},
                    }
                ],
            }
        ).encode(),
    )
    reference = {
        "schema_version": 4,
        "producer": "shared-workflows",
        "storage_layout": 2,
        "train_sha256": EARLIER,
        "repository": job["repository"],
        "source_artifact": artifact,
        "source_run_id": run,
        "source_run_attempt": attempt,
        "build_input_digest": digest,
        "artifact_key": artifact_key,
        "build_record_key": f"{artifact_key}/build-record.json",
        "artifact_index_key": f"{artifact_key}/artifact-index.json",
        "bundle_key": bundle_key,
    }
    put(
        root,
        f"artifacts/{repository}/row-reuse-index/{key}/{artifact}/{run}-{attempt}.json",
        json.dumps(
            {
                "schema_version": 1,
                "row_reuse_key": key,
                "key_record": key_record,
                "reference": reference,
                "build_record_sha256": record_sha,
                "artifact_index_sha256": index_sha,
                "catalog_sha256": catalog_sha,
                "same_run_inputs": [],
            }
        ).encode(),
    )
    if complete:
        put(
            root,
            f"train-state/{EARLIER}/completions/{run}.{attempt}.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "train_sha256": EARLIER,
                    "run_id": run,
                    "run_attempt": attempt,
                    "result": "complete",
                }
            ).encode(),
        )
    return reference


def earlier_run(root, tmp_path, *, skip_logger_arch=None, complete=True):
    """Index every row of an earlier complete run: rapids-logger first, then kvikio consuming it."""
    loggers = [
        index_row(root, tmp_path, LOGGER_JOB, row, complete=complete)
        for row in LOGGER_JOB["matrix"]["include"]
        if row["ARCH"] != skip_logger_arch
    ]
    for row in KVIKIO_JOB["matrix"]["include"]:
        index_row(root, tmp_path, KVIKIO_JOB, row, references=loggers, complete=complete)
    return loggers


def run_plan(tmp_path, manifest=MANIFEST, **overrides):
    workspace = tmp_path / "plan-work"
    workspace.mkdir(exist_ok=True)
    arguments = {
        "train_sha256": CURRENT,
        "run_id": RUN,
        "run_attempt": "1",
        "image_map": IMAGES,
        "workspace": workspace,
        **overrides,
    }
    return reuse_plan.plan(S3("candidate-store"), manifest, **arguments)


def test_everything_reused_skips_every_job_and_binds_every_row(store, tmp_path):
    earlier_run(store, tmp_path)
    matrices, reused, outcomes = run_plan(tmp_path)
    assert matrices == {}  # noqa: S101
    assert reused == ["rapids-logger--conda-cpp-build", "kvikio--cpp-build"]  # noqa: S101
    assert [outcome for _, _, outcome in outcomes] == ["reused from run 400.1"] * 4  # noqa: S101
    for repository, arch in (("rapids-logger", "amd64"), ("rapids-logger", "arm64"), ("kvikio", "amd64")):
        binding = get(
            store, f"train-state/{CURRENT}/{repository}/reuse-bindings/{RUN}/{repository}_conda_cpp_{arch}.json"
        )
        assert binding["source_run_id"] == "400"  # noqa: S101
        assert binding["train_sha256"] == CURRENT  # noqa: S101


def test_an_upstream_miss_builds_that_row_and_every_job_consuming_its_job(store, tmp_path):
    earlier_run(store, tmp_path, skip_logger_arch="amd64")
    matrices, reused, outcomes = run_plan(tmp_path)
    assert matrices == {  # noqa: S101
        "rapids-logger--conda-cpp-build": {"include": rows("amd64")},
        "kvikio--cpp-build": {"include": rows("amd64", "arm64")},
    }
    assert reused == []  # noqa: S101
    # The reusable arm64 row is still bound; kvikio waits for its upstream job and decides in-row.
    assert get(store, f"train-state/{CURRENT}/rapids-logger/reuse-bindings/{RUN}/rapids-logger_conda_cpp_arm64.json")  # noqa: S101
    assert not (store / f"train-state/{CURRENT}/kvikio/reuse-bindings").exists()  # noqa: S101
    kvikio = [outcome for job, _, outcome in outcomes if job == "kvikio--cpp-build"]
    # Only the first thing that forced the build: the rapids-logger row that had no earlier build.
    # kvikio has no earlier build of its own, which is all its rows need to say.
    assert kvikio == ["builds: no earlier build of this row"] * 2  # noqa: S101


def test_a_run_without_a_completion_record_is_not_reused(store, tmp_path):
    earlier_run(store, tmp_path, complete=False)
    matrices, reused, _ = run_plan(tmp_path)
    assert set(matrices) == {"rapids-logger--conda-cpp-build", "kvikio--cpp-build"}  # noqa: S101
    assert reused == []  # noqa: S101


def test_a_changed_input_changes_the_key_and_builds(store, tmp_path):
    earlier_run(store, tmp_path)
    changed = {**LOGGER_JOB, "identity": {**LOGGER_JOB["identity"], "script": "ci/build_conda_v2.sh"}}
    matrices, reused, _ = run_plan(tmp_path, {"schema_version": 1, "jobs": [KVIKIO_JOB, changed]})
    assert set(matrices) == {"rapids-logger--conda-cpp-build", "kvikio--cpp-build"}  # noqa: S101
    assert reused == []  # noqa: S101


def test_a_row_without_a_digest_pinned_image_builds(store, tmp_path):
    earlier_run(store, tmp_path)
    matrices, reused, outcomes = run_plan(tmp_path, image_map={})
    assert reused == []  # noqa: S101
    assert {outcome for _, _, outcome in outcomes if outcome.startswith("builds: no reuse key")}  # noqa: S101
    assert set(matrices) == {"rapids-logger--conda-cpp-build", "kvikio--cpp-build"}  # noqa: S101


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ({"workflow_runs": {"rapids-logger": RUN}}, "kvikio is bound to run None"),
        ({"workflow_runs": {"rapids-logger": RUN, "kvikio": "499"}}, "kvikio is bound to run '499'"),
    ],
)
def test_a_repository_not_bound_to_this_run_stops_the_plan(store, tmp_path, state, message):
    put(store, f"train-state/{CURRENT}/scheduler-state.json", json.dumps(state).encode())
    with pytest.raises(reuse_plan.PlanError, match=message):
        run_plan(tmp_path)


def test_a_train_that_does_not_match_its_digest_stops_the_plan(store, tmp_path):
    put(store, f"train-state/{CURRENT}/release-train.json", json.dumps({**TRAIN, "release_version": "26.10"}).encode())
    with pytest.raises(reuse_plan.PlanError, match="does not match its digest"):
        run_plan(tmp_path)


@pytest.mark.parametrize(
    ("jobs", "message"),
    [
        ([], "lists no jobs"),
        ([LOGGER_JOB, LOGGER_JOB], "invalid or repeated"),
        ([{**KVIKIO_JOB, "needs": ["missing"]}, LOGGER_JOB], "needs unknown jobs"),
        (
            [
                {**KVIKIO_JOB, "needs": ["rapids-logger--conda-cpp-build"]},
                {**LOGGER_JOB, "needs": ["kvikio--cpp-build"]},
            ],
            "cycle",
        ),
        ([{**LOGGER_JOB, "family": "docs", "unit": "docs:rapids-logger"}], "conda and wheel rows only"),
        ([{**LOGGER_JOB, "identity": {}}], "non-empty object"),
        ([{**LOGGER_JOB, "matrix": {"include": [{"ARCH": "amd64"}]}}], "every row needs"),
    ],
)
def test_a_malformed_manifest_stops_the_plan(store, tmp_path, jobs, message):
    with pytest.raises(reuse_plan.PlanError, match=message):
        run_plan(tmp_path, {"schema_version": 1, "jobs": jobs})


def test_cli_writes_the_outputs_and_the_summary(store, tmp_path):
    earlier_run(store, tmp_path, skip_logger_arch="amd64")
    (tmp_path / "manifest.json").write_text(json.dumps(MANIFEST))
    output, summary = tmp_path / "github-output", tmp_path / "summary.md"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(SCRIPT),
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--image-map",
            json.dumps(IMAGES),
            "--train-sha256",
            CURRENT,
            "--run-id",
            RUN,
            "--run-attempt",
            "1",
            "--bucket",
            "candidate-store",
            "--github-output",
            str(output),
            "--summary",
            str(summary),
            "--server-url",
            "https://github.com",
            "--repository",
            "rapidsai/release-tracking",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr  # noqa: S101
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads(outputs["reused-jobs"]) == []  # noqa: S101
    assert json.loads(outputs["matrices"])["rapids-logger--conda-cpp-build"] == {"include": rows("amd64")}  # noqa: S101
    table = summary.read_text()
    # A run is a link to that run attempt, not a bare number.
    run_link = "[run 400.1](https://github.com/rapidsai/release-tracking/actions/runs/400/attempts/1)"
    assert f"| rapids-logger--conda-cpp-build | 13.4.3, 3.12, arm64 | reused from {run_link} |" in table  # noqa: S101
    assert "| rapids-logger--conda-cpp-build | 13.4.3, 3.12, amd64 | builds: no earlier build of this row |" in table  # noqa: S101
    assert "reuse key" in result.stdout  # noqa: S101


def test_cli_fails_closed_on_a_bad_plan(store, tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"schema_version": 1, "jobs": []}))
    output = tmp_path / "github-output"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(SCRIPT),
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--image-map",
            "{}",
            "--train-sha256",
            CURRENT,
            "--run-id",
            RUN,
            "--run-attempt",
            "1",
            "--bucket",
            "candidate-store",
            "--github-output",
            str(output),
            "--summary",
            str(tmp_path / "summary.md"),
            "--server-url",
            "https://github.com",
            "--repository",
            "rapidsai/release-tracking",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0  # noqa: S101
    assert "reuse plan failed" in result.stderr  # noqa: S101
    assert not output.exists()  # noqa: S101


ACTION = Path(__file__).parents[1] / "candidate-reuse-plan" / "action.yml"


def test_the_action_passes_inputs_through_the_environment_and_pins_its_dependency():
    import yaml

    action = yaml.safe_load(ACTION.read_text())
    for step in action["runs"]["steps"]:
        assert "${{" not in step["run"], step["name"]  # noqa: S101
    install = next(step for step in action["runs"]["steps"] if step.get("id") == "install")
    assert "--require-hashes" in install["run"]  # noqa: S101
    # The interpreter passes as a step output read through env, never through GITHUB_ENV.
    assert "GITHUB_ENV" not in ACTION.read_text()  # noqa: S101
    plan_step = next(step for step in action["runs"]["steps"] if step.get("id") == "plan")
    assert plan_step["env"]["REUSE_PLAN_PYTHON"] == "${{ steps.install.outputs.python }}"  # noqa: S101
    requirements = (ACTION.parent / "requirements.txt").read_text()
    assert "packaging==25.0" in requirements  # noqa: S101
    assert "--hash=sha256:" in requirements  # noqa: S101
    assert action["outputs"]["matrices"]["value"] == "${{ steps.plan.outputs.matrices }}"  # noqa: S101
    assert action["outputs"]["reused-jobs"]["value"] == "${{ steps.plan.outputs.reused-jobs }}"  # noqa: S101


def test_the_action_step_runs_the_plan(store, tmp_path):
    import yaml

    earlier_run(store, tmp_path)
    step = next(step for step in yaml.safe_load(ACTION.read_text())["runs"]["steps"] if step.get("id") == "plan")
    output, summary = tmp_path / "github-output", tmp_path / "summary.md"
    environment = {
        **os.environ,
        "ACTION_PATH": str(ACTION.parent),
        "MANIFEST": json.dumps(MANIFEST),
        "IMAGE_MAP": json.dumps(IMAGES),
        "TRAIN_SHA256": CURRENT,
        "BUCKET": "candidate-store",
        "TRAIN_PREFIX": "train-state",
        "CONTENT_PREFIX": "artifacts",
        "GITHUB_RUN_ID": RUN,
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
        "REUSE_PLAN_PYTHON": sys.executable,
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "rapidsai/release-tracking",
    }
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", step["run"]], env=environment, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr  # noqa: S101
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads(outputs["reused-jobs"]) == ["rapids-logger--conda-cpp-build", "kvikio--cpp-build"]  # noqa: S101
    assert json.loads(outputs["matrices"]) == {}  # noqa: S101
    assert "## Reuse plan" in summary.read_text()  # noqa: S101
    assert "(https://github.com/rapidsai/release-tracking/actions/runs/400/attempts/1)" in summary.read_text()  # noqa: S101


class BarrierS3(S3):
    """Makes the first index listings wait for each other: a plan that decides rows one at a time breaks the barrier."""

    def __init__(self, bucket, parties):
        super().__init__(bucket)
        self.barrier = threading.Barrier(parties, timeout=20)
        self.waiting = parties
        self.lock = threading.Lock()

    def keys(self, prefix):
        if "/row-reuse-index/" in prefix:
            with self.lock:
                wait = self.waiting > 0
                self.waiting -= 1
            if wait:
                self.barrier.wait()
        return super().keys(prefix)


def test_the_rows_of_a_wave_are_decided_at_the_same_time(store, tmp_path):
    # Live, deciding 20 rows one at a time took 167 s (release run 45727545063).
    earlier_run(store, tmp_path)
    workspace = tmp_path / "plan-work"
    workspace.mkdir()
    matrices, reused, _ = reuse_plan.plan(
        BarrierS3("candidate-store", parties=2),
        MANIFEST,
        train_sha256=CURRENT,
        run_id=RUN,
        run_attempt="1",
        image_map=IMAGES,
        workspace=workspace,
    )
    assert matrices == {}  # noqa: S101
    assert reused == ["rapids-logger--conda-cpp-build", "kvikio--cpp-build"]  # noqa: S101


def test_an_earlier_runs_completion_records_are_listed_once(store, tmp_path):
    earlier_run(store, tmp_path)
    calls = tmp_path / "aws-calls"
    calls.unlink(missing_ok=True)
    run_plan(tmp_path)
    listings = [
        line for line in calls.read_text().splitlines() if "list-objects-v2" in line and "/completions/" in line
    ]
    assert len(listings) == 1, listings  # noqa: S101


def test_the_action_runs_the_plan_unbuffered_so_its_log_shows_each_row_as_it_is_decided():
    import yaml

    step = next(step for step in yaml.safe_load(ACTION.read_text())["runs"]["steps"] if step.get("id") == "plan")
    assert '"${REUSE_PLAN_PYTHON:?}" -u ' in step["run"]  # noqa: S101


@pytest.mark.parametrize(
    ("server", "repository"),
    [
        ("", "rapidsai/release-tracking"),
        ("https://github.com", ""),
        ("javascript:x", "rapidsai/release-tracking"),
        ("https://github.com", "../evil"),
    ],
)
def test_the_summary_links_need_a_valid_server_and_repository(store, tmp_path, server, repository):
    (tmp_path / "manifest.json").write_text(json.dumps(MANIFEST))
    output = tmp_path / "github-output"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(SCRIPT),
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--image-map",
            json.dumps(IMAGES),
            "--train-sha256",
            CURRENT,
            "--run-id",
            RUN,
            "--run-attempt",
            "1",
            "--bucket",
            "candidate-store",
            "--github-output",
            str(output),
            "--summary",
            str(tmp_path / "summary.md"),
            "--server-url",
            server,
            "--repository",
            repository,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0  # noqa: S101
    assert not output.exists()  # noqa: S101


def current_record(tmp_path, job, row, references=()):
    return key_for(tmp_path, job, row, list(references), S3("candidate-store"))[1]


def history(root, job, row, key_record, *, run="300", attempt="1", train=EARLIER, complete=True):
    """A previous build of the row, filed under the row as the upload files it."""
    repository = job["repository"].rsplit("/", 1)[-1]
    digest = reuse_plan.row_digest(key_record)
    key = hashlib.sha256(json.dumps(key_record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    put(
        root,
        f"artifacts/{repository}/row-reuse-history/{digest}/{repository}_conda_cpp_{row['ARCH']}/{run}-{attempt}.json",
        json.dumps(
            {
                "schema_version": 1,
                "row_reuse_key": key,
                "key_record": key_record,
                "train_sha256": train,
                "run_id": run,
                "run_attempt": attempt,
            }
        ).encode(),
    )
    if complete:
        put(
            root,
            f"train-state/{train}/completions/{run}.{attempt}.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "train_sha256": train,
                    "run_id": run,
                    "run_attempt": attempt,
                    "result": "complete",
                }
            ).encode(),
        )


def changed(record, path, value):
    record = json.loads(json.dumps(record))
    target = record
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    return record


@pytest.mark.parametrize(
    ("path", "value", "cause"),
    [
        (("implementation_revisions", "shared-actions"), "9" * 40, "tooling: shared-actions 9999999 → 4444444"),
        (
            ("runtime_image",),
            "ghcr.io/rapidsai/release-candidates/ci-conda@sha256:" + "0" * 64,
            "CI image: sha256:0000000 → sha256:6666666",
        ),
        (("producer", "sha"), "0" * 40, "source: rapidsai/rapids-logger 0000000 → 1111111"),
        (("build_inputs", "train_inputs", "lock_receipt_sha256"), "0" * 64, "locks changed"),
        (("build", "identity", "script"), "ci/old.sh", "release-file settings: script"),
    ],
)
def test_a_root_miss_names_the_first_input_that_changed(store, tmp_path, path, value, cause):
    row = LOGGER_JOB["matrix"]["include"][0]
    history(store, LOGGER_JOB, row, changed(current_record(tmp_path, LOGGER_JOB, row), path, value))
    _, _, outcomes = run_plan(tmp_path)
    assert (LOGGER_JOB["id"], "13.4.3, 3.12, amd64", f"builds: {cause}") in outcomes  # noqa: S101


def test_the_first_difference_wins_when_several_inputs_changed(store, tmp_path):
    row = LOGGER_JOB["matrix"]["include"][0]
    previous = changed(current_record(tmp_path, LOGGER_JOB, row), ("producer", "sha"), "0" * 40)
    previous = changed(previous, ("implementation_revisions", "shared-actions"), "9" * 40)
    history(store, LOGGER_JOB, row, previous)
    _, _, outcomes = run_plan(tmp_path)
    assert (LOGGER_JOB["id"], "13.4.3, 3.12, amd64", "builds: tooling: shared-actions 9999999 → 4444444") in outcomes  # noqa: S101


def test_an_upstream_package_change_names_the_upstream_repository(store, tmp_path):
    loggers = earlier_run(store, tmp_path)
    row = KVIKIO_JOB["matrix"]["include"][0]
    previous = current_record(tmp_path, KVIKIO_JOB, row, loggers)
    previous["upstream"][0]["sha256"] = "0" * 64
    # The earlier run's kvikio rows are indexed under today's key; remove them so this row misses on its own.
    for path in (store / "artifacts/kvikio/row-reuse-index").rglob("*.json"):
        path.unlink()
    history(store, KVIKIO_JOB, row, previous)
    _, _, outcomes = run_plan(tmp_path)
    assert (KVIKIO_JOB["id"], "13.4.3, 3.12, amd64", "builds: upstream: rapidsai/rapids-logger changed") in outcomes  # noqa: S101


def test_a_row_without_a_previous_build_says_so(store, tmp_path):
    _, _, outcomes = run_plan(tmp_path)
    assert (LOGGER_JOB["id"], "13.4.3, 3.12, amd64", "builds: no earlier build of this row") in outcomes  # noqa: S101


def test_a_previous_build_that_never_completed_is_not_the_comparison(store, tmp_path):
    row = LOGGER_JOB["matrix"]["include"][0]
    record = current_record(tmp_path, LOGGER_JOB, row)
    history(store, LOGGER_JOB, row, changed(record, ("producer", "sha"), "0" * 40), run="300")
    history(
        store,
        LOGGER_JOB,
        row,
        changed(record, ("runtime_image",), IMAGES["ci-conda:13-arm64"]),
        run="350",
        complete=False,
    )
    _, _, outcomes = run_plan(tmp_path)
    expected = (LOGGER_JOB["id"], "13.4.3, 3.12, amd64", "builds: source: rapidsai/rapids-logger 0000000 → 1111111")
    assert expected in outcomes  # noqa: S101


def test_a_cascaded_row_names_its_own_change_before_its_upstream(store, tmp_path):
    # Release run 45740919736 named the upstream job's cause on every kvikio row, though kvikio's own tooling changed.
    for job in (LOGGER_JOB, KVIKIO_JOB):
        for row in job["matrix"]["include"]:
            history(
                store,
                job,
                row,
                changed(current_record(tmp_path, job, row), ("implementation_revisions", "shared-actions"), "9" * 40),
            )
    _, _, outcomes = run_plan(tmp_path)
    kvikio = {outcome for job, _, outcome in outcomes if job == KVIKIO_JOB["id"]}
    assert kvikio == {"builds: tooling: shared-actions 9999999 → 4444444"}  # noqa: S101


def test_a_cascaded_row_with_nothing_else_changed_names_its_matching_upstream_row(store, tmp_path):
    # Only the upstream changed for kvikio, so it names the rapids-logger row with its own arch and CUDA (rapids-logger
    # builds only CUDA 13.4.3, which serves both CUDA majors, so the arch is what must match).
    for row in KVIKIO_JOB["matrix"]["include"]:
        previous = current_record(tmp_path, KVIKIO_JOB, row)
        previous["upstream"] = [{"producer": {"repository": "rapidsai/rapids-logger"}, "sha256": "0" * 64}]
        history(store, KVIKIO_JOB, row, previous)
    _, _, outcomes = run_plan(tmp_path)
    assert {(row, outcome) for job, row, outcome in outcomes if job == KVIKIO_JOB["id"]} == {  # noqa: S101
        (f"13.4.3, 3.12, {arch}", f"builds: upstream: rapids-logger--conda-cpp-build 13.4.3, 3.12, {arch} builds")
        for arch in ("amd64", "arm64")
    }


def test_the_row_digest_is_the_uploads_jq_filter():
    upload = (Path(__file__).parents[1] / "release-catalog" / "upload-candidate-s3.sh").read_text()
    assert f"jq -cSj '{reuse_plan.ROW_DIGEST_JQ}'" in upload  # noqa: S101
    record = {
        "producer": {"repository": "rapidsai/kvikio", "sha": "1" * 40},
        "build": {"entrypoint": "x-release"},
        "row": {
            "unit": "conda:kvikio",
            "family": "conda",
            "arch": "arm64",
            "cuda_version": "12.9.2",
            "python_version": "3.12",
        },
        "runtime_image": "ignored",
    }
    jq = subprocess.run(  # noqa: S603 - jq with the upload's fixed filter
        [shutil.which("jq") or "jq", "-cSj", reuse_plan.ROW_DIGEST_JQ],
        input=json.dumps(record).encode(),
        capture_output=True,
        check=True,
    ).stdout
    assert reuse_plan.row_digest(record) == hashlib.sha256(jq).hexdigest()  # noqa: S101


def test_the_plan_uses_more_workers_on_a_bigger_runner(monkeypatch):
    # Each row's AWS CLI calls are mostly process start-up, so rows compete for cores (release run 45736667710:
    # 14 s per row four at a time, 31-34 s eight at a time on a 4-core runner).
    monkeypatch.setattr(reuse_plan.os, "cpu_count", lambda: 16)
    assert reuse_plan._workers() == 32  # noqa: S101
    monkeypatch.setattr(reuse_plan.os, "cpu_count", lambda: 2)
    assert reuse_plan._workers() == 4  # noqa: S101
    monkeypatch.setattr(reuse_plan.os, "cpu_count", lambda: None)
    assert reuse_plan._workers() == 2  # noqa: S101


def test_the_action_checks_its_tools_before_planning(tmp_path):
    import yaml

    steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    check = steps[0]
    assert check["name"] == "Require the plan's tools on this runner"  # noqa: S101
    empty = tmp_path / "empty-path"
    empty.mkdir()
    (empty / "bash").symlink_to(shutil.which("bash"))
    result = subprocess.run(  # noqa: S603
        [shutil.which("bash"), "-c", check["run"]],
        env={"PATH": str(empty)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0  # noqa: S101
    assert "needs aws" in result.stderr  # noqa: S101


def test_the_summary_says_when_tooling_is_out_of_the_keys(store, tmp_path):
    development = {**TRAIN, "candidate_build_policy": {"sccache": True, "tooling_in_reuse_keys": False}}
    digest = train_digest(development)
    put(store, f"train-state/{digest}/release-train.json", json.dumps({**development, "train_sha256": digest}).encode())
    put(
        store,
        f"train-state/{digest}/scheduler-state.json",
        json.dumps({"workflow_runs": {"rapids-logger": RUN, "kvikio": RUN}}).encode(),
    )
    workspace = tmp_path / "plan-work"
    workspace.mkdir()
    _, reused, outcomes = reuse_plan.plan(
        S3("candidate-store"),
        MANIFEST,
        train_sha256=digest,
        run_id=RUN,
        run_attempt="1",
        image_map=IMAGES,
        workspace=workspace,
    )
    summary = reuse_plan._summary(
        outcomes, reused, "https://github.com/rapidsai/release-tracking/actions/runs", development=True
    )
    assert "Development: the tooling commits are not in this train's reuse keys" in summary  # noqa: S101
    assert reuse_plan.tooling_in_keys(development) is False  # noqa: S101
    assert reuse_plan.tooling_in_keys(TRAIN) is True  # noqa: S101


def test_switching_the_development_setting_is_named_as_a_tooling_change():
    current = {"implementation_revisions": "excluded for development"}
    previous = {"implementation_revisions": {"shared-actions": "4" * 40}}
    assert reuse_plan._cause(current, previous) == "tooling: development keys switched on"  # noqa: S101
    assert reuse_plan._cause(previous, current) == "tooling: development keys switched off"  # noqa: S101
