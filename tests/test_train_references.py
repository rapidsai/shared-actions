# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Which train references a build row reads, and their catalogs: train_references.py, for setup and the reuse plan.

26.12 release run 46094059402 spent about 150 of a rapidsmpf row's 176 seconds of dependency setup fetching, one at
a time, catalogs of every repository in the train, cucim's and ci-imgs' among them, which the row can never install.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "release-candidate-dependencies"))

import train_references  # noqa: E402

TRAIN_SHA = "a" * 64
RUN = "500"
ROOT = Path(__file__).parents[1]
SETUP = ROOT / "release-candidate-dependencies" / "setup.sh"


def unit(unit_id, repository, *dependencies):
    family = unit_id.split(":", 1)[0]
    return {"id": unit_id, "artifact_family": family, "repository": repository, "dependencies": list(dependencies)}


TRAIN = {
    "release_units": [
        unit("source:rmm", "rmm"),
        unit("conda:rapids-logger", "rapids-logger"),
        unit("conda:rmm", "rmm", "source:rmm", "conda:rapids-logger"),
        unit("wheel:rmm", "rmm"),
        unit("conda:kvikio", "kvikio"),
        unit("conda:rapidsmpf", "rapidsmpf", "conda:rmm", "conda:kvikio", "wheel:rmm"),
        unit("conda:cucim", "cucim"),
        unit("wheel:rapidsmpf", "rapidsmpf", "wheel:rmm"),
    ]
}


def test_a_row_reads_only_the_repositories_of_its_familys_dependency_closure():
    assert train_references.needed_repositories(TRAIN, "conda:rapidsmpf", "conda") == {  # noqa: S101
        "rmm",
        "rapids-logger",
        "kvikio",
    }
    assert train_references.needed_repositories(TRAIN, "wheel:rapidsmpf", "wheel") == {"rmm"}  # noqa: S101
    assert train_references.needed_repositories(TRAIN, "conda:rapids-logger", "conda") == set()  # noqa: S101


def test_a_train_that_does_not_name_a_units_repository_keeps_every_repository():
    train = {"release_units": [*TRAIN["release_units"][:-1], {"id": "conda:extra", "artifact_family": "conda"}]}
    train["release_units"] = [
        {**entry, "dependencies": [*entry["dependencies"], "conda:extra"]} if entry["id"] == "conda:rapidsmpf" else entry
        for entry in train["release_units"]
    ]
    assert train_references.needed_repositories(train, "conda:rapidsmpf", "conda") is None  # noqa: S101


def reference(root, repository, artifact, *, run=RUN, attempt="1", digest=None, **fields):
    digest = digest or (repository[:1] * 63 + attempt)[-64:].replace(repository[:1], "b")
    document = {
        "schema_version": 4,
        "storage_layout": 2,
        "train_sha256": TRAIN_SHA,
        "repository": f"rapidsai/{repository}",
        "source_artifact": artifact,
        "source_run_id": run,
        "source_run_attempt": attempt,
        "build_input_digest": digest,
        "artifact_key": f"artifacts/{repository}/{digest}",
        "bundle_key": f"train-state/{TRAIN_SHA}/{repository}/{run}.{attempt}/{digest}/release-catalog-entries.{artifact}.json",
        **fields,
    }
    path = root / repository / f"{run}.{attempt}" / digest / f"bundle-reference.{artifact}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    return document


@pytest.fixture
def environment(tmp_path):
    state = tmp_path / "scheduler-state.json"
    state.write_text(json.dumps({"workflow_runs": {name: RUN for name in ("rmm", "kvikio", "cucim", "rapids-logger")}}))
    (tmp_path / "bindings").mkdir()
    return {
        **os.environ,
        "GITHUB_RUN_ID": RUN,
        "RELEASE_CANDIDATE_BUCKET": "candidate-store",
        "RELEASE_CANDIDATE_TRAIN_PREFIX": "train-state",
        "RELEASE_CANDIDATE_TRAIN_SHA256": TRAIN_SHA,
        "SCHEDULER_STATE": str(state),
        "REUSE_BINDINGS": str(tmp_path / "bindings"),
    }


def test_other_repositories_are_skipped_before_anything_reads_them(tmp_path, environment):
    references = tmp_path / "references"
    reference(references, "rmm", "rmm_conda_cpp_x86_64")
    # A reference setup would refuse, in a repository the row can't install from: never read, never refused.
    reference(references, "cucim", "cucim_conda_python_x86_64", source_run_attempt="x")
    kept = train_references.select_references(references, repositories={"rmm"}, environment=environment)
    assert [path.parent.parent.parent.name for path in kept] == ["rmm"]  # noqa: S101
    with pytest.raises(train_references.TrainReferenceError, match="invalid selected run attempt"):
        train_references.select_references(references, repositories=None, environment=environment)


def test_the_newest_bound_attempt_is_kept_and_other_runs_are_dropped(tmp_path, environment):
    references = tmp_path / "references"
    reference(references, "rmm", "rmm_conda_cpp_x86_64", attempt="1", digest="1" * 64)
    reference(references, "rmm", "rmm_conda_cpp_x86_64", attempt="2", digest="2" * 64)
    reference(references, "rmm", "rmm_conda_cpp_x86_64", run="499", digest="3" * 64)
    kept = train_references.select_references(references, repositories={"rmm"}, environment=environment)
    assert [json.loads(path.read_text())["build_input_digest"] for path in kept] == ["2" * 64]  # noqa: S101


def test_a_reference_setup_must_refuse_stops_the_selection(tmp_path, environment):
    references = tmp_path / "references"
    reference(references, "rmm", "rmm_conda_cpp_x86_64", artifact_key="artifacts/rmm/../x")
    with pytest.raises(train_references.TrainReferenceError, match="unsafe artifact reference"):
        train_references.select_references(references, repositories={"rmm"}, environment=environment)
    other = tmp_path / "other"
    path = other / "rmm/500.1" / ("c" * 64) / "bundle-reference.wrong_name.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 4, "storage_layout": 2, "source_artifact": "rmm_conda_cpp_x86_64"}))
    with pytest.raises(train_references.TrainReferenceError, match="filename does not match"):
        train_references.select_references(other, repositories={"rmm"}, environment=environment)


def test_catalogs_are_fetched_only_for_kept_references_in_setups_order(tmp_path, environment):
    references = tmp_path / "references"
    documents = [
        reference(references, "rmm", "rmm_conda_cpp_x86_64", digest="d" * 64),
        reference(references, "kvikio", "kvikio_conda_cpp_x86_64", digest="e" * 64),
    ]
    reference(references, "cucim", "cucim_conda_python_x86_64", digest="f" * 64)
    fetched = []

    def fetch(key):
        fetched.append(key)
        return json.dumps({"entries": [], "source": {"key": key}}).encode()

    kept = train_references.select_references(references, repositories={"rmm", "kvikio"}, environment=environment)
    records = train_references.catalog_records(references, kept, fetch=fetch, environment=environment)
    assert sorted(fetched) == sorted(document["bundle_key"] for document in documents)  # noqa: S101
    # Sorted as setup's glob listed them: by directory, kvikio before rmm.
    assert [record["artifact_key"] for record in records] == ["artifacts/kvikio/" + "e" * 64, "artifacts/rmm/" + "d" * 64]  # noqa: S101
    assert records[0]["build_input_digest"] == "e" * 64  # noqa: S101


def test_cli_fails_when_the_rows_family_dependencies_left_no_catalog(tmp_path, environment, capsys, monkeypatch):
    train = tmp_path / "release-train.json"
    train.write_text(json.dumps(TRAIN))
    references = tmp_path / "references"
    references.mkdir()
    reference(references, "cucim", "cucim_conda_python_x86_64")
    output = tmp_path / "records.json"
    for key in ("GITHUB_RUN_ID", "SCHEDULER_STATE", "REUSE_BINDINGS") + tuple(k for k in environment if k.startswith("RELEASE_")):
        monkeypatch.setenv(key, environment[key])
    arguments = ["--references", str(references), "--train", str(train), "--output", str(output)]
    assert train_references.main([*arguments, "--target-unit", "conda:rapidsmpf", "--family", "conda"]) == 1  # noqa: S101
    assert "no completed candidate catalog envelopes found for conda:rapidsmpf" in capsys.readouterr().err  # noqa: S101
    assert not output.exists()  # noqa: S101
    # A row with no dependency of its family needs no catalog.
    assert train_references.main([*arguments, "--target-unit", "conda:rapids-logger", "--family", "conda"]) == 0  # noqa: S101
    assert json.loads(output.read_text()) == []  # noqa: S101


def test_setup_reads_its_catalogs_through_train_references():
    setup = SETUP.read_text()
    assert 'python3 "${script_directory}/train_references.py"' in setup  # noqa: S101
    assert "--target-unit \"${candidate_unit_id}\"" in setup  # noqa: S101
    assert "--family \"${RELEASE_CANDIDATE_ARTIFACT_FAMILY}\"" in setup  # noqa: S101
    # Every catalog it reads comes from there; setup no longer fetches them one at a time itself.
    assert "release-catalog-entries.*.json" not in setup  # noqa: S101
    module = (ROOT / "release-candidate-dependencies" / "train_references.py").read_text()
    assert '"reference-binding.sh"' in module and '"catalog-bundle-key.sh"' in module  # noqa: S101
