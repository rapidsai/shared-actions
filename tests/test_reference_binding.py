# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Which train references a build may consume: the repository's bound run, or a row reused for this run."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
_SCRIPT = ROOT / "release-candidate-dependencies" / "reference-binding.sh"
_SETUP = ROOT / "release-candidate-dependencies" / "setup.sh"
TRAIN = "a" * 64
EARLIER = "c" * 64
DIGEST = "b" * 64
RUN = "500"
REPOSITORY = "rapidsai/kvikio"
ARTIFACT = "kvikio_conda_cpp_x86_64"

FAKE_AWS = """#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

root = Path(os.environ["FAKE_S3_ROOT"])
args = sys.argv[1:]
with open(os.environ["AWS_CALLS"], "a") as calls:
    calls.write(" ".join(args) + "\\n")
if args[:2] == ["s3api", "list-objects-v2"]:
    bucket = args[args.index("--bucket") + 1]
    prefix = args[args.index("--prefix") + 1]
    base = root / bucket
    keys = sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())
    print(json.dumps({"Contents": [{"Key": k} for k in keys if k.startswith(prefix)]}))
elif args[:2] == ["s3", "cp"]:
    source = root / args[2].removeprefix("s3://")
    if not source.exists():
        print("fatal error: An error occurred (404) when calling the HeadObject operation", file=sys.stderr)
        sys.exit(1)
    shutil.copyfile(source, args[3])
else:
    print("unexpected aws call: " + " ".join(args), file=sys.stderr)
    sys.exit(2)
"""


def _reference(**fields):
    reference = {
        "schema_version": 4,
        "storage_layout": 2,
        "train_sha256": TRAIN,
        "repository": REPOSITORY,
        "source_artifact": ARTIFACT,
        "source_run_id": RUN,
        "source_run_attempt": "1",
        "build_input_digest": DIGEST,
    }
    reference.update(fields)
    return reference


def _reused(source_run="400", attempt="1", source_train=EARLIER, **fields):
    return _reference(
        source_run_id=source_run,
        source_run_attempt=attempt,
        reuse={"source_train_sha256": source_train, "artifact_objects": {"k": "v"}},
        **fields,
    )


def _binding(source_run="400", attempt="1", **fields):
    binding = {
        "schema_version": 1,
        "train_sha256": TRAIN,
        "repository": REPOSITORY,
        "artifact": ARTIFACT,
        "run_id": RUN,
        "source_run_id": source_run,
        "source_run_attempt": attempt,
        "build_input_digest": DIGEST,
    }
    binding.update(fields)
    return binding


def _completion(run="400", attempt="1", train=EARLIER, **fields):
    record = {"schema_version": 1, "train_sha256": train, "run_id": run, "run_attempt": attempt, "result": "complete"}
    record.update(fields)
    return record


def _run(tmp_path, reference, *, workflow_runs=None, binding=None, completions=(), run=RUN):
    s3 = tmp_path / "s3" / "candidate-store"
    for record in completions:
        key = s3 / f"train-state/{record['train_sha256']}/completions/{record['run_id']}.{record['run_attempt']}.json"
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text(json.dumps(record))
    bindings = tmp_path / "bindings"
    if binding is not None:
        path = bindings / f"kvikio/reuse-bindings/{run}/{ARTIFACT}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(binding))
    state = tmp_path / "scheduler-state.json"
    state.write_text(json.dumps({"workflow_runs": {"kvikio": RUN} if workflow_runs is None else workflow_runs}))
    path = tmp_path / "bundle-reference.json"
    path.write_text(json.dumps(reference))
    aws = tmp_path / "bin" / "aws"
    aws.parent.mkdir(exist_ok=True)
    aws.write_text(FAKE_AWS)
    aws.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{aws.parent}:{os.environ['PATH']}",
        "FAKE_S3_ROOT": str(tmp_path / "s3"),
        "AWS_CALLS": str(tmp_path / "aws-calls"),
        "GITHUB_RUN_ID": run,
        "RELEASE_CANDIDATE_BUCKET": "candidate-store",
        "RELEASE_CANDIDATE_TRAIN_PREFIX": "train-state",
        "RELEASE_CANDIDATE_TRAIN_SHA256": TRAIN,
        "SCHEDULER_STATE": str(state),
        "REUSE_BINDINGS": str(bindings),
    }
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(_SCRIPT), str(path)], env=env, capture_output=True, text=True, check=False
    )


def _decision(result):
    assert result.returncode == 0, result.stderr  # noqa: S101
    return result.stdout.strip()


# The repository's bound run: unchanged behaviour.


def test_a_reference_from_the_bound_run_is_bound(tmp_path):
    assert _decision(_run(tmp_path, _reference())) == "bound"  # noqa: S101


def test_a_reference_from_another_run_is_dropped(tmp_path):
    assert _decision(_run(tmp_path, _reference(source_run_id="499"))) == "drop"  # noqa: S101


def test_a_repository_without_a_binding_keeps_its_references(tmp_path):
    assert _decision(_run(tmp_path, _reference(), workflow_runs={})) == "unbound"  # noqa: S101


def test_whole_repository_reuse_still_binds_through_the_scheduler_state(tmp_path):
    # The ci-imgs reuse job rebinds the repository to the earlier run; no per-row binding is needed.
    result = _run(tmp_path, _reused(), workflow_runs={"kvikio": "400"})
    assert _decision(result) == "bound"  # noqa: S101


# A row reused for this run.


def test_a_reused_row_with_a_binding_and_a_completed_source_run_is_reused(tmp_path):
    result = _run(tmp_path, _reused(), binding=_binding(), completions=[_completion()])
    assert _decision(result) == "reused"  # noqa: S101


def test_a_later_attempts_completion_covers_the_reused_attempt(tmp_path):
    result = _run(tmp_path, _reused(), binding=_binding(), completions=[_completion(attempt="2")])
    assert _decision(result) == "reused"  # noqa: S101


def test_a_reused_row_from_the_same_train_is_reused(tmp_path):
    result = _run(tmp_path, _reused(source_train=TRAIN), binding=_binding(), completions=[_completion(train=TRAIN)])
    assert _decision(result) == "reused"  # noqa: S101


def test_a_reused_reference_without_a_binding_for_this_run_is_dropped(tmp_path):
    # Another run of the train reused it; this run did not.
    assert _decision(_run(tmp_path, _reused(), completions=[_completion()])) == "drop"  # noqa: S101


@pytest.mark.parametrize(
    "binding",
    [
        _binding(source_run="401"),
        _binding(attempt="2"),
        _binding(build_input_digest="d" * 64),
    ],
)
def test_a_reused_reference_the_binding_does_not_name_is_dropped(tmp_path, binding):
    # Earlier runs of the train may have reused the same artifact from elsewhere; only the bound one counts.
    assert _decision(_run(tmp_path, _reused(), binding=binding, completions=[_completion()])) == "drop"  # noqa: S101


@pytest.mark.parametrize(
    ("binding", "completions", "message"),
    [
        (_binding(), [], "no completion record"),
        (_binding(), [_completion(attempt="0")], "no completion record"),
        (_binding(), [_completion(result="failed")], "no completion record"),
        (_binding(), [_completion(train=TRAIN)], "no completion record"),
        (_binding(schema_version=2), [_completion()], "malformed reuse binding"),
        (_binding(train_sha256="f" * 64), [_completion()], "malformed reuse binding"),
        (_binding(run_id="501"), [_completion()], "malformed reuse binding"),
        (_binding(repository="rapidsai/cudf"), [_completion()], "malformed reuse binding"),
        (_binding(artifact="other"), [_completion()], "malformed reuse binding"),
    ],
)
def test_a_binding_that_cannot_be_trusted_fails_loudly(tmp_path, binding, completions, message):
    result = _run(tmp_path, _reused(), binding=binding, completions=completions)
    assert result.returncode != 0  # noqa: S101
    assert message in result.stderr  # noqa: S101
    assert result.stdout == ""  # noqa: S101


def test_an_artifact_both_reused_and_built_in_this_run_fails(tmp_path):
    result = _run(tmp_path, _reference(), binding=_binding(), completions=[_completion()])
    assert result.returncode != 0  # noqa: S101
    assert "both reused and built" in result.stderr  # noqa: S101


@pytest.mark.parametrize("field", ["source_run_id", "source_run_attempt"])
def test_a_reused_reference_with_a_malformed_run_fails(tmp_path, field):
    reference = _reused()
    reference[field] = "x"
    result = _run(tmp_path, reference, binding=_binding(), completions=[_completion()])
    assert result.returncode != 0  # noqa: S101


def test_candidate_setup_uses_the_binding_helper_and_fetches_this_runs_bindings():
    setup = _SETUP.read_text()
    assert 'bash "${script_directory}/reference-binding.sh"' in setup  # noqa: S101
    assert '--include "*/reuse-bindings/${GITHUB_RUN_ID:?current GitHub run ID is required}/*.json"' in setup  # noqa: S101
    # The downloader runs after setup, outside the action, so setup installs the checks beside it.
    assert 'cp "${script_directory}/reference-binding.sh" "${script_directory}/catalog-bundle-key.sh"' in setup  # noqa: S101


# A row reused from an earlier run of this same train: the earlier run's original reference is already in this
# train, so reuse copies nothing and the binding names that original reference, which has no reuse block.


def _original(source_run="400", attempt="1", **fields):
    return _reference(source_run_id=source_run, source_run_attempt=attempt, **fields)


def test_an_original_reference_from_an_earlier_run_of_this_train_is_reused_when_bound(tmp_path):
    result = _run(tmp_path, _original(), binding=_binding(), completions=[_completion(train=TRAIN)])
    assert _decision(result) == "reused"  # noqa: S101


def test_an_original_reference_from_another_run_without_a_binding_is_still_dropped(tmp_path):
    result = _run(tmp_path, _original(), completions=[_completion(train=TRAIN)])
    assert _decision(result) == "drop"  # noqa: S101


def test_an_original_reference_the_binding_does_not_name_is_dropped(tmp_path):
    result = _run(tmp_path, _original(), binding=_binding(source_run="401"), completions=[_completion(train=TRAIN)])
    assert _decision(result) == "drop"  # noqa: S101


@pytest.mark.parametrize(
    ("reference", "completions", "message"),
    [
        # The completion must be in this train, where the original reference was built.
        (_original(), [_completion()], "no completion record"),
        (_original(), [], "no completion record"),
        # An original reference from another train can only arrive as a copy with a reuse block.
        (_original(train_sha256=EARLIER), [_completion()], "belongs to another train"),
    ],
)
def test_a_same_train_binding_that_cannot_be_trusted_fails_loudly(tmp_path, reference, completions, message):
    result = _run(tmp_path, reference, binding=_binding(), completions=completions)
    assert result.returncode != 0  # noqa: S101
    assert message in result.stderr  # noqa: S101
    assert result.stdout == ""  # noqa: S101
