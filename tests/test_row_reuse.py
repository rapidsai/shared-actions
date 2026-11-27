# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""A build row reuses its artifacts from the newest earlier complete run with the same reuse key, or builds."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "release-candidate-dependencies" / "row_reuse.py"
TRAIN = "a" * 64
EARLIER = "c" * 64
REPOSITORY = "rapidsai/kvikio"
ARTIFACT = "kvikio_conda_cpp_amd64"
RUN = "500"
KEY_RECORD = json.dumps(
    {"row": "kvikio cpp amd64", "schema_version": 1}, sort_keys=True, separators=(",", ":")
).encode()
KEY = hashlib.sha256(KEY_RECORD).hexdigest()

FAKE_AWS = r"""#!/usr/bin/env python3
import base64, hashlib, json, os, sys
from pathlib import Path

root = Path(os.environ["FAKE_S3"])
args = sys.argv[1:]
with open(os.environ["AWS_CALLS"], "a") as calls:
    calls.write(" ".join(args) + "\n")
def value(flag):
    return args[args.index(flag) + 1]
assert args[0] == "s3api", args
operation = args[1]
path = root / value("--bucket") / value("--key") if "--key" in args else None
if operation == "list-objects-v2":
    base = root / value("--bucket")
    prefix = value("--prefix")
    keys = sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())
    print(json.dumps({"Contents": [{"Key": k} for k in keys if k.startswith(prefix)]}))
elif operation == "head-object":
    if not path.is_file():
        print("An error occurred (404) when calling the HeadObject operation: Not Found", file=sys.stderr)
        sys.exit(254)
    data = path.read_bytes()
    print(json.dumps({"ChecksumSHA256": base64.b64encode(hashlib.sha256(data).digest()).decode(),
                      "ContentLength": len(data), "VersionId": "v-" + hashlib.sha256(data).hexdigest()[:8]}))
elif operation == "get-object":
    if not path.is_file():
        print("An error occurred (NoSuchKey) when calling the GetObject operation", file=sys.stderr)
        sys.exit(254)
    Path(args[-1]).write_bytes(path.read_bytes())
    print(json.dumps({}))
elif operation == "put-object":
    body = Path(value("--body")).read_bytes()
    if path.exists():
        print("An error occurred (PreconditionFailed) when calling the PutObject operation", file=sys.stderr)
        sys.exit(254)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    print(json.dumps({}))
else:
    print("unexpected aws call: " + " ".join(args), file=sys.stderr)
    sys.exit(2)
"""


def put(root: Path, key: str, data: bytes) -> str:
    path = root / "candidate-store" / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def index_run(
    root: Path,
    run="400",
    attempt="1",
    train=EARLIER,
    *,
    complete=True,
    key_record=KEY_RECORD,
    payload=b"pkg",
    same_run_inputs=(),
):
    """An earlier run that built and indexed the row, and (optionally) recorded its completion."""
    digest = hashlib.sha256(f"{run}.{attempt}".encode()).hexdigest()
    artifact_key = f"artifacts/kvikio/{digest}"
    payload_sha = put(root, f"{artifact_key}/libkvikio.conda", payload)
    index_sha = put(
        root,
        f"{artifact_key}/artifact-index.json",
        json.dumps(
            {
                "schema_version": 3,
                "build_input_digest": digest,
                "source_artifact": ARTIFACT,
                "entries": [{"path": "libkvikio.conda", "sha256": payload_sha}],
            }
        ).encode(),
    )
    record_sha = put(root, f"{artifact_key}/build-record.json", b'{"record": true}')
    bundle_key = f"train-state/{train}/kvikio/{run}.{attempt}/{digest}/release-catalog-entries.{ARTIFACT}.json"
    catalog_sha = put(root, bundle_key, b'{"entries": []}')
    reference = {
        "schema_version": 4,
        "producer": "shared-workflows",
        "storage_layout": 2,
        "train_sha256": train,
        "repository": REPOSITORY,
        "source_artifact": ARTIFACT,
        "source_run_id": run,
        "source_run_attempt": attempt,
        "build_input_digest": digest,
        "artifact_key": artifact_key,
        "build_record_key": f"{artifact_key}/build-record.json",
        "artifact_index_key": f"{artifact_key}/artifact-index.json",
        "bundle_key": bundle_key,
    }
    pointer = {
        "schema_version": 1,
        "row_reuse_key": KEY,
        "key_record": json.loads(key_record),
        "reference": reference,
        "build_record_sha256": record_sha,
        "artifact_index_sha256": index_sha,
        "catalog_sha256": catalog_sha,
        "same_run_inputs": list(same_run_inputs),
    }
    put(root, f"artifacts/kvikio/row-reuse-index/{KEY}/{ARTIFACT}/{run}-{attempt}.json", json.dumps(pointer).encode())
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
    return reference, payload_sha


def run_reuse(tmp_path, *, key=KEY, key_record=KEY_RECORD, attempt="1"):
    fake = tmp_path / "bin" / "aws"
    fake.parent.mkdir(exist_ok=True)
    fake.write_text(FAKE_AWS)
    fake.chmod(0o755)
    (tmp_path / "key.json").write_bytes(key_record)
    output = tmp_path / "github-output"
    env = {
        **os.environ,
        "PATH": f"{fake.parent}:{os.environ['PATH']}",
        "FAKE_S3": str(tmp_path / "s3"),
        "AWS_CALLS": str(tmp_path / "aws-calls"),
    }
    command = [
        sys.executable,
        str(SCRIPT),
        "--key",
        key,
        "--key-file",
        str(tmp_path / "key.json"),
        "--repository",
        REPOSITORY,
        "--run-id",
        RUN,
        "--run-attempt",
        attempt,
        "--train-sha256",
        TRAIN,
        "--bucket",
        "candidate-store",
        "--train-prefix",
        "train-state",
        "--content-prefix",
        "artifacts",
        "--github-output",
        str(output),
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)  # noqa: S603
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
    return result, outputs


def s3(tmp_path, key):
    return json.loads((tmp_path / "s3" / "candidate-store" / key).read_text())


def test_a_row_with_an_earlier_complete_build_reuses_it(tmp_path):
    reference, payload_sha = index_run(tmp_path / "s3")
    result, outputs = run_reuse(tmp_path)
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert outputs == {"reused": "true", "run": "400.1"}  # noqa: S101
    digest = reference["build_input_digest"]
    copied = s3(tmp_path, f"train-state/{TRAIN}/kvikio/400.1/{digest}/bundle-reference.{ARTIFACT}.json")
    expected = {
        **reference,
        "train_sha256": TRAIN,
        "reuse": {
            "source_train_sha256": EARLIER,
            "artifact_objects": {
                f"{reference['artifact_key']}/libkvikio.conda": {
                    "version_id": "v-" + payload_sha[:8],
                    "sha256": payload_sha,
                    "size": 3,
                }
            },
        },
    }
    assert copied == expected  # noqa: S101
    assert s3(tmp_path, f"train-state/{TRAIN}/kvikio/reuse-bindings/{RUN}/{ARTIFACT}.json") == {  # noqa: S101
        "schema_version": 1,
        "train_sha256": TRAIN,
        "repository": REPOSITORY,
        "artifact": ARTIFACT,
        "run_id": RUN,
        "source_run_id": "400",
        "source_run_attempt": "1",
        "build_input_digest": digest,
    }


def test_the_newest_complete_run_wins(tmp_path):
    index_run(tmp_path / "s3", run="400")
    index_run(tmp_path / "s3", run="410", payload=b"newer")
    _result, outputs = run_reuse(tmp_path)
    assert outputs["run"] == "410.1"  # noqa: S101


def test_an_incomplete_newer_run_falls_back_to_an_older_complete_one(tmp_path):
    index_run(tmp_path / "s3", run="400")
    index_run(tmp_path / "s3", run="410", complete=False)
    _result, outputs = run_reuse(tmp_path)
    assert outputs["run"] == "400.1"  # noqa: S101


def test_a_changed_payload_is_not_reused(tmp_path):
    reference, _sha = index_run(tmp_path / "s3")
    put(tmp_path / "s3", f"{reference['artifact_key']}/libkvikio.conda", b"tampered")
    result, outputs = run_reuse(tmp_path)
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert outputs == {"reused": "false"}  # noqa: S101
    assert not (tmp_path / "s3" / "candidate-store" / f"train-state/{TRAIN}").exists()  # noqa: S101


@pytest.mark.parametrize(
    "setup",
    [
        lambda root: None,  # nothing indexed under this key
        lambda root: index_run(root, complete=False),  # the only run never completed
        lambda root: index_run(root, key_record=b'{"row":"other","schema_version":1}'),  # forged key record
    ],
)
def test_without_a_trustworthy_earlier_build_the_row_builds(tmp_path, setup):
    setup(tmp_path / "s3")
    result, outputs = run_reuse(tmp_path)
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert outputs == {"reused": "false"}  # noqa: S101


def test_a_key_that_does_not_match_its_record_fails(tmp_path):
    index_run(tmp_path / "s3")
    result, outputs = run_reuse(tmp_path, key="f" * 64)
    assert result.returncode != 0  # noqa: S101
    assert outputs == {}  # noqa: S101


def test_reusing_again_in_a_later_attempt_of_the_same_run_is_idempotent(tmp_path):
    index_run(tmp_path / "s3")
    first, _ = run_reuse(tmp_path)
    second, outputs = run_reuse(tmp_path)
    assert first.returncode == 0 and second.returncode == 0, second.stderr  # noqa: S101
    assert outputs["reused"] == "true"  # noqa: S101


def test_candidate_setup_installs_the_reuse_command_only_for_a_row_with_a_key():
    setup = (Path(__file__).parents[1] / "release-candidate-dependencies" / "setup.sh").read_text()
    install = setup.index('>"${tools}/candidate-row-reuse"')
    assert setup.index('row_reuse_key="$(python3 "${script_directory}/row_reuse_key.py"') < install  # noqa: S101
    assert 'if [[ -n "${row_reuse_key}" ]]; then\n  cp "${script_directory}/row_reuse.py"' in setup  # noqa: S101


def original_reference(root, reference):
    """What the earlier run's upload left in its own train: the reference, with no reuse block."""
    digest = reference["build_input_digest"]
    run, attempt = reference["source_run_id"], reference["source_run_attempt"]
    key = f"train-state/{reference['train_sha256']}/kvikio/{run}.{attempt}/{digest}/bundle-reference.{ARTIFACT}.json"
    put(root, key, json.dumps(reference, sort_keys=True, separators=(",", ":")).encode())
    return key


def test_a_row_reused_from_an_earlier_run_of_the_same_train_only_binds_it(tmp_path):
    # The earlier run's original reference already sits where a copy would go, so nothing is copied.
    reference, _sha = index_run(tmp_path / "s3", train=TRAIN)
    key = original_reference(tmp_path / "s3", reference)
    before = (tmp_path / "s3" / "candidate-store" / key).read_bytes()
    result, outputs = run_reuse(tmp_path)
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert outputs == {"reused": "true", "run": "400.1"}  # noqa: S101
    assert (tmp_path / "s3" / "candidate-store" / key).read_bytes() == before  # noqa: S101
    binding = s3(tmp_path, f"train-state/{TRAIN}/kvikio/reuse-bindings/{RUN}/{ARTIFACT}.json")
    assert (binding["source_run_id"], binding["build_input_digest"]) == ("400", reference["build_input_digest"])  # noqa: S101


@pytest.mark.parametrize("change", ["missing", "different"])
def test_a_same_train_run_whose_original_reference_is_missing_or_different_is_not_reused(tmp_path, change):
    reference, _sha = index_run(tmp_path / "s3", train=TRAIN)
    if change == "different":
        original_reference(tmp_path / "s3", {**reference, "artifact_key": "artifacts/kvikio/elsewhere"})
    result, outputs = run_reuse(tmp_path)
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert outputs == {"reused": "false"}  # noqa: S101
    assert not (tmp_path / "s3" / "candidate-store" / f"train-state/{TRAIN}/kvikio/reuse-bindings").exists()  # noqa: S101


# A row that consumed another artifact of its own repository from the same run (kvikio Python consumes kvikio C++)
# is reused only if this run has that artifact with the same build input digest. The key cannot hold it: other rows
# of the repository may still be uploading when setup computes the key.
CPP = "kvikio_conda_cpp_amd64_lib"
CPP_DIGEST = "e" * 64


def consumed(digest=CPP_DIGEST, artifact=CPP):
    return {"artifact": artifact, "build_input_digest": digest}


def built_in_this_run(root, digest=CPP_DIGEST, artifact=CPP, attempt="1"):
    put(root, f"train-state/{TRAIN}/kvikio/{RUN}.{attempt}/{digest}/bundle-reference.{artifact}.json", b"{}")


def reused_in_this_run(root, digest=CPP_DIGEST, artifact=CPP):
    binding = {"schema_version": 1, "artifact": artifact, "run_id": RUN, "build_input_digest": digest}
    put(root, f"train-state/{TRAIN}/kvikio/reuse-bindings/{RUN}/{artifact}.json", json.dumps(binding).encode())


@pytest.mark.parametrize("current", [built_in_this_run, reused_in_this_run])
def test_a_row_is_reused_when_this_run_has_the_same_same_run_input(tmp_path, current):
    index_run(tmp_path / "s3", same_run_inputs=[consumed()])
    current(tmp_path / "s3")
    result, outputs = run_reuse(tmp_path)
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert outputs == {"reused": "true", "run": "400.1"}  # noqa: S101


def test_a_rerun_row_checks_its_same_run_input_against_the_attempt_that_built_it(tmp_path):
    # "Re-run failed jobs" (attempt 2) keeps kvikio C++ from attempt 1; the Python row's check must find it there,
    # as the downloader and the final collection do (newest attempt up to this one).
    index_run(tmp_path / "s3", same_run_inputs=[consumed()])
    built_in_this_run(tmp_path / "s3", attempt="1")
    result, outputs = run_reuse(tmp_path, attempt="2")
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert outputs == {"reused": "true", "run": "400.1"}  # noqa: S101


def test_a_rerun_row_takes_the_newest_attempt_that_built_its_same_run_input(tmp_path):
    index_run(tmp_path / "s3", same_run_inputs=[consumed()])
    built_in_this_run(tmp_path / "s3", attempt="1")
    built_in_this_run(tmp_path / "s3", digest="f" * 64, attempt="2")
    result, outputs = run_reuse(tmp_path, attempt="3")
    assert outputs == {"reused": "false"}  # noqa: S101
    assert "built differently" in result.stderr  # noqa: S101


@pytest.mark.parametrize(
    "current",
    [
        # kvikio C++ rebuilt in this run (for example against a changed rapids-logger): its digest is new.
        lambda root: built_in_this_run(root, digest="f" * 64),
        lambda root: reused_in_this_run(root, digest="f" * 64),
        # This run has no such artifact, or an ambiguous one.
        lambda root: None,
        lambda root: (built_in_this_run(root), built_in_this_run(root, digest="f" * 64)),
        # Only a later attempt built it; a row never looks ahead of its own attempt, as the downloader doesn't.
        lambda root: built_in_this_run(root, attempt="2"),
    ],
)
def test_a_row_whose_same_run_input_differs_in_this_run_builds(tmp_path, current):
    index_run(tmp_path / "s3", same_run_inputs=[consumed()])
    current(tmp_path / "s3")
    result, outputs = run_reuse(tmp_path)
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert outputs == {"reused": "false"}  # noqa: S101
    assert "same-run input" in result.stderr  # noqa: S101
    assert not (tmp_path / "s3" / "candidate-store" / f"train-state/{TRAIN}/kvikio/400.1").exists()  # noqa: S101


@pytest.mark.parametrize(
    "inputs",
    [None, "x", [{"artifact": CPP}], [{"artifact": "../x", "build_input_digest": CPP_DIGEST}], [consumed("abc")]],
)
def test_an_index_entry_without_valid_same_run_inputs_is_not_reused(tmp_path, inputs):
    index_run(tmp_path / "s3")
    key = f"artifacts/kvikio/row-reuse-index/{KEY}/{ARTIFACT}/400-1.json"
    pointer = s3(tmp_path, key)
    if inputs is None:
        del pointer["same_run_inputs"]
    else:
        pointer["same_run_inputs"] = inputs
    put(tmp_path / "s3", key, json.dumps(pointer).encode())
    result, outputs = run_reuse(tmp_path)
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert outputs == {"reused": "false"}  # noqa: S101


def test_candidate_setup_passes_the_run_attempt_to_the_reuse_check():
    setup = (Path(__file__).parents[1] / "release-candidate-dependencies" / "setup.sh").read_text()
    assert '--run-attempt "${GITHUB_RUN_ATTEMPT:?}"' in setup  # noqa: S101


# A run that didn't complete (a row failed, or it was cancelled) still leaves the rows verify checked: verify writes a
# record per artifact it fully verified, and an earlier row is reusable with that record instead of the run's
# completion record (26.12 release run 45926793154 failed and lost every row it had built).
def verified(root, reference, **changes):
    record = {
        "schema_version": 1,
        "train_sha256": reference["train_sha256"],
        "repository": "kvikio",
        "artifact": ARTIFACT,
        "run_id": reference["source_run_id"],
        "run_attempt": reference["source_run_attempt"],
        "build_input_digest": reference["build_input_digest"],
        "result": "verified",
        **changes,
    }
    run, attempt = reference["source_run_id"], reference["source_run_attempt"]
    put(
        root,
        f"train-state/{reference['train_sha256']}/verified/kvikio/{run}.{attempt}/{ARTIFACT}.json",
        json.dumps(record).encode(),
    )


def test_a_row_of_an_incomplete_run_is_reused_when_verify_recorded_its_artifact(tmp_path):
    reference, _ = index_run(tmp_path / "s3", complete=False)
    verified(tmp_path / "s3", reference)
    result, outputs = run_reuse(tmp_path)
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert outputs == {"reused": "true", "run": "400.1"}  # noqa: S101


@pytest.mark.parametrize(
    "changes",
    [
        {"build_input_digest": "f" * 64},  # verify checked another build of the artifact
        {"run_attempt": "2"},  # another attempt
        {"result": "complete"},
        {"repository": "rmm"},
    ],
)
def test_a_verification_record_for_anything_else_is_not_trusted(tmp_path, changes):
    reference, _ = index_run(tmp_path / "s3", complete=False)
    verified(tmp_path / "s3", reference, **changes)
    result, outputs = run_reuse(tmp_path)
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert outputs == {"reused": "false"}  # noqa: S101
    assert not (tmp_path / "s3" / "candidate-store" / f"train-state/{TRAIN}/kvikio/reuse-bindings").exists()  # noqa: S101
