# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""A train's reuse plan, made by Create/Update Train and applied by the release run (plans/per-row-reuse.md, 4c)."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "release-candidate-dependencies"))
sys.path.insert(0, str(Path(__file__).parent))

from row_reuse import S3, _current_digest
from test_reuse_plan import (
    CURRENT,
    IMAGES,
    KVIKIO_JOB,
    LOGGER_JOB,
    MANIFEST,
    RUN,
    earlier_run,
    get,
    put,
    reuse_plan,
    rows,
)

PLANNING_RUN = "900"
# These tests' manifest has no image job, so the fixture's image map stands in for the image wave on both sides.
PLAN_KEY = f"train-state/{CURRENT}/reuse-plans/{PLANNING_RUN}-1.json"


@pytest.fixture
def store(tmp_path, monkeypatch):
    from test_reuse_plan import make_store

    return make_store(tmp_path, monkeypatch)


def workspace(tmp_path, name):
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    return path


def plan_train(tmp_path, manifest=MANIFEST, run=PLANNING_RUN):
    return reuse_plan.plan_train(
        S3("candidate-store"),
        manifest,
        train_sha256=CURRENT,
        planning_run_id=run,
        planning_run_attempt="1",
        workspace=workspace(tmp_path, f"train-work-{run}"),
        image_map=IMAGES,
    )


def apply(tmp_path, manifest=MANIFEST):
    return reuse_plan.apply_plan(
        S3("candidate-store"),
        manifest,
        train_sha256=CURRENT,
        run_id=RUN,
        run_attempt="1",
        workspace=workspace(tmp_path, "apply-work"),
        image_map=IMAGES,
    )


def bindings(store):
    return sorted(path.name for path in store.rglob(f"reuse-bindings/{RUN}/*.json"))


def test_planning_a_train_records_every_rows_decision_and_binds_nothing(store, tmp_path):
    earlier_run(store, tmp_path)
    # A train being planned has no run, so nothing needs to be bound to one.
    (store / f"train-state/{CURRENT}/scheduler-state.json").unlink()

    record, outcomes, reused = plan_train(tmp_path)

    assert get(store, PLAN_KEY) == record  # noqa: S101
    assert record["planning_run"] == {"run_id": PLANNING_RUN, "run_attempt": "1"}  # noqa: S101
    assert record["manifest_sha256"] == reuse_plan.manifest_digest(MANIFEST)  # noqa: S101
    assert [(row["job"], row["wave"]) for row in record["rows"]] == [  # noqa: S101
        ("rapids-logger--conda-cpp-build", 1),
        ("rapids-logger--conda-cpp-build", 1),
        ("kvikio--cpp-build", 2),
        ("kvikio--cpp-build", 2),
    ]
    assert all(row["hit"]["run"] == "400" for row in record["rows"])  # noqa: S101
    assert reused == ["rapids-logger--conda-cpp-build", "kvikio--cpp-build"]  # noqa: S101
    assert [outcome for _, _, outcome in outcomes] == ["reused from run 400.1"] * 4  # noqa: S101
    assert not list(store.rglob("reuse-bindings"))  # noqa: S101
    assert not (store / f"train-state/{CURRENT}/rapids-logger").exists()  # noqa: S101


def test_the_release_run_rechecks_and_binds_the_planned_hits(store, tmp_path):
    earlier_run(store, tmp_path)
    plan_train(tmp_path)

    matrices, reused, outcomes, _, planning = apply(tmp_path)

    assert matrices == {}  # noqa: S101
    assert reused == ["rapids-logger--conda-cpp-build", "kvikio--cpp-build"]  # noqa: S101
    assert planning == {"run_id": PLANNING_RUN, "run_attempt": "1"}  # noqa: S101
    assert [outcome for _, _, outcome in outcomes] == ["reused from run 400.1"] * 4  # noqa: S101
    assert bindings(store) == [  # noqa: S101
        "kvikio_conda_cpp_amd64.json",
        "kvikio_conda_cpp_arm64.json",
        "rapids-logger_conda_cpp_amd64.json",
        "rapids-logger_conda_cpp_arm64.json",
    ]


def test_applying_a_partial_plan_gives_the_matrices_the_run_plan_gives(store, tmp_path):
    earlier_run(store, tmp_path, skip_logger_arch="amd64")
    _, planned_outcomes, _ = plan_train(tmp_path)

    matrices, reused, outcomes, _, _ = apply(tmp_path)

    assert matrices == {  # noqa: S101
        "rapids-logger--conda-cpp-build": {"include": rows("amd64")},
        "kvikio--cpp-build": {"include": rows("amd64", "arm64")},
    }
    assert reused == []  # noqa: S101
    assert sorted(outcomes) == sorted(planned_outcomes)  # noqa: S101
    assert bindings(store) == ["rapids-logger_conda_cpp_arm64.json"]  # noqa: S101


def test_a_train_without_a_plan_stops_the_run(store, tmp_path):
    earlier_run(store, tmp_path)
    with pytest.raises(reuse_plan.PlanError, match="has no reuse plan; run Create/Update Train"):
        apply(tmp_path)
    assert bindings(store) == []  # noqa: S101


def test_a_plan_for_another_release_file_stops_the_run(store, tmp_path):
    earlier_run(store, tmp_path)
    plan_train(tmp_path)
    changed = {**LOGGER_JOB, "identity": {**LOGGER_JOB["identity"], "script": "ci/build_conda_v2.sh"}}
    with pytest.raises(reuse_plan.PlanError, match="another release file"):
        apply(tmp_path, {"schema_version": 1, "jobs": [KVIKIO_JOB, changed]})
    assert bindings(store) == []  # noqa: S101


def test_the_newest_plan_is_applied(store, tmp_path):
    # Planned once with nothing to reuse, then again after the earlier run completed.
    plan_train(tmp_path, run="800")
    earlier_run(store, tmp_path)
    plan_train(tmp_path, run=PLANNING_RUN)

    matrices, _, _, _, planning = apply(tmp_path)

    assert matrices == {}  # noqa: S101
    assert planning["run_id"] == PLANNING_RUN  # noqa: S101


def test_a_planned_hit_whose_bytes_changed_stops_the_run_before_binding_it(store, tmp_path):
    earlier_run(store, tmp_path)
    plan_train(tmp_path)
    payload = next(
        path
        for path in store.rglob("*.conda")
        if "artifacts/rapids-logger" in path.as_posix() and "amd64" in path.read_text()
    )
    payload.write_bytes(b"changed")

    with pytest.raises(reuse_plan.PlanError, match=r"no longer holds: .* missing or changed\. Run Create/Update Train"):
        apply(tmp_path)

    assert "rapids-logger_conda_cpp_amd64.json" not in bindings(store)  # noqa: S101
    assert not [name for name in bindings(store) if name.startswith("kvikio")]  # noqa: S101


def test_a_planned_hit_that_lost_its_completion_record_stops_the_run(store, tmp_path):
    earlier_run(store, tmp_path)
    plan_train(tmp_path)
    for completion in store.rglob("completions/400.1.json"):
        completion.unlink()

    with pytest.raises(reuse_plan.PlanError, match="no completion record"):
        apply(tmp_path)
    assert bindings(store) == []  # noqa: S101


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("producer", "sha"), "f" * 40, "producer is not this train's"),
        (("build", "entrypoint"), "conda-python-build-release", "build is not this train's"),
        (("implementation_revisions",), "excluded for development", "implementation_revisions is not this train's"),
        (("runtime_image",), "ghcr.io/rapidsai/release-candidates/ci-conda@sha256:" + "0" * 64, "another CI image"),
    ],
)
def test_a_planned_key_record_that_is_not_this_trains_stops_the_run(store, tmp_path, path, value, message):
    earlier_run(store, tmp_path)
    plan_train(tmp_path)
    record = get(store, PLAN_KEY)
    target = record["rows"][0]["hit"]["key_record"]
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value
    put(store, PLAN_KEY, json.dumps(record).encode())

    with pytest.raises(reuse_plan.PlanError, match=message):
        apply(tmp_path)
    assert "rapids-logger_conda_cpp_amd64.json" not in bindings(store)  # noqa: S101


def test_a_plan_that_misses_a_row_of_the_release_file_stops_the_run(store, tmp_path):
    earlier_run(store, tmp_path)
    plan_train(tmp_path)
    record = get(store, PLAN_KEY)
    record["rows"] = record["rows"][1:]
    put(store, PLAN_KEY, json.dumps(record).encode())
    with pytest.raises(reuse_plan.PlanError, match="does not decide every row"):
        apply(tmp_path)
    assert bindings(store) == []  # noqa: S101


def test_a_repository_not_bound_to_this_run_stops_the_apply(store, tmp_path):
    earlier_run(store, tmp_path)
    plan_train(tmp_path)
    put(store, f"train-state/{CURRENT}/scheduler-state.json", json.dumps({"workflow_runs": {}}).encode())
    with pytest.raises(reuse_plan.PlanError, match="is bound to run None"):
        apply(tmp_path)


def test_planning_holds_its_bindings_where_the_same_run_check_reads_them(store, tmp_path):
    planning = reuse_plan.PlanningStore(S3("candidate-store"))
    base = f"train-state/{CURRENT}/kvikio"
    digest = "a" * 64
    planning.put_once(
        f"{base}/reuse-bindings/0/kvikio_conda_cpp_amd64.json", json.dumps({"build_input_digest": digest}).encode()
    )
    args = argparse.Namespace(
        train_prefix="train-state", train_sha256=CURRENT, repository="rapidsai/kvikio", run_id="0", run_attempt="0"
    )

    assert _current_digest(planning, args, "kvikio_conda_cpp_amd64") == digest  # noqa: S101
    assert not (store / base).exists()  # noqa: S101
    with pytest.raises(reuse_plan.PlanError, match="twice with different content"):
        planning.put_once(f"{base}/reuse-bindings/0/kvikio_conda_cpp_amd64.json", b"{}")


def cli(tmp_path, mode, *extra):
    (tmp_path / "manifest.json").write_text(json.dumps(MANIFEST))
    return [
        "--mode",
        mode,
        "--manifest",
        str(tmp_path / "manifest.json"),
        "--train-sha256",
        CURRENT,
        "--bucket",
        "candidate-store",
        "--summary",
        str(tmp_path / f"{mode}-summary.md"),
        "--server-url",
        "https://github.com",
        "--repository",
        "rapidsai/release-tracking",
        *extra,
    ]


def test_the_cli_plans_a_train_then_applies_its_plan(store, tmp_path, monkeypatch):
    monkeypatch.setattr(reuse_plan, "Boto3S3", S3)
    earlier_run(store, tmp_path, skip_logger_arch="amd64")
    plan_output = tmp_path / "reuse-plan.json"

    reuse_plan.main(
        cli(
            tmp_path,
            "train",
            *("--run-id", PLANNING_RUN, "--run-attempt", "1", "--plan-output", str(plan_output)),
            *("--image-map", json.dumps(IMAGES)),
        )
    )

    assert json.loads(plan_output.read_text()) == get(store, PLAN_KEY)  # noqa: S101
    train_summary = (tmp_path / "train-summary.md").read_text()
    assert "Planned with this train, before its release run" in train_summary  # noqa: S101
    assert (  # noqa: S101
        "| rapids-logger--conda-cpp-build | 13.4.3, 3.12, amd64 | builds: no earlier build of this row |"
        in train_summary
    )

    output = tmp_path / "github-output"
    reuse_plan.main(
        cli(
            tmp_path,
            "apply",
            *("--run-id", RUN, "--run-attempt", "1", "--github-output", str(output)),
            *("--image-map", json.dumps(IMAGES)),
        )
    )

    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads(outputs["matrices"])["rapids-logger--conda-cpp-build"] == {"include": rows("amd64")}  # noqa: S101
    assert json.loads(outputs["reused-jobs"]) == []  # noqa: S101
    assert f"Planned by Create/Update Train run {PLANNING_RUN}.1" in (tmp_path / "apply-summary.md").read_text()  # noqa: S101


@pytest.mark.parametrize(
    ("mode", "missing"),
    [("train", "--run-id, --run-attempt, --plan-output"), ("apply", "--github-output"), ("run", "--image-map")],
)
def test_each_mode_requires_its_arguments(tmp_path, mode, missing):
    extra = () if mode == "train" else ("--run-id", RUN, "--run-attempt", "1")
    with pytest.raises(SystemExit, match=f"--mode {mode} needs {missing}"):
        reuse_plan.main(cli(tmp_path, mode, *extra))


def test_the_plan_is_written_once_per_planning_run(store, tmp_path):
    earlier_run(store, tmp_path)
    plan_train(tmp_path)
    shutil.copy(store / PLAN_KEY, tmp_path / "first.json")
    # Planning the same train again in the same run attempt must not replace what the first attempt recorded.
    plan_train(tmp_path)
    assert (store / PLAN_KEY).read_bytes() == (tmp_path / "first.json").read_bytes()  # noqa: S101
