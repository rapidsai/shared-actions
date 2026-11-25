# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Upstream package selection shared by dependency setup and the reuse plan.

Dependency setup and the reuse plan must select exactly the same upstream packages for a row, because both compute
the row's reuse key from them (plans/per-row-reuse.md, 4b). The selection was a jq program in setup.sh; the parity
test keeps that program as the reference.
"""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_DIRECTORY = Path(__file__).parents[1] / "release-candidate-dependencies"
_SCRIPT = _DIRECTORY / "upstream_selection.py"
sys.path.insert(0, str(_DIRECTORY))
_SPEC = importlib.util.spec_from_file_location("upstream_selection", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"unable to load upstream selection: {_SCRIPT}")
selection = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(selection)
_WHEELS_SPEC = importlib.util.spec_from_file_location("select_wheel", _DIRECTORY / "select_wheel.py")
if _WHEELS_SPEC is None or _WHEELS_SPEC.loader is None:
    raise RuntimeError("unable to load select_wheel.py")
select_wheel = importlib.util.module_from_spec(_WHEELS_SPEC)
_WHEELS_SPEC.loader.exec_module(select_wheel)

# setup.sh's selection before it moved into upstream_selection.py, verbatim apart from two line breaks.
REFERENCE_JQ = r"""
    ($train[0].release_units | map({key: .id, value: .}) | from_entries) as $units
    | def closure($id):
        ($units[$id].dependencies // [])
        | map(select($units[.] != null and $units[.].artifact_family != "source") | ., closure(.))
        | flatten | unique;
    closure($target) as $dependencies
    | (if $family == "docs" or $family == "container" or $family == "maven"
       then ["conda:", "maven:"] else [$family + ":"] end) as $family_prefixes
    | [ $dependencies[]
        | select(. as $dependency | $family_prefixes | any(. as $prefix | $dependency | startswith($prefix))) ]
      as $family_dependencies
    | [ $records[0][]
        | . as $record
        | .entries[]
        | select(.release_catalog_key as $key | $family_dependencies | index($key))
        | select((.package.cuda_major // $cuda_major) == $cuda_major)
        | select(
            $family != "conda"
            or (.package.platform == "noarch" or .package.platform == $conda_platform)
          )
        | select(
            ($family != "maven" and $family != "docs" and $family != "container")
            or (.package.ecosystem != "maven")
            or (.package.platform == ($arch + "-cuda" + $cuda_major))
          )
        | {
          release_catalog_key: .release_catalog_key,
            source: $record.source,
            artifact_key: $record.artifact_key,
            producer_build_input_digest: $record.build_input_digest,
            path: .path,
            package: .package
          }
      ]
    | sort_by([.package.name, .package.version, if .source.matrix.python_version == $python_version then 0 else 1 end])
    | group_by([.package.name, .package.version, .package.build, .package.platform, .path])
    | map(.[0])
"""

TRAIN = {
    "release_units": [
        {"id": "source:logger", "artifact_family": "source", "dependencies": []},
        {"id": "conda:logger", "artifact_family": "conda", "dependencies": ["source:logger"]},
        {"id": "wheel:logger", "artifact_family": "wheel", "dependencies": ["source:logger"]},
        {"id": "maven:jni", "artifact_family": "maven", "dependencies": []},
        {"id": "conda:kvikio", "artifact_family": "conda", "dependencies": ["conda:logger", "source:kvikio"]},
        {"id": "wheel:kvikio", "artifact_family": "wheel", "dependencies": ["wheel:logger"]},
        {"id": "conda:cudf", "artifact_family": "conda", "dependencies": ["conda:kvikio"]},
        {"id": "docs:cudf", "artifact_family": "docs", "dependencies": ["conda:cudf", "maven:jni"]},
    ]
}


def entry(key, path, name, version="1.0", **package):
    return {
        "release_catalog_key": key,
        "path": path,
        "sha256": hashlib.sha256(f"{key}/{path}".encode()).hexdigest(),
        "package": {"name": name, "version": version, **package},
    }


def record(repository, python_version, digest, *entries):
    return {
        "source": {"repository": repository, "sha": "a" * 40, "matrix": {"python_version": python_version}},
        "artifact_key": f"artifacts/{repository.split('/')[-1]}/{digest * 64}",
        "build_input_digest": digest * 64,
        "entries": list(entries),
    }


RECORDS = [
    record(
        "rapidsai/logger",
        "3.12",
        "1",
        entry("conda:logger", "linux-64/logger-1.0-h0.conda", "logger", platform="linux-64", build="h0"),
        entry("conda:logger", "linux-aarch64/logger-1.0-h0.conda", "logger", platform="linux-aarch64", build="h0"),
        entry("conda:logger", "noarch/logger-data-1.0-0.conda", "logger-data", platform="noarch", build="0"),
        entry("conda:logger", "linux-64/logger-cu12-1.0-h1.conda", "logger-cu12", platform="linux-64", cuda_major="12"),
        entry("conda:logger", "linux-64/logger-cu13-1.0-h1.conda", "logger-cu13", platform="linux-64", cuda_major="13"),
    ),
    # The same package from a job with another Python: the 3.12 job's copy wins for a 3.12 row.
    record(
        "rapidsai/logger",
        "3.11",
        "2",
        entry("conda:logger", "linux-64/logger-1.0-h0.conda", "logger", platform="linux-64", build="h0"),
    ),
    record(
        "rapidsai/logger",
        "3.12",
        "3",
        entry("wheel:logger", "logger-1.0-py3-none-manylinux_2_28_x86_64.whl", "logger", ecosystem="wheel"),
        entry("wheel:logger", "logger-1.0-py3-none-manylinux_2_28_aarch64.whl", "logger", ecosystem="wheel"),
        entry("wheel:logger", "logger_data-1.0-py3-none-any.whl", "logger-data", ecosystem="wheel"),
        entry("wheel:logger", "logger_ext-1.0-cp310-cp310-manylinux_2_28_x86_64.whl", "logger-ext", ecosystem="wheel"),
        entry(
            "wheel:logger",
            "logger_cu12-1.0-py3-none-manylinux_2_28_x86_64.whl",
            "logger-cu12",
            ecosystem="wheel",
            cuda_major="12",
        ),
    ),
    record(
        "rapidsai/kvikio",
        "3.12",
        "4",
        entry(
            "conda:kvikio", "linux-64/libkvikio-1.0-cuda13_0.conda", "libkvikio", platform="linux-64", cuda_major="13"
        ),
    ),
    record(
        "rapidsai/jni",
        "3.12",
        "5",
        entry("maven:jni", "jni-amd64-cuda13.tar.gz", "jni", ecosystem="maven", platform="amd64-cuda13"),
        entry("maven:jni", "jni-arm64-cuda13.tar.gz", "jni", ecosystem="maven", platform="arm64-cuda13"),
    ),
]

ROWS = list(
    itertools.product(
        ["conda:kvikio", "wheel:kvikio", "conda:cudf", "docs:cudf", "conda:logger"],
        ["amd64", "arm64"],
        ["12", "13"],
        ["3.12", "3.11"],
    )
)


def _family(target):
    return target.split(":", 1)[0]


def _reference(tmp_path, target, arch, cuda_major, python_version):
    train = tmp_path / "train.json"
    records = tmp_path / "records.json"
    train.write_text(json.dumps(TRAIN))
    records.write_text(json.dumps(RECORDS))
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is not installed")
    result = subprocess.run(  # noqa: S603
        [
            jq,
            "-n",
            "--slurpfile",
            "train",
            str(train),
            "--arg",
            "target",
            target,
            "--arg",
            "family",
            _family(target),
            "--arg",
            "arch",
            arch,
            "--arg",
            "cuda_major",
            cuda_major,
            "--arg",
            "python_version",
            python_version,
            "--arg",
            "conda_platform",
            {"amd64": "linux-64", "arm64": "linux-aarch64"}[arch],
            "--slurpfile",
            "records",
            str(records),
            REFERENCE_JQ,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    chosen = json.loads(result.stdout)
    # setup.sh then skipped wheels its row can't install (select_wheel.py) while downloading.
    return [
        item
        for item in chosen
        if not item["path"].endswith(".whl")
        or (
            select_wheel.supports_python(Path(item["path"]).name, python_version)
            and select_wheel.supports_arch(Path(item["path"]).name, arch)
        )
    ]


@pytest.mark.parametrize(("target", "arch", "cuda_major", "python_version"), ROWS)
def test_selection_matches_the_setup_jq_it_replaces(tmp_path, target, arch, cuda_major, python_version):
    expected = _reference(tmp_path, target, arch, cuda_major, python_version)
    actual = selection.select_upstream(
        TRAIN,
        RECORDS,
        target_unit=target,
        family=_family(target),
        arch=arch,
        cuda_major=cuda_major,
        python_version=python_version,
    )
    assert [{k: v for k, v in item.items() if k != "sha256"} for item in actual] == expected  # noqa: S101


def test_selection_carries_the_catalog_digest_of_each_package():
    chosen = selection.select_upstream(
        TRAIN, RECORDS, target_unit="conda:kvikio", family="conda", arch="amd64", cuda_major="13", python_version="3.12"
    )
    by_path = {item["path"]: item for item in chosen}
    catalog = {(e["path"]): e["sha256"] for r in RECORDS for e in r["entries"]}
    assert by_path  # noqa: S101
    for path, item in by_path.items():
        assert item["sha256"] == catalog[path]  # noqa: S101
    # The 3.12 job's copy wins over the 3.11 job's for a 3.12 row.
    assert by_path["linux-64/logger-1.0-h0.conda"]["artifact_key"].endswith("1" * 64)  # noqa: S101


def test_selection_keeps_only_the_rows_platform_cuda_and_wheel_tags():
    conda = selection.select_upstream(
        TRAIN, RECORDS, target_unit="conda:kvikio", family="conda", arch="arm64", cuda_major="12", python_version="3.12"
    )
    assert sorted(item["path"] for item in conda) == [  # noqa: S101
        "linux-aarch64/logger-1.0-h0.conda",
        "noarch/logger-data-1.0-0.conda",
    ]
    wheels = selection.select_upstream(
        TRAIN, RECORDS, target_unit="wheel:kvikio", family="wheel", arch="arm64", cuda_major="13", python_version="3.12"
    )
    assert sorted(item["path"] for item in wheels) == [  # noqa: S101
        "logger-1.0-py3-none-manylinux_2_28_aarch64.whl",
        "logger_data-1.0-py3-none-any.whl",
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"arch": "ppc64le"},
        {"family": "wheel", "target_unit": "wheel:kvikio", "python_version": ""},
        {"cuda_major": "thirteen"},
    ],
)
def test_selection_rejects_inputs_it_cannot_honour(overrides):
    arguments = {
        "target_unit": "conda:kvikio",
        "family": "conda",
        "arch": "amd64",
        "cuda_major": "13",
        "python_version": "3.12",
        **overrides,
    }
    with pytest.raises(ValueError, match=r"."):
        selection.select_upstream(TRAIN, RECORDS, **arguments)


def test_selection_rejects_a_catalog_entry_without_a_digest():
    records = json.loads(json.dumps(RECORDS))
    del records[0]["entries"][0]["sha256"]
    with pytest.raises(ValueError, match="sha256"):
        selection.select_upstream(
            TRAIN,
            records,
            target_unit="conda:kvikio",
            family="conda",
            arch="amd64",
            cuda_major="13",
            python_version="3.12",
        )


def test_cli_writes_the_selection(tmp_path):
    (tmp_path / "train.json").write_text(json.dumps(TRAIN))
    (tmp_path / "records.json").write_text(json.dumps(RECORDS))
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(_SCRIPT),
            "--train",
            str(tmp_path / "train.json"),
            "--records",
            str(tmp_path / "records.json"),
            "--target-unit",
            "conda:kvikio",
            "--family",
            "conda",
            "--arch",
            "amd64",
            "--cuda-major",
            "13",
            "--python-version",
            "3.12",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert json.loads(result.stdout) == selection.select_upstream(  # noqa: S101
        TRAIN, RECORDS, target_unit="conda:kvikio", family="conda", arch="amd64", cuda_major="13", python_version="3.12"
    )
