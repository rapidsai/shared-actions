# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Rows the plan left to build only because an upstream row builds are decided again, natively, before their job.

Every row of a release build starts its CI image container before its own reuse check runs, about two minutes per
row (release run 45912763605). The plan keys a job only when every job it needs was reused in full, so one rebuilt
upstream row used to send every row of every job below it into a container, though most of them were then reused.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "release-candidate-dependencies"))
sys.path.insert(0, str(Path(__file__).parent))

import reuse_plan
from row_reuse import S3
from test_reuse_plan import (
    CURRENT,
    EARLIER,
    FAKE_AWS,
    IMAGES,
    KVIKIO_JOB,
    LOGGER_JOB,
    MANIFEST,
    RUN,
    TRAIN,
    changed,
    cli_arguments,
    current_record,
    earlier_run,
    get,
    history,
    make_store,
    put,
    rows,
    run_plan,
)

# reference-binding.sh reads completion records with `aws s3 cp`; the shared fake speaks only s3api.
FAKE_AWS_WITH_CP = FAKE_AWS.replace(
    'assert args[0] == "s3api", args',
    """if args[:2] == ["s3", "cp"]:
    bucket, _, key = args[2].removeprefix("s3://").partition("/")
    source = root / bucket / key
    if not source.is_file():
        print("fatal error: An error occurred (404) when calling the HeadObject operation", file=sys.stderr)
        sys.exit(1)
    Path(args[3]).write_bytes(source.read_bytes())
    sys.exit(0)
assert args[0] == "s3api", args""",
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = make_store(tmp_path, monkeypatch)
    (tmp_path / "bin" / "aws").write_text(FAKE_AWS_WITH_CP)
    return root


def built_rows(store, tmp_path, job):
    """Each of the job's rows was built before and nothing of its own changed since."""
    for row in job["matrix"]["include"]:
        history(store, job, row, current_record(tmp_path, job, row))


def plan_decisions(tmp_path):
    decisions = []
    matrices, reused, _, _ = run_plan(tmp_path, decisions=decisions)
    return matrices, reused, decisions


def built_in_this_run(root, job, row, *, attempt="1"):
    """This run's upload of one row: its packages, catalog and train reference."""
    repository = job["repository"].rsplit("/", 1)[-1]
    platform = {"amd64": "linux-64", "arm64": "linux-aarch64"}[row["ARCH"]]
    artifact = f"{repository}_conda_cpp_{row['ARCH']}"
    digest = hashlib.sha256(f"{repository}/{row['ARCH']}/{RUN}.{attempt}".encode()).hexdigest()
    artifact_key = f"artifacts/{repository}/{digest}"
    package_path = f"{platform}/{repository}-26.12.00-h1.conda"
    payload_sha = put(root, f"{artifact_key}/{package_path}", f"{artifact} rebuilt".encode())
    bundle_key = f"train-state/{CURRENT}/{repository}/{RUN}.{attempt}/{digest}/release-catalog-entries.{artifact}.json"
    put(
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
                        "package": {"name": repository, "version": "26.12.00", "build": "h1", "platform": platform},
                    }
                ],
            }
        ).encode(),
    )
    put(
        root,
        f"train-state/{CURRENT}/{repository}/{RUN}.{attempt}/{digest}/bundle-reference.{artifact}.json",
        json.dumps(
            {
                "schema_version": 4,
                "producer": "shared-workflows",
                "storage_layout": 2,
                "train_sha256": CURRENT,
                "repository": job["repository"],
                "source_artifact": artifact,
                "source_run_id": RUN,
                "source_run_attempt": attempt,
                "build_input_digest": digest,
                "artifact_key": artifact_key,
                "build_record_key": f"{artifact_key}/build-record.json",
                "artifact_index_key": f"{artifact_key}/artifact-index.json",
                "bundle_key": bundle_key,
            }
        ).encode(),
    )


def decide(tmp_path, job, job_rows, **overrides):
    workspace = tmp_path / "decide-work"
    workspace.mkdir(exist_ok=True)
    arguments = {
        "train_sha256": CURRENT,
        "run_id": RUN,
        "run_attempt": "1",
        "image_map": IMAGES,
        "workspace": workspace,
        **overrides,
    }
    return reuse_plan.decide_job(S3("candidate-store"), MANIFEST, job["id"], {"include": job_rows}, **arguments)


def test_a_row_whose_only_building_upstream_row_it_does_not_install_is_left_open(store, tmp_path):
    # rapids-logger amd64 has no earlier build; its arm64 row and kvikio's earlier builds are all reusable.
    earlier_run(store, tmp_path, skip_logger_arch="amd64")
    built_rows(store, tmp_path, KVIKIO_JOB)
    matrices, _, decisions = plan_decisions(tmp_path)
    assert matrices["kvikio--cpp-build"] == {"include": rows("amd64", "arm64")}  # noqa: S101
    # kvikio arm64 installs only rapids-logger arm64, which was reused: it is open. Its amd64 row needs the
    # rapids-logger amd64 row that builds, so it builds whatever happens.
    assert {  # noqa: S101
        decision["row"]["ARCH"]
        for decision in decisions
        if decision["job"] == "kvikio--cpp-build"
        if decision.get("may_reuse")
    } == {"arm64"}
    assert reuse_plan.decide_jobs(decisions, MANIFEST["jobs"]) == ["kvikio--cpp-build"]  # noqa: S101


def test_a_row_whose_own_inputs_changed_is_not_left_open(store, tmp_path):
    earlier_run(store, tmp_path, skip_logger_arch="amd64")
    for row in KVIKIO_JOB["matrix"]["include"]:
        record = current_record(tmp_path, KVIKIO_JOB, row)
        history(store, KVIKIO_JOB, row, changed(record, ("producer", "sha"), "0" * 40))
    _, _, decisions = plan_decisions(tmp_path)
    assert reuse_plan.decide_jobs(decisions, MANIFEST["jobs"]) == []  # noqa: S101


def test_a_row_never_built_before_is_not_left_open(store, tmp_path):
    earlier_run(store, tmp_path, skip_logger_arch="amd64")
    _, _, decisions = plan_decisions(tmp_path)
    assert reuse_plan.decide_jobs(decisions, MANIFEST["jobs"]) == []  # noqa: S101


def test_a_row_built_before_in_a_run_that_never_completed_is_left_open(store, tmp_path):
    # Verify records each artifact it checked even when its run did not complete, and reuse takes those.
    earlier_run(store, tmp_path, skip_logger_arch="amd64")
    for row in KVIKIO_JOB["matrix"]["include"]:
        history(store, KVIKIO_JOB, row, current_record(tmp_path, KVIKIO_JOB, row), run="350", complete=False)
    _, _, decisions = plan_decisions(tmp_path)
    assert reuse_plan.decide_jobs(decisions, MANIFEST["jobs"]) == ["kvikio--cpp-build"]  # noqa: S101
    kvikio = {decision["outcome"] for decision in decisions if decision["job"] == "kvikio--cpp-build"}
    assert "builds: no completed earlier build of this row" in kvikio  # noqa: S101


def test_once_its_upstream_finished_the_open_row_is_reused_and_the_other_builds(store, tmp_path):
    earlier_run(store, tmp_path, skip_logger_arch="amd64")
    built_rows(store, tmp_path, KVIKIO_JOB)
    matrices, _, _ = plan_decisions(tmp_path)
    built_in_this_run(store, LOGGER_JOB, rows("amd64")[0])

    missed, outcomes = decide(tmp_path, KVIKIO_JOB, matrices["kvikio--cpp-build"]["include"])

    assert missed == rows("amd64")  # noqa: S101
    assert ("kvikio--cpp-build", "13.4.3, 3.12, arm64", "reused from run 400.1") in outcomes  # noqa: S101
    binding = get(store, f"train-state/{CURRENT}/kvikio/reuse-bindings/{RUN}/kvikio_conda_cpp_arm64.json")
    assert binding["source_run_id"] == "400"  # noqa: S101
    assert not (store / f"train-state/{CURRENT}/kvikio/reuse-bindings/{RUN}/kvikio_conda_cpp_amd64.json").exists()  # noqa: S101


def test_the_rows_read_this_runs_newest_attempt_and_drop_other_runs(store, tmp_path):
    earlier_run(store, tmp_path, skip_logger_arch="amd64")
    run_plan(tmp_path)
    built_in_this_run(store, LOGGER_JOB, rows("amd64")[0], attempt="1")
    built_in_this_run(store, LOGGER_JOB, rows("amd64")[0], attempt="2")
    # Another run of this train built it too; this run is bound, so that one is dropped.
    name = "bundle-reference.rapids-logger_conda_cpp_amd64.json"
    ours = next((Path(store) / f"train-state/{CURRENT}/rapids-logger/{RUN}.1").rglob(name))
    other = {**json.loads(ours.read_text()), "source_run_id": "499", "build_input_digest": "e" * 64}
    put(store, f"train-state/{CURRENT}/rapids-logger/499.1/{'e' * 64}/{name}", json.dumps(other).encode())
    workspace = tmp_path / "records"
    workspace.mkdir()
    records = reuse_plan._run_catalog_records(
        S3("candidate-store"), train_sha256=CURRENT, run_id=RUN, train_prefix="train-state", workspace=workspace
    )
    builds = sorted(record["entries"][0]["package"]["build"] for record in records)
    # rapids-logger arm64 reused from run 400 (h0), and only attempt 2 of this run's amd64 build (h1).
    assert builds == ["h0", "h1"]  # noqa: S101
    digests = {record["build_input_digest"] for record in records}
    assert hashlib.sha256(f"rapids-logger/amd64/{RUN}.2".encode()).hexdigest() in digests  # noqa: S101


def test_a_reference_setup_would_refuse_stops_the_decision(store, tmp_path):
    earlier_run(store, tmp_path, skip_logger_arch="amd64")
    run_plan(tmp_path)
    built_in_this_run(store, LOGGER_JOB, rows("amd64")[0])
    # A binding this run wrote that names another source run than its reference: setup would refuse it, so must this.
    binding = (
        Path(store) / f"train-state/{CURRENT}/rapids-logger/reuse-bindings/{RUN}/rapids-logger_conda_cpp_arm64.json"
    )
    binding.write_text(json.dumps({**json.loads(binding.read_text()), "train_sha256": EARLIER}))
    with pytest.raises(reuse_plan.PlanError, match=r"a train reference setup would refuse"):
        decide(tmp_path, KVIKIO_JOB, rows("arm64"))


def test_rows_outside_the_jobs_matrix_are_refused(store, tmp_path):
    with pytest.raises(reuse_plan.PlanError, match="non-empty subset of its matrix"):
        decide(tmp_path, KVIKIO_JOB, [{**rows("amd64")[0], "ARCH": "ppc64le"}])
    with pytest.raises(reuse_plan.PlanError, match="non-empty subset of its matrix"):
        decide(tmp_path, KVIKIO_JOB, [])


def test_cli_job_mode_writes_the_rows_that_build(store, tmp_path, monkeypatch):
    monkeypatch.setattr(reuse_plan, "Boto3S3", S3)
    earlier_run(store, tmp_path, skip_logger_arch="amd64")
    built_rows(store, tmp_path, KVIKIO_JOB)
    run_plan(tmp_path)
    built_in_this_run(store, LOGGER_JOB, rows("amd64")[0])
    (tmp_path / "manifest.json").write_text(json.dumps(MANIFEST))
    output, summary = tmp_path / "github-output", tmp_path / "summary.md"
    arguments = [
        "--mode",
        "job",
        "--job",
        "kvikio--cpp-build",
        "--rows",
        json.dumps({"include": rows("amd64", "arm64")}),
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
    ]
    assert reuse_plan.main(arguments) == 0  # noqa: S101
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads(outputs["matrix"]) == {"include": rows("amd64")}  # noqa: S101
    assert outputs["build"] == "true"  # noqa: S101
    assert "| kvikio--cpp-build | 13.4.3, 3.12, arm64 | reused from" in summary.read_text()  # noqa: S101


def test_cli_run_mode_names_the_jobs_to_decide_again(store, tmp_path, monkeypatch):
    monkeypatch.setattr(reuse_plan, "Boto3S3", S3)
    earlier_run(store, tmp_path, skip_logger_arch="amd64")
    built_rows(store, tmp_path, KVIKIO_JOB)
    output = tmp_path / "github-output"
    assert reuse_plan.main(cli_arguments(tmp_path, MANIFEST, IMAGES, output, tmp_path / "summary.md")) == 0  # noqa: S101
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads(outputs["decide-jobs"]) == ["kvikio--cpp-build"]  # noqa: S101
