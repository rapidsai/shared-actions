# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""CI image rows as the reuse plan's first wave (plans/per-row-reuse.md, 4a)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

_DIRECTORY = Path(__file__).parents[1] / "release-candidate-dependencies"
sys.path.insert(0, str(_DIRECTORY))
sys.path.insert(0, str(Path(__file__).parent))

import reuse_plan  # noqa: E402
from row_reuse import S3  # noqa: E402
from test_reuse_plan import (  # noqa: E402
    CURRENT,
    EARLIER,
    IMAGES,
    KVIKIO_JOB,
    LOGGER_JOB,
    RUN,
    TRAIN,
    cli_arguments,
    earlier_run,
    get,
    make_store,
    put,
    train_digest,
)

IMAGE_JOB_ID = "ci-imgs--candidate-images"


@pytest.fixture
def store(tmp_path, monkeypatch):
    return make_store(tmp_path, monkeypatch)


@pytest.fixture
def cli_store(monkeypatch):
    # The plan runs on boto3 (s3_boto.py); here it runs on the fake AWS CLI store.
    monkeypatch.setattr(reuse_plan, "Boto3S3", S3)


def image_rows(*arches):
    return [
        {
            "ARCH": arch,
            "CUDA_VER": "13.4.3",
            "PYTHON_VER": "3.12",
            "LINUX_VER": "rockylinux8",
            "IMAGE_REPO": "ci-conda",
            "IMAGE_NAME": "rapidsai/ci-conda:13",
            "DOCKERFILE": "ci-conda.Dockerfile",
            "DOCKER_TARGET": "",
        }
        for arch in arches
    ]


IMAGE_JOB = {
    "id": IMAGE_JOB_ID,
    "repository": "rapidsai/ci-imgs",
    "project": "ci-imgs",
    "unit": "container:ci-images",
    "family": "container",
    "entrypoint": "ci-images-build-release",
    "identity": {"rapids-version": "26.12"},
    "needs": [],
    "matrix": {"include": image_rows("amd64", "arm64")},
}
MANIFEST = {"schema_version": 1, "jobs": [KVIKIO_JOB, LOGGER_JOB, IMAGE_JOB]}


def image_key_for(tmp_path, row, train=TRAIN):
    workspace = tmp_path / "image-key-work"
    workspace.mkdir(exist_ok=True)
    return reuse_plan._image_row_key(IMAGE_JOB, row, train, workspace)


def image_record(row, digest):
    return {
        "schema_version": 1,
        "registry": "ghcr.io",
        "repository": f"rapidsai/release-candidates/{row['IMAGE_REPO']}",
        "digest": digest,
        "media_type": "application/vnd.oci.image.manifest.v1+json",
        "config_digest": "sha256:" + "c" * 64,
        "platform": f"linux/{row['ARCH']}",
        "size": 1234,
    }


def index_image(root, tmp_path, row, *, record=None, train=TRAIN, run="400", attempt="1", complete=True):
    """An earlier run that built the image row, indexed it under its key and filed its history, as the upload does."""
    key, key_record = image_key_for(tmp_path, row, train)
    image = IMAGES[f"ci-conda:13-{row['ARCH']}"]
    record = record or image_record(row, image.split("@", 1)[1])
    artifact = f"ci-images-{row['ARCH']}"
    digest = hashlib.sha256(f"ci-imgs/{row['ARCH']}/{run}.{attempt}".encode()).hexdigest()
    artifact_key = f"artifacts/ci-imgs/{digest}"
    path = f"image-ci-conda-13-{row['ARCH']}.oci-image.json"
    payload_sha = put(root, f"{artifact_key}/{path}", json.dumps(record).encode())
    index_sha = put(
        root,
        f"{artifact_key}/artifact-index.json",
        json.dumps(
            {
                "schema_version": 3,
                "build_input_digest": digest,
                "source_artifact": artifact,
                "entries": [{"path": path, "sha256": payload_sha}],
            }
        ).encode(),
    )
    record_sha = put(root, f"{artifact_key}/build-record.json", b'{"record": true}')
    bundle_key = f"train-state/{EARLIER}/ci-imgs/{run}.{attempt}/{digest}/release-catalog-entries.{artifact}.json"
    catalog_sha = put(root, bundle_key, json.dumps({"entries": [{"path": path, "sha256": payload_sha}]}).encode())
    reference = {
        "schema_version": 4,
        "producer": "shared-workflows",
        "storage_layout": 2,
        "train_sha256": EARLIER,
        "repository": "rapidsai/ci-imgs",
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
        f"artifacts/ci-imgs/row-reuse-index/{key}/{artifact}/{run}-{attempt}.json",
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
    put(
        root,
        f"artifacts/ci-imgs/row-reuse-history/{reuse_plan.row_digest(key_record)}/{artifact}/{run}-{attempt}.json",
        json.dumps(
            {
                "schema_version": 1,
                "row_reuse_key": key,
                "key_record": key_record,
                "train_sha256": EARLIER,
                "run_id": run,
                "run_attempt": attempt,
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


def plan(tmp_path, manifest=MANIFEST, *, train_sha256=CURRENT, image_map=None, s3=None):
    workspace = tmp_path / "plan-work"
    workspace.mkdir(exist_ok=True)
    return reuse_plan.plan(
        s3 or S3("candidate-store"),
        manifest,
        train_sha256=train_sha256,
        run_id=RUN,
        run_attempt="1",
        image_map={} if image_map is None else image_map,
        workspace=workspace,
    )


def store_train(root, train):
    digest = train_digest(train)
    put(root, f"train-state/{digest}/release-train.json", json.dumps({**train, "train_sha256": digest}).encode())
    put(
        root,
        f"train-state/{digest}/scheduler-state.json",
        json.dumps({"workflow_runs": {"rapids-logger": RUN, "kvikio": RUN, "ci-imgs": RUN}}).encode(),
    )
    return digest


def test_every_image_reused_fills_the_image_map_and_decides_every_package_row_in_one_plan(store, tmp_path):
    for row in IMAGE_JOB["matrix"]["include"]:
        index_image(store, tmp_path, row)
    earlier_run(store, tmp_path)
    matrices, reused, outcomes, image_map = plan(tmp_path)
    assert matrices == {}  # noqa: S101
    assert reused == [IMAGE_JOB_ID, "rapids-logger--conda-cpp-build", "kvikio--cpp-build"]  # noqa: S101
    # No verify-ci-imgs: the map comes straight from the earlier run's image records.
    assert image_map == IMAGES  # noqa: S101
    assert (IMAGE_JOB_ID, "ci-conda 13.4.3, 3.12, amd64", "reused from run 400.1") in outcomes  # noqa: S101
    binding = get(store, f"train-state/{CURRENT}/ci-imgs/reuse-bindings/{RUN}/ci-images-amd64.json")
    assert binding["source_run_id"] == "400"  # noqa: S101


def test_a_changed_base_digest_rebuilds_that_image_and_names_it_on_the_rows_that_run_in_it(store, tmp_path):
    old = json.loads(json.dumps(TRAIN))
    old["candidate_image_bases"]["ci-conda:13-amd64"]["CUDA_BASE_IMAGE"] = (
        "docker.io/nvidia/cuda:13.4.3-base-rockylinux8@sha256:" + "d" * 64
    )
    index_image(store, tmp_path, image_rows("amd64")[0], train=old)
    index_image(store, tmp_path, image_rows("arm64")[0])
    earlier_run(store, tmp_path)
    matrices, reused, outcomes, image_map = plan(tmp_path)
    assert matrices[IMAGE_JOB_ID] == {"include": image_rows("amd64")}  # noqa: S101
    assert image_map == {"ci-conda:13-arm64": IMAGES["ci-conda:13-arm64"]}  # noqa: S101
    assert (  # noqa: S101
        IMAGE_JOB_ID,
        "ci-conda 13.4.3, 3.12, amd64",
        "builds: base image: CUDA_BASE_IMAGE sha256:ddddddd → sha256:eeeeeee",
    ) in outcomes
    by_row = {(job, row): outcome for job, row, outcome in outcomes}
    assert by_row[(LOGGER_JOB["id"], "13.4.3, 3.12, amd64")] == "builds: CI image: ci-conda:13-amd64 builds"  # noqa: S101
    # The arm64 row's image was reused, so the row is decided in the plan.
    assert by_row[(LOGGER_JOB["id"], "13.4.3, 3.12, arm64")] == "reused from run 400.1"  # noqa: S101
    assert by_row[(KVIKIO_JOB["id"], "13.4.3, 3.12, amd64")] == "builds: CI image: ci-conda:13-amd64 builds"  # noqa: S101
    assert matrices[LOGGER_JOB["id"]]["include"] == [LOGGER_JOB["matrix"]["include"][0]]  # noqa: S101
    # kvikio waits for rapids-logger's job, so both of its rows build, in their matrix order.
    assert matrices[KVIKIO_JOB["id"]] == KVIKIO_JOB["matrix"]  # noqa: S101
    assert reused == []  # noqa: S101


def test_a_changed_base_tag_is_named_by_its_tag(store, tmp_path):
    old = json.loads(json.dumps(TRAIN))
    old["candidate_image_bases"]["ci-conda:13-amd64"]["MINIFORGE_BASE_IMAGE"] = (
        "docker.io/condaforge/miniforge3:26.3.0-0@sha256:" + "f" * 64
    )
    index_image(store, tmp_path, image_rows("amd64")[0], train=old)
    _, _, outcomes, _ = plan(tmp_path)
    assert (  # noqa: S101
        IMAGE_JOB_ID,
        "ci-conda 13.4.3, 3.12, amd64",
        "builds: base image: MINIFORGE_BASE_IMAGE docker.io/condaforge/miniforge3:26.3.0-0 →"
        " docker.io/condaforge/miniforge3:26.9.4-0",
    ) in outcomes


def test_an_image_refresh_rebuilds_every_image(store, tmp_path):
    refresh = {**TRAIN, "candidate_image_policy": {"schema_version": 1, "mode": "refresh", "refresh_key": ""}}
    digest = store_train(store, refresh)
    for row in IMAGE_JOB["matrix"]["include"]:
        index_image(store, tmp_path, row, train=refresh)
    matrices, _, outcomes, image_map = plan(tmp_path, train_sha256=digest)
    assert matrices[IMAGE_JOB_ID] == IMAGE_JOB["matrix"]  # noqa: S101
    assert image_map == {}  # noqa: S101
    assert {outcome for job, _, outcome in outcomes if job == IMAGE_JOB_ID} == {  # noqa: S101
        "builds: image refresh requested (candidate_image_policy.mode: refresh)"
    }


def test_a_train_without_base_digests_builds_its_images_without_a_key(store, tmp_path):
    digest = store_train(store, {key: value for key, value in TRAIN.items() if key != "candidate_image_bases"})
    matrices, _, outcomes, _ = plan(tmp_path, train_sha256=digest)
    assert matrices[IMAGE_JOB_ID] == IMAGE_JOB["matrix"]  # noqa: S101
    assert {outcome for job, _, outcome in outcomes if job == IMAGE_JOB_ID} == {  # noqa: S101
        "builds: no reuse key: the train records no base image digests"
    }


def test_a_train_missing_one_images_bases_stops_the_plan(store, tmp_path):
    partial = json.loads(json.dumps(TRAIN))
    del partial["candidate_image_bases"]["ci-conda:13-arm64"]
    digest = store_train(store, partial)
    with pytest.raises(reuse_plan.PlanError, match="no base images for ci-conda:13-arm64"):
        plan(tmp_path, train_sha256=digest)


def test_a_reused_image_that_disagrees_with_the_given_map_stops_the_plan(store, tmp_path):
    for row in IMAGE_JOB["matrix"]["include"]:
        index_image(store, tmp_path, row)
    other = {"ci-conda:13-amd64": "ghcr.io/rapidsai/release-candidates/ci-conda@sha256:" + "1" * 64}
    with pytest.raises(reuse_plan.PlanError, match="the given image map names"):
        plan(tmp_path, image_map=other)


@pytest.mark.parametrize(
    "change",
    [
        {"repository": "rapidsai/release-candidates/ci-wheel"},
        {"registry": "docker.io"},
        {"platform": "linux/arm64"},
        {"digest": "sha256:short"},
    ],
)
def test_an_image_record_that_is_not_the_rows_image_stops_the_plan_before_binding(store, tmp_path, change):
    row = image_rows("amd64")[0]
    index_image(store, tmp_path, row, record={**image_record(row, "sha256:" + "6" * 64), **change})
    with pytest.raises(reuse_plan.PlanError, match="image record"):
        plan(tmp_path)
    assert not (store / f"train-state/{CURRENT}/ci-imgs/reuse-bindings").exists()  # noqa: S101


@pytest.mark.parametrize(
    ("jobs", "message"),
    [
        ([{**IMAGE_JOB, "needs": [LOGGER_JOB["id"]]}, LOGGER_JOB], "need no job"),
        ([IMAGE_JOB, {**LOGGER_JOB, "needs": [IMAGE_JOB_ID]}], "through candidate_image_key"),
        ([{**IMAGE_JOB, "matrix": {"include": [{"ARCH": "amd64"}]}}], "every row needs"),
        ([{**IMAGE_JOB, "matrix": {"include": image_rows("amd64", "amd64")}}], "repeat an image"),
        (
            [{**IMAGE_JOB, "matrix": {"include": [{**image_rows("amd64")[0], "IMAGE_NAME": "rapidsai/ci-wheel:13"}]}}],
            "is not rapidsai/ci-conda",
        ),
        ([IMAGE_JOB, {**IMAGE_JOB, "id": "ci-imgs--again"}], "build the same image"),
    ],
)
def test_a_malformed_image_job_stops_the_plan(store, tmp_path, jobs, message):
    with pytest.raises(reuse_plan.PlanError, match=message):
        plan(tmp_path, {"schema_version": 1, "jobs": jobs})


def test_cli_writes_the_image_map(store, tmp_path, cli_store):
    for row in IMAGE_JOB["matrix"]["include"]:
        index_image(store, tmp_path, row)
    output, summary = tmp_path / "github-output", tmp_path / "summary.md"
    assert reuse_plan.main(cli_arguments(tmp_path, MANIFEST, {}, output, summary)) == 0  # noqa: S101
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads(outputs["image-map"]) == IMAGES  # noqa: S101
    assert IMAGE_JOB_ID in json.loads(outputs["reused-jobs"])  # noqa: S101
    assert "| ci-imgs--candidate-images | ci-conda 13.4.3, 3.12, amd64 | reused from [run 400.1]" in summary.read_text()  # noqa: S101


def test_the_action_outputs_the_image_map_and_defaults_to_an_empty_given_map():
    import yaml

    action = yaml.safe_load((Path(__file__).parents[1] / "candidate-reuse-plan" / "action.yml").read_text())
    assert action["outputs"]["image-map"]["value"] == "${{ steps.plan.outputs.image-map }}"  # noqa: S101
    assert action["inputs"]["image-map"]["default"] == "{}"  # noqa: S101
