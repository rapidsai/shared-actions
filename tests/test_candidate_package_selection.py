# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# ruff: noqa: S101
"""Exercise the actual resolver selector with V5's admitted RDD metadata."""

import json
import subprocess
from pathlib import Path

import pytest

SETUP = Path(__file__).parents[1] / "release-candidate-dependencies/setup.sh"


def selection_program():
    text = SETUP.read_text().split("# Package metadata determines compatibility.", maxsplit=1)[1]
    return text.split("\n  '\n", maxsplit=1)[1].split('\n  \' >"${selected}"', maxsplit=1)[0]


@pytest.fixture
def v5_records():
    """Preserve the package identity, source scope, and canonical V5 input keys."""
    digest = "a6f26299af6317344abfedc1ac89c66f7e8a0c2d487963325ed4cbc7cc1bf652"
    source = {"matrix": {"arch": "amd64", "cuda_version": "13.3.0", "python_version": "3.14"}}
    return [
        {
            "source": source,
            "artifact_key": f"artifacts/rapids-dask-dependency/{digest}",
            "build_input_digest": digest,
            "entries": [
                {
                    "release_catalog_key": "conda:rapids-dask-dependency",
                    "path": "noarch/rapids-dask-dependency-26.12.00-261014184436_5ced47f7.conda",
                    "package": {
                        "ecosystem": "conda",
                        "name": "rapids-dask-dependency",
                        "version": "26.12.00",
                        "build": "261014184436_5ced47f7",
                        "platform": "noarch",
                    },
                }
            ],
        },
        {
            "source": source,
            "artifact_key": f"artifacts/rapids-dask-dependency/{'b' * 64}",
            "build_input_digest": "b" * 64,
            "entries": [
                {
                    "release_catalog_key": "wheel:rapids-dask-dependency",
                    "path": "rapids_dask_dependency-26.12.0-py3-none-any.whl",
                    "package": {"ecosystem": "wheel", "name": "rapids-dask-dependency", "version": "26.12.0"},
                }
            ],
        },
    ]


@pytest.fixture
def train():
    units = [
        {"id": "source:dask-cuda", "artifact_family": "source", "dependencies": []},
        {"id": "source:rapids-dask-dependency", "artifact_family": "source", "dependencies": []},
        {
            "id": "conda:rapids-dask-dependency",
            "artifact_family": "conda",
            "dependencies": ["source:rapids-dask-dependency"],
        },
        {
            "id": "wheel:rapids-dask-dependency",
            "artifact_family": "wheel",
            "dependencies": ["source:rapids-dask-dependency"],
        },
        {
            "id": "conda:dask-cuda",
            "artifact_family": "conda",
            "dependencies": ["source:dask-cuda", "conda:rapids-dask-dependency"],
        },
        {
            "id": "wheel:dask-cuda",
            "artifact_family": "wheel",
            "dependencies": ["source:dask-cuda", "wheel:rapids-dask-dependency"],
        },
    ]
    return {"release_units": units}


def select(
    tmp_path, train, records, *, family="conda", target="conda:dask-cuda", arch="amd64", cuda="13", python="3.14"
):
    train_path = tmp_path / "train.json"
    records_path = tmp_path / "records.json"
    train_path.write_text(json.dumps(train))
    records_path.write_text(json.dumps(records))
    command = ["jq", "-n", "--slurpfile", "train", str(train_path), "--slurpfile", "records", str(records_path)]
    for name, value in {
        "target": target,
        "family": family,
        "arch": arch,
        "cuda_major": cuda,
        "python_version": python,
        "conda_platform": "linux-64" if arch == "amd64" else "linux-aarch64",
    }.items():
        command.extend(["--arg", name, value])
    command.append(selection_program())
    response = subprocess.run(command, text=True, capture_output=True, check=True)  # noqa: S603 - reviewed jq, no shell.
    return json.loads(response.stdout)


def test_v5_dask_cuda_selects_admitted_noarch_rdd(tmp_path, train, v5_records):
    selected = select(tmp_path, train, v5_records)
    assert len(selected) == 1
    assert selected[0]["release_catalog_key"] == "conda:rapids-dask-dependency"
    assert selected[0]["package"]["version"] == "26.12.00"
    assert selected[0]["path"].startswith("noarch/")
    assert selected[0]["artifact_key"] == v5_records[0]["artifact_key"]


def test_noarch_metadata_outweighs_producer_job_scope(tmp_path, train, v5_records):
    selected = select(tmp_path, train, v5_records, arch="arm64", cuda="12", python="3.12")
    assert [entry["release_catalog_key"] for entry in selected] == ["conda:rapids-dask-dependency"]


def test_wheel_dependency_never_borrows_conda_family(tmp_path, train, v5_records):
    selected = select(tmp_path, train, v5_records, family="wheel", target="wheel:dask-cuda")
    assert [entry["release_catalog_key"] for entry in selected] == ["wheel:rapids-dask-dependency"]


@pytest.mark.parametrize("family", ["maven", "docs", "container"])
def test_combined_families_include_declared_conda_and_maven_only(tmp_path, train, v5_records, family):
    target = f"{family}:consumer"
    train["release_units"].extend(
        [
            {"id": "maven:upstream", "artifact_family": "maven", "dependencies": []},
            {
                "id": target,
                "artifact_family": family,
                "dependencies": ["conda:rapids-dask-dependency", "maven:upstream", "wheel:rapids-dask-dependency"],
            },
        ]
    )
    v5_records.append(
        {
            "source": {"matrix": {}},
            "artifact_key": f"artifacts/upstream/{'c' * 64}",
            "build_input_digest": "c" * 64,
            "entries": [
                {
                    "release_catalog_key": "maven:upstream",
                    "path": "upstream.tar.gz",
                    "package": {
                        "ecosystem": "maven",
                        "name": "upstream",
                        "version": "26.12.0",
                        "platform": "amd64-cuda13",
                    },
                }
            ],
        }
    )
    selected = select(tmp_path, train, v5_records, family=family, target=target)
    assert {entry["release_catalog_key"] for entry in selected} == {"conda:rapids-dask-dependency", "maven:upstream"}


def test_transitive_closure_excludes_undeclared_catalogs(tmp_path, train, v5_records):
    train["release_units"].extend(
        [
            {"id": "conda:consumer", "artifact_family": "conda", "dependencies": ["conda:dask-cuda"]},
            {"id": "conda:unrelated", "artifact_family": "conda", "dependencies": []},
        ]
    )
    v5_records[0]["entries"].append(
        {
            "release_catalog_key": "conda:unrelated",
            "path": "noarch/unrelated.conda",
            "package": {"name": "unrelated", "version": "26.12.00", "platform": "noarch"},
        }
    )
    selected = select(tmp_path, train, v5_records, target="conda:consumer")
    assert [entry["release_catalog_key"] for entry in selected] == ["conda:rapids-dask-dependency"]


@pytest.mark.parametrize("field,value", [("cuda_major", "12"), ("platform", "linux-aarch64")])
def test_package_incompatibility_still_excludes_inputs(tmp_path, train, v5_records, field, value):
    v5_records[0]["entries"][0]["package"][field] = value
    assert select(tmp_path, train, v5_records) == []
