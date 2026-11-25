# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).parents[1] / "release-candidate-dependencies" / "select_wheel.py"
_SPEC = importlib.util.spec_from_file_location("select_wheel", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"unable to load wheel selector: {_SCRIPT}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
supports_python = _MODULE.supports_python

_PATCHER = Path(__file__).parents[1] / "release-candidate-dependencies" / "patch_recipe.py"
_PATCHER_SPEC = importlib.util.spec_from_file_location("patch_recipe", _PATCHER)
if _PATCHER_SPEC is None or _PATCHER_SPEC.loader is None:
    raise RuntimeError(f"unable to load recipe patcher: {_PATCHER}")
_PATCHER_MODULE = importlib.util.module_from_spec(_PATCHER_SPEC)
_PATCHER_SPEC.loader.exec_module(_PATCHER_MODULE)

_LOCK_SELECTOR = Path(__file__).parents[1] / "release-candidate-dependencies" / "select_lock.py"
_SETUP = Path(__file__).parents[1] / "release-candidate-dependencies" / "setup.sh"
_DOWNLOADER = Path(__file__).parents[1] / "release-candidate-dependencies" / "download-from-candidate.sh"
_SOURCE_INPUTS = Path(__file__).parents[1] / "release-candidate-dependencies" / "write-source-inputs.sh"
_LOCK_SELECTOR_SPEC = importlib.util.spec_from_file_location("select_lock", _LOCK_SELECTOR)
if _LOCK_SELECTOR_SPEC is None or _LOCK_SELECTOR_SPEC.loader is None:
    raise RuntimeError(f"unable to load lock selector: {_LOCK_SELECTOR}")
_LOCK_SELECTOR_MODULE = importlib.util.module_from_spec(_LOCK_SELECTOR_SPEC)
_LOCK_SELECTOR_SPEC.loader.exec_module(_LOCK_SELECTOR_MODULE)


def test_candidate_conda_channels_exclude_public_rapids_and_nvidia_channels():
    setup = _SETUP.read_text()

    expected_contracts = (
        "candidate build rejected external Conda channel",
        "release train must contain the complete Conda bootstrap tool bundle",
        "bootstrap-channel/noarch/*",
        "rapidsai|rapidsai-nightly|nvidia|",
        "conda-forge|https://conda.anaconda.org/conda-forge|file://*|/*",
    )
    missing = [contract for contract in expected_contracts if contract not in setup]
    if missing:
        pytest.fail(f"candidate channel policy is incomplete: {missing}")


def test_candidate_conda_lock_selection_is_repository_scoped():
    setup = _SETUP.read_text()
    selector = (Path(__file__).parents[1] / "release-candidate-dependencies" / "select_conda_lock.py").read_text()

    expected_contracts = (
        'repository="${producer_repository#*/}"',
        '"${script_directory}/select_conda_lock.py"',
        'record["scope"].get("repository") == repository',
        "exactly one Conda lock for ",
    )
    combined = setup + selector
    missing = [contract for contract in expected_contracts if contract not in combined]
    if missing:
        pytest.fail(f"repository-scoped Conda lock selection is incomplete: {missing}")


def test_candidate_setup_exports_cache_policy_and_train_input_identity():
    setup = _SETUP.read_text()

    expected_contracts = (
        "candidate_build_policy.sccache",
        "lock_receipt_sha256",
        "variant_receipt_sha256",
        "RELEASE_CANDIDATE_BUILD_INPUTS",
        "RAPIDS_RELEASE_CANDIDATE_SCCACHE",
    )
    missing = [contract for contract in expected_contracts if contract not in setup]
    if missing:
        pytest.fail(f"candidate build-input identity is incomplete: {missing}")


def test_source_inputs_follow_recursive_source_dependencies_and_frozen_build_tools(tmp_path):
    train = {
        "release_units": [
            {"id": "conda:app", "artifact_family": "conda", "dependencies": ["source:app"]},
            {
                "id": "source:app",
                "artifact_family": "source",
                "repository": "app",
                "source_revision": "a" * 40,
                "dependencies": ["source:library"],
            },
            {"id": "source:library", "artifact_family": "source", "repository": "library", "source_revision": "b" * 40},
        ],
        "source_revisions": {"app": "a" * 40, "library": "b" * 40},
        "github_repositories": {"app": "rapidsai/app", "library": "rapidsai/library"},
        "build_tool_revisions": {
            "dependency-file-generator": "c" * 40,
            "rapids-build-backend": "d" * 40,
            "rapids-cmake": "e" * 40,
        },
    }
    train_path = tmp_path / "release-train.json"
    output_path = tmp_path / "source-inputs.json"
    train_path.write_text(json.dumps(train), encoding="utf-8")

    subprocess.run(  # noqa: S603
        [
            "/bin/bash",
            str(_SOURCE_INPUTS),
            "--train",
            str(train_path),
            "--output",
            str(output_path),
            "--target-unit",
            "conda:app",
        ],
        check=True,
    )
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result == {  # noqa: S101
        "schema_version": 1,
        "sources": {
            "app": {"repository": "rapidsai/app", "revision": "a" * 40},
            "library": {"repository": "rapidsai/library", "revision": "b" * 40},
        },
        "build_tools": {
            "dependency-file-generator": {"repository": "rapidsai/dependency-file-generator", "revision": "c" * 40},
            "rapids-build-backend": {"repository": "rapidsai/rapids-build-backend", "revision": "d" * 40},
            "rapids-cmake": {"repository": "rapidsai/rapids-cmake", "revision": "e" * 40},
        },
    }

    train["source_revisions"]["library"] = "f" * 40
    train["release_units"][2]["source_revision"] = "f" * 40
    train_path.write_text(json.dumps(train), encoding="utf-8")
    result_process = subprocess.run(  # noqa: S603
        [
            "/bin/bash",
            str(_SOURCE_INPUTS),
            "--train",
            str(train_path),
            "--output",
            str(output_path),
            "--target-unit",
            "conda:app",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result_process.returncode == 0  # noqa: S101
    assert json.loads(output_path.read_text(encoding="utf-8"))["sources"]["library"]["revision"] == "f" * 40  # noqa: S101

    train["release_units"][0]["dependencies"] = ["source:missing"]
    train_path.write_text(json.dumps(train), encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(  # noqa: S603
            [
                "/bin/bash",
                str(_SOURCE_INPUTS),
                "--train",
                str(train_path),
                "--output",
                str(output_path),
                "--target-unit",
                "conda:app",
            ],
            check=True,
            capture_output=True,
            text=True,
        )


def test_candidate_archive_families_use_train_dependencies_and_frozen_sources():
    setup = _SETUP.read_text()

    expected_contracts = (
        '"container" ]]; then',
        'if $family == "docs" or $family == "container" or $family == "maven" then ["conda:", "maven:"]',
        "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}",
        "write_source_manifest.py",
        "RAPIDS_RELEASE_CANDIDATE_TRAIN_FILE",
        "RAPIDS_RELEASE_CANDIDATE_UNIT_VERSIONS",
        "RELEASE_CANDIDATE_SOURCE_MANIFEST",
        "RELEASE_CANDIDATE_UNIT_ID",
        'target_unit="${candidate_unit_id}"',
        "index-conda-channel.sh",
        "train declares a Conda dependency but prepare-conda-channel is false",
    )
    missing = [contract for contract in expected_contracts if contract not in setup]
    if missing:
        pytest.fail(f"candidate archive dependency contract is incomplete: {missing}")


def _run_container_setup_without_a_channel(tmp_path, dependencies, prepare_conda_channel="false", extra_env=None):
    import os

    train = {
        "release_units": [
            {"id": "container:ci-images", "artifact_family": "container", "dependencies": dependencies},
            {"id": "conda:unrelated", "artifact_family": "conda", "dependencies": []},
            {"id": "conda:transitive", "artifact_family": "conda", "dependencies": ["conda:unrelated"]},
            {"id": "source:ci-images", "artifact_family": "source", "dependencies": []},
        ]
    }
    train_without_digest = json.dumps(train, sort_keys=True, separators=(",", ":"))
    train_sha256 = hashlib.sha256(train_without_digest.encode()).hexdigest()
    train["train_sha256"] = train_sha256
    train_path = tmp_path / "release-train.json"
    train_path.write_text(json.dumps(train), encoding="utf-8")

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    aws = stub_bin / "aws"
    aws.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [[ "$1" != "s3" || "$2" != "cp" || "$3" != *"/release-train.json" ]]; then\n'
        '  echo "unexpected aws request" >&2\n'
        "  exit 91\n"
        "fi\n"
        'cp "${CANDIDATE_TEST_TRAIN}" "$4"\n',
        encoding="utf-8",
    )
    aws.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "CANDIDATE_TEST_TRAIN": str(train_path),
        "GITHUB_ENV": str(tmp_path / "github-env"),
        "GITHUB_PATH": str(tmp_path / "github-path"),
        "RUNNER_TEMP": str(tmp_path / "runner-temp"),
        "GITHUB_REPOSITORY": "rapidsai/ci-imgs",
        "GITHUB_SHA": "f" * 40,
        "RELEASE_CANDIDATE_BUCKET": "candidate-test",
        "RELEASE_CANDIDATE_TRAIN_PREFIX": "train-state",
        "RELEASE_CANDIDATE_TRAIN_SHA256": train_sha256,
        "RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL": prepare_conda_channel,
        "RELEASE_CANDIDATE_ARTIFACT_FAMILY": "container",
        "RELEASE_CANDIDATE_UNIT_ID": "container:ci-images",
        "RELEASE_CANDIDATE_ARCH": "amd64",
        "RELEASE_CANDIDATE_CUDA_VERSION": "12.2",
        **(extra_env or {}),
    }
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(_SETUP)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_container_without_conda_dependencies_may_skip_private_channel(tmp_path):
    result = _run_container_setup_without_a_channel(tmp_path, [])

    assert result.returncode != 0  # noqa: S101
    assert "private Conda channel" not in result.stderr  # noqa: S101
    assert "release train must lock rapids-cmake" in result.stderr  # noqa: S101


def test_container_with_conda_dependency_cannot_skip_private_channel(tmp_path):
    result = _run_container_setup_without_a_channel(tmp_path, ["conda:unrelated"])

    assert result.returncode != 0  # noqa: S101
    assert "train declares a Conda dependency but prepare-conda-channel is false" in result.stderr  # noqa: S101
    assert "release train must lock rapids-cmake" not in result.stderr  # noqa: S101


def test_container_transitive_conda_dependency_cannot_skip_private_channel(tmp_path):
    result = _run_container_setup_without_a_channel(tmp_path, ["conda:transitive"])

    assert result.returncode != 0  # noqa: S101
    assert "train declares a Conda dependency but prepare-conda-channel is false" in result.stderr  # noqa: S101


def test_container_unknown_dependency_cannot_be_treated_as_empty(tmp_path):
    result = _run_container_setup_without_a_channel(tmp_path, ["conda:missing"])

    assert result.returncode != 0  # noqa: S101
    assert "unable to verify candidate dependencies from the release train" in result.stderr  # noqa: S101
    assert "release train must lock rapids-cmake" not in result.stderr  # noqa: S101


def test_container_with_conda_dependency_keeps_private_channel_mode(tmp_path):
    result = _run_container_setup_without_a_channel(tmp_path, ["conda:unrelated"], prepare_conda_channel="true")

    assert result.returncode != 0  # noqa: S101
    assert "private Conda channel" not in result.stderr  # noqa: S101
    assert "train declares a Conda dependency but prepare-conda-channel is false" not in result.stderr  # noqa: S101
    assert "release train must lock rapids-cmake" in result.stderr  # noqa: S101


def test_image_preflight_inputs_must_be_supplied_as_a_complete_set(tmp_path):
    result = _run_container_setup_without_a_channel(
        tmp_path,
        [],
        extra_env={"RELEASE_CANDIDATE_IMAGE_PREFLIGHT_FILE": str(tmp_path / "preflight.json")},
    )

    assert result.returncode != 0  # noqa: S101
    assert "must be supplied together" in result.stderr  # noqa: S101
    assert "unexpected aws request" not in result.stderr  # noqa: S101


def test_maven_dependency_resolution_does_not_require_a_python_matrix():
    action = (Path(__file__).parents[1] / "release-candidate-dependencies" / "action.yml").read_text()

    python_input = action.split("  python-version:", maxsplit=1)[1].split("\nruns:", maxsplit=1)[0]
    if "required: false" not in python_input:
        pytest.fail("Maven candidate dependency action unexpectedly requires a Python matrix")


def _stage_same_run_candidate(tmp_path):
    """Stage one committed same-run candidate bundle in a fake S3 tree and return the downloader inputs."""
    import os

    bucket = "candidate-store"
    train_sha256 = "a" * 64
    build_input_digest = "b" * 64
    artifact_digest = build_input_digest
    repository = "rapidsai/kvikio"
    artifact_name = "kvikio_conda_cpp_libkvikio_aarch64_cu12"
    package_path = "linux-aarch64/libkvikio-26.10.0-h1234567_0.conda"
    package_bytes = b"canonical package bytes"
    package_sha256 = hashlib.sha256(package_bytes).hexdigest()
    sbom_path = "release-evidence/libkvikio.cdx.json"
    sbom_bytes = b"canonical SBOM bytes"
    sbom_sha256 = hashlib.sha256(sbom_bytes).hexdigest()
    fake_s3 = tmp_path / "s3"
    train_prefix = f"train-state/{train_sha256}"
    run_prefix = f"{train_prefix}/kvikio/12345.2"
    reference_path = fake_s3 / bucket / run_prefix / build_input_digest / f"bundle-reference.{artifact_name}.json"
    catalog_key = f"{run_prefix}/{build_input_digest}/release-catalog-entries.{artifact_name}.json"
    artifact_key = f"artifacts/kvikio/{artifact_digest}"
    catalog_path = fake_s3 / bucket / catalog_key
    index_path = fake_s3 / bucket / artifact_key / "artifact-index.json"
    package_file = fake_s3 / bucket / artifact_key / package_path
    sbom_file = fake_s3 / bucket / artifact_key / sbom_path
    reference_path.parent.mkdir(parents=True)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    package_file.parent.mkdir(parents=True)
    sbom_file.parent.mkdir(parents=True)
    reference_path.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "storage_layout": 2,
                "train_sha256": train_sha256,
                "repository": repository,
                "source_artifact": artifact_name,
                "source_run_id": "12345",
                "source_run_attempt": "2",
                "build_input_digest": build_input_digest,
                "artifact_key": artifact_key,
                "artifact_index_key": f"{artifact_key}/artifact-index.json",
                "bundle_key": catalog_key,
            }
        )
    )
    prior_attempt_reference = (
        fake_s3 / bucket / f"{train_prefix}/kvikio/12345.1/{build_input_digest}/bundle-reference.{artifact_name}.json"
    )
    prior_attempt_reference.parent.mkdir(parents=True)
    prior_attempt_reference.write_text('{"source_run_id":"12345","source_run_attempt":"1"}')
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": {
                    "artifact": artifact_name,
                    "repository": repository,
                    "run_id": "12345",
                    "run_attempt": "2",
                    "sha": "d" * 40,
                },
                "entries": [],
            }
        )
    )
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "build_input_digest": build_input_digest,
                "source_artifact": artifact_name,
                "entries": [
                    {
                        "path": package_path,
                        "sha256": package_sha256,
                        "sbom": {"path": sbom_path, "sha256": sbom_sha256},
                    }
                ],
            }
        )
    )
    package_file.write_bytes(package_bytes)
    sbom_file.write_bytes(sbom_bytes)

    aws = tmp_path / "aws"
    aws.write_text(
        """#!/usr/bin/env python3
import fnmatch
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

if sys.argv[1:3] == ["s3api", "list-objects-v2"]:
    import json
    bucket = sys.argv[sys.argv.index("--bucket") + 1]
    prefix = sys.argv[sys.argv.index("--prefix") + 1]
    base = Path(os.environ["FAKE_S3_ROOT"]) / bucket
    keys = sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())
    print(json.dumps({"Contents": [{"Key": k} for k in keys if k.startswith(prefix)]}))
    sys.exit(0)
_, service, operation, source, destination, *options = sys.argv
assert service == "s3" and operation == "cp"
parsed = urlparse(source)
source_path = Path(os.environ["FAKE_S3_ROOT"]) / parsed.netloc / parsed.path.lstrip("/")
destination_path = Path(destination)
if "--recursive" in options:
    includes = [options[index + 1] for index, value in enumerate(options[:-1]) if value == "--include"]
    for path in source_path.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source_path).as_posix()
        if not includes or any(fnmatch.fnmatch(relative, pattern) for pattern in includes):
            target = destination_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
else:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, destination_path)
"""
    )
    aws.chmod(0o755)
    output = tmp_path / "download"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FAKE_S3_ROOT": str(fake_s3),
        "GITHUB_REPOSITORY": repository,
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": "d" * 40,
        "RELEASE_CANDIDATE_BUCKET": bucket,
        "RELEASE_CANDIDATE_TRAIN_PREFIX": "train-state",
        "RELEASE_CANDIDATE_TRAIN_SHA256": train_sha256,
        "RAPIDS_UNZIP_DIR": str(output),
    }
    return SimpleNamespace(
        env=env,
        artifact_name=artifact_name,
        output=output,
        package_path=package_path,
        package_bytes=package_bytes,
        sbom_path=sbom_path,
        sbom_bytes=sbom_bytes,
        reference_path=reference_path,
        build_input_digest=build_input_digest,
        artifact_key=artifact_key,
        catalog_path=catalog_path,
        sbom_file=sbom_file,
        index_path=index_path,
    )


def _reuse_staged_candidate(staged, *, completion=True, keep_own_reference=False):
    """Turn the staged same-run artifact into one this run reused from run 400.1 of an earlier train."""
    bucket_root = Path(staged.env["FAKE_S3_ROOT"]) / "candidate-store"
    train = staged.env["RELEASE_CANDIDATE_TRAIN_SHA256"]
    earlier = "c" * 64
    digest = staged.build_input_digest
    artifact = staged.artifact_name
    reference = json.loads(staged.reference_path.read_text())
    if not keep_own_reference:
        for path in (bucket_root / f"train-state/{train}/kvikio").glob(
            f"12345.*/{digest}/bundle-reference.{artifact}.json"
        ):
            path.unlink()
    catalog_key = f"train-state/{earlier}/kvikio/400.1/{digest}/release-catalog-entries.{artifact}.json"
    catalog = json.loads(staged.catalog_path.read_text())
    catalog["source"].update(run_id="400", run_attempt="1")
    (bucket_root / catalog_key).parent.mkdir(parents=True)
    (bucket_root / catalog_key).write_text(json.dumps(catalog))
    staged.catalog_path.unlink()
    reference.update(
        source_run_id="400",
        source_run_attempt="1",
        bundle_key=catalog_key,
        reuse={"source_train_sha256": earlier, "artifact_objects": {"k": "v"}},
    )
    reused = bucket_root / f"train-state/{train}/kvikio/400.1/{digest}/bundle-reference.{artifact}.json"
    reused.parent.mkdir(parents=True)
    reused.write_text(json.dumps(reference))
    binding = bucket_root / f"train-state/{train}/kvikio/reuse-bindings/12345/{artifact}.json"
    binding.parent.mkdir(parents=True)
    binding.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "train_sha256": train,
                "repository": "rapidsai/kvikio",
                "artifact": artifact,
                "run_id": "12345",
                "source_run_id": "400",
                "source_run_attempt": "1",
                "build_input_digest": digest,
            }
        )
    )
    if completion:
        record = bucket_root / f"train-state/{earlier}/completions/400.1.json"
        record.parent.mkdir(parents=True)
        record.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "train_sha256": earlier,
                    "run_id": "400",
                    "run_attempt": "1",
                    "result": "complete",
                }
            )
        )
    return staged


def test_same_run_downloader_fetches_an_artifact_this_run_reused(tmp_path):
    # A kvikio Python row needs the kvikio C++ artifact; this run reused it from an earlier complete run.
    staged = _reuse_staged_candidate(_stage_same_run_candidate(tmp_path))
    result = _run_same_run_downloader(staged.artifact_name, staged.env)
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert (staged.output / staged.package_path).read_bytes() == staged.package_bytes  # noqa: S101


def test_same_run_downloader_refuses_a_reuse_whose_source_run_never_completed(tmp_path):
    staged = _reuse_staged_candidate(_stage_same_run_candidate(tmp_path), completion=False)
    result = _run_same_run_downloader(staged.artifact_name, staged.env)
    assert result.returncode != 0  # noqa: S101
    assert "no completion record" in result.stderr  # noqa: S101
    assert not (staged.output / staged.package_path).exists()  # noqa: S101


def test_same_run_downloader_refuses_an_artifact_both_reused_and_built(tmp_path):
    staged = _reuse_staged_candidate(_stage_same_run_candidate(tmp_path), keep_own_reference=True)
    result = _run_same_run_downloader(staged.artifact_name, staged.env)
    assert result.returncode != 0  # noqa: S101
    assert "both reused and built" in result.stderr  # noqa: S101
    assert not (staged.output / staged.package_path).exists()  # noqa: S101


def _run_same_run_downloader(artifact_name, env):
    return subprocess.run(  # noqa: S603
        [str(_DOWNLOADER), artifact_name], env=env, capture_output=True, text=True, check=False
    )


def test_same_run_downloader_fetches_exact_reference_and_verifies_canonical_bytes(tmp_path):
    staged = _stage_same_run_candidate(tmp_path)
    env = staged.env
    artifact_name = staged.artifact_name
    output = staged.output
    package_path, package_bytes = staged.package_path, staged.package_bytes
    sbom_path, sbom_bytes, sbom_file = staged.sbom_path, staged.sbom_bytes, staged.sbom_file
    reference_path, catalog_path, index_path = staged.reference_path, staged.catalog_path, staged.index_path
    build_input_digest, artifact_key = staged.build_input_digest, staged.artifact_key

    def run_downloader():
        return subprocess.run(  # noqa: S603
            [str(_DOWNLOADER), artifact_name], env=env, capture_output=True, text=True, check=False
        )

    result = run_downloader()

    if result.returncode != 0:
        pytest.fail(result.stderr)
    if (
        result.stdout != str(output)
        or (output / package_path).read_bytes() != package_bytes
        or (output / sbom_path).read_bytes() != sbom_bytes
    ):
        pytest.fail("same-run downloader did not return the verified canonical artifact")

    reference = json.loads(reference_path.read_text())
    reference["artifact_key"] = f"artifacts/another-repository/{build_input_digest}"
    reference_path.write_text(json.dumps(reference))
    wrong_repository_result = run_downloader()
    if (
        wrong_repository_result.returncode == 0
        or "unsafe or inconsistent storage keys" not in wrong_repository_result.stderr
    ):
        pytest.fail("same-run downloader accepted a canonical artifact key for another repository")
    reference["artifact_key"] = artifact_key
    reference["artifact_index_key"] = f"{artifact_key}/other-index.json"
    reference_path.write_text(json.dumps(reference))
    wrong_index_key_result = run_downloader()
    if (
        wrong_index_key_result.returncode == 0
        or "unsafe or inconsistent storage keys" not in wrong_index_key_result.stderr
    ):
        pytest.fail("same-run downloader accepted a noncanonical artifact-index key")
    reference["artifact_index_key"] = f"{artifact_key}/artifact-index.json"
    reference_path.write_text(json.dumps(reference))

    catalog = json.loads(catalog_path.read_text())
    catalog["source"]["artifact"] = "another-artifact"
    catalog_path.write_text(json.dumps(catalog))
    wrong_artifact_result = run_downloader()
    if (
        wrong_artifact_result.returncode == 0
        or "does not match the current repository" not in wrong_artifact_result.stderr
    ):
        pytest.fail("same-run downloader accepted a catalog for another source artifact")

    catalog["source"]["artifact"] = artifact_name
    catalog["source"]["sha"] = "e" * 40
    catalog_path.write_text(json.dumps(catalog))
    wrong_sha_result = run_downloader()
    if (
        wrong_sha_result.returncode == 0
        or "does not match the current repository, run, attempt, and SHA" not in wrong_sha_result.stderr
    ):
        pytest.fail("same-run downloader accepted a catalog from another source SHA")

    catalog["source"]["sha"] = "d" * 40
    catalog_path.write_text(json.dumps(catalog))
    sbom_file.write_bytes(b"changed after canonical indexing")
    changed_result = run_downloader()
    if changed_result.returncode == 0 or "do not match the committed index" not in changed_result.stderr:
        pytest.fail("same-run downloader accepted bytes that disagree with the canonical index")

    index = json.loads(index_path.read_text())
    index["entries"] = []
    index_path.write_text(json.dumps(index))
    empty_index_result = run_downloader()
    if empty_index_result.returncode == 0 or "does not match its committed reference" not in empty_index_result.stderr:
        pytest.fail("same-run downloader accepted an empty canonical artifact index")


def _train_generated_run_env(staged, **producer):
    """A release run executes in release-tracking at its own commit while building the producer's commit."""
    env = {
        **staged.env,
        "GITHUB_REPOSITORY": "rapidsai/release-tracking",
        "GITHUB_SHA": "c" * 40,
    }
    env.update(producer)
    return env


@pytest.mark.parametrize(
    "producer",
    [
        # The action step exports the producer through the environment.
        {"RELEASE_CANDIDATE_PRODUCER_REPOSITORY": "rapidsai/kvikio", "RELEASE_CANDIDATE_PRODUCER_SHA": "d" * 40},
        # The installed rapids-download-from-github helper embeds the values captured at setup.
        {"producer_repository": "rapidsai/kvikio", "producer_sha": "d" * 40},
    ],
)
def test_same_run_downloader_checks_the_producer_sha_in_a_train_generated_run(tmp_path, producer):
    staged = _stage_same_run_candidate(tmp_path)

    result = _run_same_run_downloader(staged.artifact_name, _train_generated_run_env(staged, **producer))

    if result.returncode != 0:
        pytest.fail(result.stderr)
    if (staged.output / staged.package_path).read_bytes() != staged.package_bytes:
        pytest.fail("train-generated downloader did not return the producer's canonical artifact")


def test_same_run_downloader_rejects_a_catalog_bound_to_the_run_repository_sha(tmp_path):
    staged = _stage_same_run_candidate(tmp_path)
    catalog = json.loads(staged.catalog_path.read_text())
    catalog["source"]["sha"] = "c" * 40
    staged.catalog_path.write_text(json.dumps(catalog))
    env = _train_generated_run_env(
        staged,
        RELEASE_CANDIDATE_PRODUCER_REPOSITORY="rapidsai/kvikio",
        RELEASE_CANDIDATE_PRODUCER_SHA="d" * 40,
    )

    result = _run_same_run_downloader(staged.artifact_name, env)

    if result.returncode == 0 or "does not match the current repository, run, attempt, and SHA" not in result.stderr:
        pytest.fail("downloader accepted a catalog bound to the release-tracking commit instead of the producer's")
    if (staged.output / staged.package_path).exists():
        pytest.fail("downloader materialized an artifact from a mismatched catalog")


@pytest.mark.parametrize(
    ("producer", "message"),
    [
        ({"RELEASE_CANDIDATE_PRODUCER_REPOSITORY": "rapidsai/kvikio"}, "producer SHA is required"),
        ({"producer_repository": "rapidsai/kvikio"}, "producer SHA is required"),
        ({"RELEASE_CANDIDATE_PRODUCER_SHA": "d" * 40}, "producer SHA requires a producer repository"),
        (
            {"RELEASE_CANDIDATE_PRODUCER_REPOSITORY": "rapidsai/kvikio", "RELEASE_CANDIDATE_PRODUCER_SHA": "d" * 7},
            "producer SHA must be a full 40-character commit",
        ),
    ],
)
def test_same_run_downloader_rejects_incomplete_producer_identity(tmp_path, producer, message):
    staged = _stage_same_run_candidate(tmp_path)
    env = _train_generated_run_env(staged, **producer)
    aws_log = tmp_path / "aws-called"
    env["PATH"] = f"{_logging_aws(tmp_path, aws_log)}:{env['PATH']}"

    result = _run_same_run_downloader(staged.artifact_name, env)

    if result.returncode == 0 or message not in result.stderr:
        pytest.fail(f"downloader accepted an incomplete producer identity: {result.stderr}")
    if aws_log.exists():
        pytest.fail("downloader reached S3 before validating the producer identity")


def _logging_aws(tmp_path, log_path):
    """Shadow aws with a stub that records any call, so a test can prove no S3 request was made."""
    stub_dir = tmp_path / "logging-aws"
    stub_dir.mkdir()
    stub = stub_dir / "aws"
    stub.write_text(f'#!/usr/bin/env bash\necho "$*" >>"{log_path}"\nexit 97\n')
    stub.chmod(0o755)
    return stub_dir


@pytest.mark.parametrize(
    ("producer", "message"),
    [
        ({"RELEASE_CANDIDATE_PRODUCER_REPOSITORY": "rapidsai/ci-imgs"}, "producer SHA is required"),
        ({"RELEASE_CANDIDATE_PRODUCER_SHA": "d" * 40}, "producer SHA requires a producer repository"),
        (
            {"RELEASE_CANDIDATE_PRODUCER_REPOSITORY": "rapidsai/ci-imgs", "RELEASE_CANDIDATE_PRODUCER_SHA": "main"},
            "producer SHA must be a full 40-character commit",
        ),
    ],
)
def test_candidate_setup_rejects_incomplete_producer_identity(tmp_path, producer, message):
    result = _run_container_setup_without_a_channel(tmp_path, [], extra_env=producer)

    assert result.returncode != 0  # noqa: S101
    assert message in result.stderr  # noqa: S101
    # Validation precedes the train download, so the workspace never receives a train.
    assert not (tmp_path / "runner-temp" / "release-candidate-dependencies" / "release-train.json").exists()  # noqa: S101


def test_candidate_setup_accepts_a_complete_producer_identity(tmp_path):
    result = _run_container_setup_without_a_channel(
        tmp_path,
        [],
        extra_env={
            "RELEASE_CANDIDATE_PRODUCER_REPOSITORY": "rapidsai/ci-imgs",
            "RELEASE_CANDIDATE_PRODUCER_SHA": "d" * 40,
        },
    )

    # The stub train lacks rapids-cmake, so setup stops there: past producer validation.
    assert "producer SHA" not in result.stderr  # noqa: S101
    assert "release train must lock rapids-cmake" in result.stderr  # noqa: S101


def test_same_run_downloader_is_installed_by_candidate_dependency_setup():
    setup = _SETUP.read_text()
    if 'tail -n +2 "${script_directory}/download-from-candidate.sh"' not in setup:
        pytest.fail("candidate setup does not install the same-run downloader")
    if "printf 'producer_sha=%q\\n' \"${producer_sha}\"" not in setup:
        pytest.fail("candidate setup does not embed the producer SHA in the installed downloader")


def test_candidate_dependency_action_forwards_the_producer_sha():
    action = (Path(__file__).parents[1] / "release-candidate-dependencies" / "action.yml").read_text()
    if "  producer-sha:" not in action or "RELEASE_CANDIDATE_PRODUCER_SHA: ${{ inputs.producer-sha }}" not in action:
        pytest.fail("candidate dependency action does not forward producer-sha to setup")


def test_candidate_wheel_lock_lookup_passes_cuda_major():
    setup = _SETUP.read_text()
    if '--cuda-major "${cuda_major}"' not in setup:
        pytest.fail("candidate wheel lock lookup must include the exact CUDA major")


def test_candidate_setup_fails_closed_when_shared_implementation_revisions_drift():
    setup = _SETUP.read_text()

    expected_contracts = (
        '"gha-tools", "shared-actions", "shared-workflows"',
        'test("^[0-9a-f]{40}$")',
        '"${actual_repository}" != "${expected_repository}" || "${actual_revision}" != "${expected_revision}"',
        "RELEASE_CANDIDATE_SHARED_ACTIONS_REVISION",
        "RELEASE_CANDIDATE_SHARED_WORKFLOWS_REVISION",
    )
    missing = [contract for contract in expected_contracts if contract not in setup]
    if missing:
        pytest.fail(f"candidate implementation revision contract is incomplete: {missing}")


def test_direct_candidate_setup_skips_only_reusable_workflow_revision_check():
    setup = _SETUP.read_text()
    action = (Path(__file__).parents[1] / "release-candidate-dependencies" / "action.yml").read_text()

    if "implementation-mode:" not in action or "default: reusable" not in action:
        pytest.fail("candidate dependency action must default to reusable-workflow verification")
    if "RELEASE_CANDIDATE_IMPLEMENTATION_MODE: ${{ inputs.implementation-mode }}" not in action:
        pytest.fail("candidate dependency action does not pass its implementation mode")
    if 'implementation_mode="${RELEASE_CANDIDATE_IMPLEMENTATION_MODE:-reusable}"' not in setup:
        pytest.fail("candidate setup does not default to the reusable implementation mode")
    if '[[ "${implementation_mode}" != "reusable" && "${implementation_mode}" != "direct" ]]' not in setup:
        pytest.fail("candidate setup does not reject unsupported implementation modes")
    direct_mode = setup.split('if [[ "${implementation_mode}" == "reusable" ]]; then', maxsplit=1)
    if len(direct_mode) != 2 or '"shared-workflows"' not in direct_mode[1]:
        pytest.fail("the reusable-workflow source assertion is not guarded by implementation mode")
    if setup.index('"shared-actions"') > setup.index('if [[ "${implementation_mode}" == "reusable" ]]; then'):
        pytest.fail("direct mode must retain exact shared-actions source verification")


def test_wheel_lock_selector_requires_one_exact_matrix_scope():
    expected = {
        "path": "locks/wheel/x86_64-manylinux_2_28/python-3.14/requirements.txt",
        "sha256": "a" * 64,
    }
    receipt = {
        "wheel": [
            {
                **expected,
                "scope": {"platform": "x86_64-manylinux_2_28", "cuda": "13", "python": "3.14"},
            },
            {
                "path": "locks/wheel/x86_64-manylinux_2_28/cuda-12/python-3.14/requirements.txt",
                "sha256": "c" * 64,
                "scope": {"platform": "x86_64-manylinux_2_28", "cuda": "12", "python": "3.14"},
            },
            {
                "path": "locks/wheel/aarch64-manylinux_2_28/python-3.14/requirements.txt",
                "sha256": "b" * 64,
                "scope": {"platform": "aarch64-manylinux_2_28", "cuda": "13", "python": "3.14"},
            },
        ]
    }

    selected = _LOCK_SELECTOR_MODULE.select_wheel_lock(receipt, "x86_64-manylinux_2_28", "13", "3.14")

    if selected != expected:
        pytest.fail(f"wheel lock selector returned the wrong matrix lock: {selected}")


def test_wheel_lock_selector_requires_the_exact_cuda_scope():
    receipt = {
        "wheel": [
            {
                "path": "locks/wheel/x86_64-manylinux_2_28/cuda-12/python-3.14/requirements.txt",
                "sha256": "a" * 64,
                "scope": {"platform": "x86_64-manylinux_2_28", "cuda": "12", "python": "3.14"},
            }
        ]
    }

    with pytest.raises(ValueError, match=re.escape("CUDA 13, Python 3.14")):
        _LOCK_SELECTOR_MODULE.select_wheel_lock(receipt, "x86_64-manylinux_2_28", "13", "3.14")


def test_wheel_lock_selector_rejects_duplicate_exact_scopes():
    record = {
        "path": "locks/wheel/x86_64-manylinux_2_28/python-3.14/requirements.txt",
        "sha256": "a" * 64,
        "scope": {"platform": "x86_64-manylinux_2_28", "cuda": "13", "python": "3.14"},
    }
    with pytest.raises(ValueError, match="exactly one wheel lock"):
        _LOCK_SELECTOR_MODULE.select_wheel_lock({"wheel": [record, record]}, "x86_64-manylinux_2_28", "13", "3.14")


def test_wheel_lock_selector_rejects_missing_and_unsafe_records():
    with pytest.raises(ValueError, match="exactly one wheel lock"):
        _LOCK_SELECTOR_MODULE.select_wheel_lock({"wheel": []}, "x86_64-manylinux_2_28", "13", "3.14")
    with pytest.raises(ValueError, match="unsafe wheel lock path"):
        _LOCK_SELECTOR_MODULE.select_wheel_lock(
            {
                "wheel": [
                    {
                        "path": "../requirements.txt",
                        "sha256": "a" * 64,
                        "scope": {"platform": "x86_64-manylinux_2_28", "cuda": "13", "python": "3.14"},
                    }
                ]
            },
            "x86_64-manylinux_2_28",
            "13",
            "3.14",
        )


@pytest.mark.parametrize(
    ("wheel", "python_version"),
    [
        ("rapids_logger-0.3.0-py3-none-any.whl", "3.10"),
        ("package-1.0.0-cp310-abi3-manylinux_2_17_x86_64.whl", "3.12"),
        ("package-1.0.0-cp312-cp312-manylinux_2_17_x86_64.whl", "3.12"),
    ],
)
def test_wheel_tags_allow_compatible_python_versions(wheel, python_version):
    if not supports_python(wheel, python_version):
        pytest.fail(f"expected {wheel} to support Python {python_version}")


@pytest.mark.parametrize(
    ("wheel", "python_version"),
    [
        ("package-1.0.0-cp310-cp310-manylinux_2_17_x86_64.whl", "3.12"),
        ("package-1.0.0-cp312-abi3-manylinux_2_17_x86_64.whl", "3.10"),
        ("not-a-wheel", "3.12"),
    ],
)
def test_wheel_tags_reject_incompatible_python_versions(wheel, python_version):
    if supports_python(wheel, python_version):
        pytest.fail(f"expected {wheel} not to support Python {python_version}")


# Release run 45671939934: kvikio's arm64 wheel rows downloaded rapids-logger's x86_64 wheel too, so it was in their
# reuse key and a rebuild of the amd64 wheel alone would have rebuilt every arm64 wheel row downstream.
@pytest.mark.parametrize(
    ("wheel", "arch"),
    [
        ("rapids_logger-0.3.0-py3-none-any.whl", "amd64"),
        ("rapids_logger-0.3.0-py3-none-any.whl", "arm64"),
        ("rapids_logger-0.3.0-py3-none-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl", "amd64"),
        ("rapids_logger-0.3.0-py3-none-manylinux_2_26_aarch64.manylinux_2_28_aarch64.whl", "arm64"),
        ("package-1.0.0-cp312-cp312-musllinux_1_2_x86_64.whl", "amd64"),
        ("package-1.0.0-cp312-cp312-linux_aarch64.whl", "arm64"),
    ],
)
def test_wheel_tags_allow_the_row_architecture(wheel, arch):
    if not _MODULE.supports_arch(wheel, arch):
        pytest.fail(f"expected {wheel} to support {arch}")


@pytest.mark.parametrize(
    ("wheel", "arch"),
    [
        ("rapids_logger-0.3.0-py3-none-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl", "arm64"),
        ("rapids_logger-0.3.0-py3-none-manylinux_2_26_aarch64.manylinux_2_28_aarch64.whl", "amd64"),
        ("package-1.0.0-cp312-cp312-macosx_11_0_x86_64.whl", "amd64"),
        ("package-1.0.0-cp312-cp312-win_amd64.whl", "amd64"),
        ("not-a-wheel", "amd64"),
    ],
)
def test_wheel_tags_reject_other_architectures(wheel, arch):
    if _MODULE.supports_arch(wheel, arch):
        pytest.fail(f"expected {wheel} not to support {arch}")


def _select_wheel(*arguments):
    return subprocess.run(  # noqa: S603
        [sys.executable, str(_SCRIPT), *arguments], capture_output=True, text=True, check=False
    )


def test_wheel_selector_exits_1_only_for_an_incompatible_wheel():
    x86 = "rapids_logger-0.3.0-py3-none-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
    if _select_wheel("--python-version", "3.12", "--arch", "amd64", x86).returncode != 0:
        pytest.fail("an amd64 row must select an x86_64 wheel")
    if _select_wheel("--python-version", "3.12", "--arch", "arm64", x86).returncode != 1:
        pytest.fail("an arm64 row must skip an x86_64 wheel with exit status 1")


@pytest.mark.parametrize(
    "arguments",
    [
        ("--python-version", "3.12", "--arch", "ppc64le"),
        ("--python-version", "3.12"),
    ],
)
def test_wheel_selector_rejects_a_missing_or_unknown_arch(arguments):
    result = _select_wheel(*arguments, "rapids_logger-0.3.0-py3-none-any.whl")
    if result.returncode != 2:
        pytest.fail(f"expected a usage error, got {result.returncode}: {result.stderr}")


def test_candidate_setup_selects_wheels_for_the_row_arch_and_fails_on_selector_errors():
    setup = _SETUP.read_text()
    wheel_branch = setup.split("    *.whl)\n", maxsplit=1)[1].split("\n      ;;\n", maxsplit=1)[0]
    if '--arch "${RELEASE_CANDIDATE_ARCH}"' not in wheel_branch:
        pytest.fail("candidate setup must select upstream wheels for the row's architecture")
    # Exit 1 means "not for this row"; anything else is a selector failure and must not silently drop a dependency.
    for contract in ("1) continue ;;", "unable to select candidate wheel", "exit 1"):
        if contract not in wheel_branch:
            pytest.fail(f"candidate setup wheel selection is missing {contract!r}")


def test_recipe_patcher_adds_lock_metapackages_to_root_and_outputs():
    recipe = {
        "requirements": {"build": ["cmake"], "host": ["zlib"]},
        "outputs": [{"package": {"name": "example"}, "requirements": {"host": ["python"]}}],
    }

    _PATCHER_MODULE._add_requirements(recipe, "candidate-build-lock", "candidate-host-lock")
    _PATCHER_MODULE._add_requirements(recipe["outputs"][0], "candidate-build-lock", "candidate-host-lock")

    expected_root = {
        "build": ["cmake", "candidate-build-lock"],
        "host": ["zlib", "candidate-host-lock"],
    }
    expected_output = {
        "build": ["candidate-build-lock"],
        "host": ["python", "candidate-host-lock"],
    }
    if recipe["requirements"] != expected_root:
        pytest.fail("root recipe requirements did not receive both lock metapackages")
    if recipe["outputs"][0]["requirements"] != expected_output:
        pytest.fail("output requirements did not receive both lock metapackages")


def test_recipe_patcher_injects_locks_only_into_multi_output_build_targets():
    recipe = {
        "requirements": {"build": ["cmake"], "host": ["zlib"]},
        "outputs": [
            {
                "package": {"name": "library"},
                "build": {"script": {"content": "cmake --install build", "env": {"KEEP": "true"}}},
                "requirements": {"host": ["python"]},
            },
            {"package": {"name": "tests"}, "requirements": {"build": ["ninja"]}},
        ],
    }

    _PATCHER_MODULE._prepare_recipe_documents(recipe, "candidate-build-lock", "candidate-host-lock", "deadbeef", set())

    if "requirements" in recipe:
        pytest.fail("multi-output recipe retained a forbidden top-level requirements field")
    expected_library_requirements = {
        "build": ["cmake", "candidate-build-lock"],
        "host": ["zlib", "python", "candidate-host-lock"],
    }
    if recipe["outputs"][0]["requirements"] != expected_library_requirements:
        pytest.fail("library output did not inherit shared and candidate requirements")
    output_script = recipe["outputs"][0]["build"]["script"]
    if output_script["env"] != {"KEEP": "true"} or "-Drapids-cmake-sha=deadbeef" not in output_script["content"]:
        pytest.fail("script-object content was not pinned without losing its environment")
    expected_test_requirements = {
        "build": ["cmake", "ninja", "candidate-build-lock"],
        "host": ["zlib", "candidate-host-lock"],
    }
    if recipe["outputs"][1]["requirements"] != expected_test_requirements:
        pytest.fail("test output did not inherit shared and candidate requirements")


def test_recipe_patcher_injects_locks_into_single_output_root():
    recipe = {
        "build": {"script": "cmake -S . -B build"},
        "requirements": {"build": ["cmake"], "host": ["zlib"]},
        "tests": [{"script": "cmake -S tests -B build"}],
    }

    _PATCHER_MODULE._prepare_recipe_documents(recipe, "candidate-build-lock", "candidate-host-lock", "deadbeef", set())

    expected_requirements = {
        "build": ["cmake", "candidate-build-lock"],
        "host": ["zlib", "candidate-host-lock"],
    }
    if recipe["requirements"] != expected_requirements:
        pytest.fail("single-output root did not receive candidate requirements")
    if "-Drapids-cmake-sha=deadbeef" not in recipe["build"]["script"]:
        pytest.fail("package build script did not pin RAPIDS CMake")
    if "-Drapids-cmake-sha=deadbeef" in recipe["tests"][0]["script"]:
        pytest.fail("package test script unexpectedly pinned RAPIDS CMake")


def test_recipe_patcher_leaves_multi_output_tests_unmodified():
    recipe = {
        "outputs": [
            {"package": {"name": "library"}, "tests": [{"script": "ctest"}]},
            {"package": {"name": "bindings"}, "tests": [{"python": {"imports": ["bindings"]}}]},
        ]
    }

    _PATCHER_MODULE._prepare_recipe_documents(recipe, "candidate-build-lock", "candidate-host-lock", "deadbeef", set())

    for output in recipe["outputs"]:
        if "requirements" in output["tests"][0]:
            pytest.fail("candidate recipe patcher modified a package test")


def test_recipe_patcher_pins_and_locks_shared_cache_build():
    recipe = {
        "cache": {
            "build": {"script": {"content": "./build.sh", "secrets": ["AWS_TOKEN"]}},
            "requirements": {"build": ["cmake"], "host": ["zlib"]},
        },
        "outputs": [{"package": {"name": "library"}}],
    }

    _PATCHER_MODULE._prepare_recipe_documents(recipe, "candidate-build-lock", "candidate-host-lock", "deadbeef", set())

    if recipe["cache"]["requirements"] != {
        "build": ["cmake", "candidate-build-lock"],
        "host": ["zlib", "candidate-host-lock"],
    }:
        pytest.fail("shared cache build did not receive candidate lock packages")
    cache_script = recipe["cache"]["build"]["script"]
    if cache_script["secrets"] != ["AWS_TOKEN"] or "-Drapids-cmake-sha=deadbeef" not in cache_script["content"]:
        pytest.fail("shared cache build script was not pinned without losing its configuration")


def test_recipe_patcher_locks_staging_and_preserves_inheritance():
    recipe = {
        "source": {"path": "../../.."},
        "outputs": [
            {
                "staging": {"name": "libkvikio-build"},
                "build": {
                    "script": {
                        "content": "./build.sh -v -n libkvikio benchmarks",
                        "secrets": ["AWS_SESSION_TOKEN"],
                        "env": {"SCCACHE_CONF": "config"},
                    }
                },
                "requirements": {"build": ["cmake", "ninja"], "host": ["libcurl", "rapids-logger"]},
            },
            {"package": {"name": "libkvikio"}, "inherit": "libkvikio-build"},
            {"package": {"name": "libkvikio-tests"}, "inherit": "libkvikio-build"},
        ],
    }

    _PATCHER_MODULE._prepare_recipe_documents(recipe, "candidate-build-lock", "candidate-host-lock", "deadbeef", set())

    for output in recipe["outputs"]:
        if "candidate-build-lock" not in output["requirements"]["build"]:
            pytest.fail("staging or inherited output lost its build lock")
        if "candidate-host-lock" not in output["requirements"]["host"]:
            pytest.fail("staging or inherited output lost its host lock")
    stage, library, tests = recipe["outputs"]
    if stage["staging"] != {"name": "libkvikio-build"}:
        pytest.fail("staging build identity changed")
    if stage["requirements"]["build"] != ["cmake", "ninja", "candidate-build-lock"]:
        pytest.fail("staging build dependencies changed")
    if stage["requirements"]["host"] != ["libcurl", "rapids-logger", "candidate-host-lock"]:
        pytest.fail("staging host dependencies changed")
    script = stage["build"]["script"]
    if "-Drapids-cmake-sha=deadbeef" not in script["content"]:
        pytest.fail("staging build lost the RAPIDS CMake pin")
    if "./build.sh -v -n libkvikio benchmarks" not in script["content"]:
        pytest.fail("staging build command changed")
    if script["secrets"] != ["AWS_SESSION_TOKEN"] or script["env"] != {"SCCACHE_CONF": "config"}:
        pytest.fail("staging script environment or secrets changed")
    if library["inherit"] != "libkvikio-build" or tests["inherit"] != "libkvikio-build":
        pytest.fail("library or tests lost staging inheritance")
    if recipe["source"] != {"path": "../../.."} or "requirements" in recipe or "cache" in recipe:
        pytest.fail("patcher changed the migrated multi-output recipe structure")


def test_recipe_patcher_adapts_exact_variant_spelling_from_local_config(tmp_path):
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text("package:\n  name: example\n  version: 1\n")
    (tmp_path / "conda_build_config.yaml").write_text("libcurl_version:\n  - ==8.13.0\n")

    keys = _PATCHER_MODULE._local_exact_variant_keys(recipe)

    expected = "libcurl ==${{ libcurl_version }}"
    if _PATCHER_MODULE._add_exact_operator("libcurl ${{ libcurl_version }}", keys) != expected:
        pytest.fail("recipe patcher did not add the local exact MatchSpec operator")
    if _PATCHER_MODULE._add_exact_operator(expected, keys) != expected:
        pytest.fail("recipe patcher duplicated the local exact MatchSpec operator")


def test_recipe_patcher_accepts_recipe_directory(tmp_path):
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    expected = recipe / "recipe.yaml"
    expected.write_text("package:\n  name: example\n  version: 1\n")

    resolved = _PATCHER_MODULE._resolve_recipe(recipe, argparse.ArgumentParser())

    if resolved != expected:
        pytest.fail("recipe patcher did not resolve a recipe directory to recipe.yaml")


def test_recipe_patcher_copies_recipe_directory_with_recipe_yaml(tmp_path):
    recipe = tmp_path / "recipes" / "example"
    recipe.mkdir(parents=True)
    source_recipe = recipe / "recipe.yaml"
    source_recipe.write_text("package:\n  name: example\n  version: 1\n")
    (recipe / "build.sh").write_text("#!/usr/bin/env bash\n")

    destination = _PATCHER_MODULE._copy_recipe_directory(
        source_recipe,
        "candidate-build-lock",
        "candidate-host-lock",
    )

    if not destination.name.startswith(".example.release-candidate-"):
        pytest.fail("recipe patcher did not use a deterministic disposable directory")
    if not (destination / "recipe.yaml").is_file():
        pytest.fail("disposable recipe directory did not retain recipe.yaml")
    if not (destination / "build.sh").is_file():
        pytest.fail("disposable recipe directory did not retain recipe-local files")


@pytest.mark.parametrize(
    ("architecture", "platform"),
    [("amd64", "linux-64"), ("x86_64", "linux-64"), ("arm64", "linux-aarch64"), ("aarch64", "linux-aarch64")],
)
def test_candidate_conda_platform_normalizes_ci_architecture_names(architecture, platform):
    script = Path(__file__).parents[1] / "release-candidate-dependencies" / "platform.sh"
    # The command runs the repository-local helper with a fixed shell program.
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", 'source "$1"; conda_platform_for_arch "$2"', "bash", str(script), architecture],
        check=True,
        capture_output=True,
        text=True,
    )

    if result.stdout.strip() != platform:
        pytest.fail(f"expected {architecture} to normalize to {platform}")


def test_candidate_conda_platform_rejects_unknown_architecture():
    script = Path(__file__).parents[1] / "release-candidate-dependencies" / "platform.sh"
    # The command runs the repository-local helper with a fixed shell program.
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", 'source "$1"; conda_platform_for_arch "$2"', "bash", str(script), "not-an-arch"],
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode == 0 or "unsupported candidate architecture" not in result.stderr:
        pytest.fail("unknown candidate architecture was accepted")


def test_candidate_cmake_wrapper_pins_rapids_cmake_for_configure(tmp_path):
    output = tmp_path / "arguments.txt"
    original = tmp_path / "cmake"
    original.write_text('#!/bin/bash\nprintf \'%s\\n\' "$@" >"${TEST_OUTPUT}"\n')
    original.chmod(0o755)
    wrapper = Path(__file__).parents[1] / "release-candidate-dependencies" / "cmake.sh"
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(wrapper), "-S", ".", "-B", "build"],
        check=True,
        capture_output=True,
        text=True,
        env={
            "RAPIDS_CANDIDATE_ORIGINAL_CMAKE": str(original),
            "RAPIDS_CANDIDATE_RAPIDS_CMAKE_SHA": "a" * 40,
            "TEST_OUTPUT": str(output),
            "PATH": str(tmp_path),
        },
    )

    if result.stdout:
        pytest.fail("candidate CMake wrapper wrote unexpected standard output")
    expected_arguments = [
        "-Drapids-cmake-sha=" + "a" * 40,
        "-S",
        ".",
        "-B",
        "build",
    ]
    if output.read_text().splitlines() != expected_arguments:
        pytest.fail("candidate CMake wrapper did not pin RAPIDS CMake during configuration")


def test_candidate_cmake_wrapper_does_not_modify_build_mode(tmp_path):
    output = tmp_path / "arguments.txt"
    original = tmp_path / "cmake"
    original.write_text('#!/bin/bash\nprintf \'%s\\n\' "$@" >"${TEST_OUTPUT}"\n')
    original.chmod(0o755)
    wrapper = Path(__file__).parents[1] / "release-candidate-dependencies" / "cmake.sh"
    subprocess.run(  # noqa: S603
        ["/bin/bash", str(wrapper), "--build", "build"],
        check=True,
        capture_output=True,
        text=True,
        env={
            "RAPIDS_CANDIDATE_ORIGINAL_CMAKE": str(original),
            "RAPIDS_CANDIDATE_RAPIDS_CMAKE_SHA": "a" * 40,
            "TEST_OUTPUT": str(output),
            "PATH": str(tmp_path),
        },
    )

    if output.read_text().splitlines() != ["--build", "build"]:
        pytest.fail("candidate CMake wrapper modified a build-mode invocation")


# The reuse check compares a row's same-run inputs with the current run, so the downloader records exactly what it
# handed the build, and only after the bytes were verified.


def test_same_run_downloader_records_the_artifact_it_consumed(tmp_path):
    staged = _stage_same_run_candidate(tmp_path)
    record = tmp_path / "same-run-inputs.jsonl"
    record.write_text("")
    env = {**staged.env, "RELEASE_CANDIDATE_SAME_RUN_INPUTS": str(record)}
    result = _run_same_run_downloader(staged.artifact_name, env)
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert [json.loads(line) for line in record.read_text().splitlines()] == [  # noqa: S101
        {"artifact": staged.artifact_name, "build_input_digest": staged.build_input_digest}
    ]


def test_same_run_downloader_records_an_artifact_this_run_reused_by_its_digest(tmp_path):
    staged = _reuse_staged_candidate(_stage_same_run_candidate(tmp_path))
    record = tmp_path / "same-run-inputs.jsonl"
    record.write_text("")
    result = _run_same_run_downloader(
        staged.artifact_name, {**staged.env, "RELEASE_CANDIDATE_SAME_RUN_INPUTS": str(record)}
    )
    assert result.returncode == 0, result.stderr  # noqa: S101
    assert json.loads(record.read_text()) == {  # noqa: S101
        "artifact": staged.artifact_name,
        "build_input_digest": staged.build_input_digest,
    }


def test_same_run_downloader_records_nothing_when_it_fails(tmp_path):
    staged = _reuse_staged_candidate(_stage_same_run_candidate(tmp_path), keep_own_reference=True)
    record = tmp_path / "same-run-inputs.jsonl"
    record.write_text("")
    result = _run_same_run_downloader(
        staged.artifact_name, {**staged.env, "RELEASE_CANDIDATE_SAME_RUN_INPUTS": str(record)}
    )
    assert result.returncode != 0  # noqa: S101
    assert record.read_text() == ""  # noqa: S101


def test_setup_gives_the_downloader_and_upload_one_same_run_input_record():
    setup = (Path(__file__).parents[1] / "release-candidate-dependencies" / "setup.sh").read_text()
    assert ': >"${same_run_inputs}"' in setup  # noqa: S101
    assert "printf 'same_run_inputs=%q\\n' \"${same_run_inputs}\"" in setup  # noqa: S101
    assert "printf 'RELEASE_CANDIDATE_SAME_RUN_INPUTS=%s\\n' \"${same_run_inputs}\"" in setup  # noqa: S101
