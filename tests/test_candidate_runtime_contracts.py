# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Runtime contracts for candidate actions on lean host runners."""

import importlib.util
import os
import subprocess
from pathlib import Path

import yaml
import pytest

ROOT = Path(__file__).parents[1]
_SELECTOR_PATH = ROOT / "release-candidate-dependencies" / "select_conda_lock.py"
_SELECTOR_SPEC = importlib.util.spec_from_file_location("select_conda_lock", _SELECTOR_PATH)
assert _SELECTOR_SPEC is not None and _SELECTOR_SPEC.loader is not None
_SELECTOR_MODULE = importlib.util.module_from_spec(_SELECTOR_SPEC)
_SELECTOR_SPEC.loader.exec_module(_SELECTOR_MODULE)


def test_context_action_runs_with_python3_when_python_alias_is_absent(tmp_path):
    action = yaml.safe_load((ROOT / "release-candidate-context" / "action.yml").read_text())
    resolve_step = next(step for step in action["runs"]["steps"] if step.get("id") == "resolve")
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    invocations = tmp_path / "python3-arguments"
    fake_python3 = executable_dir / "python3"
    fake_python3.write_text(
        "#!/bin/bash\nprintf '%s\\n' \"$@\" >> \"${PYTHON3_INVOCATIONS}\"\n",
        encoding="utf-8",
    )
    fake_python3.chmod(0o755)
    output = tmp_path / "github-output"
    environment = {
        **os.environ,
        "PATH": str(executable_dir),
        "IMPLEMENTATION_PATH": str(tmp_path),
        "SOURCE_SHA": "a" * 40,
        "CUSTOM_SCRIPT": "",
        "GITHUB_OUTPUT": str(output),
        "PYTHON3_INVOCATIONS": str(invocations),
    }

    result = subprocess.run(
        ["/bin/bash", "-e", "-c", resolve_step["run"]],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert invocations.read_text().splitlines() == [
        str(tmp_path / "resolver.py"),
        "--source-sha",
        "a" * 40,
        "--script",
        "",
    ]


def _conda_lock(path, repository, platform, cuda, environment):
    return {
        "path": path,
        "sha256": "a" * 64,
        "scope": {
            "repository": repository,
            "platform": platform,
            "cuda": cuda,
            "environment": environment,
        },
    }


def test_conda_lock_selector_handles_full_train_matrix_and_exact_cuda_minor():
    target_lock = _conda_lock(
        "locks/conda/linux-64/cuda-12.9/cucim/host-constraints.txt",
        "cucim",
        "linux-64",
        "12.9",
        "host",
    )
    receipt = {
        "conda": [
            _conda_lock(
                "locks/conda/linux-64/cuda-12.9/cugraph/host-constraints.txt",
                "cugraph",
                "linux-64",
                "12.9",
                "host",
            ),
            _conda_lock(
                "locks/conda/linux-aarch64/cuda-12.9/cucim/host-constraints.txt",
                "cucim",
                "linux-aarch64",
                "12.9",
                "host",
            ),
            _conda_lock(
                "locks/conda/linux-64/cuda-13.3/cucim/host-constraints.txt",
                "cucim",
                "linux-64",
                "13.3",
                "host",
            ),
            _conda_lock(
                "locks/conda/linux-64/cuda-12.9/cucim/build-constraints.txt",
                "cucim",
                "linux-64",
                "12.9",
                "build",
            ),
            target_lock,
        ]
    }

    selected = _SELECTOR_MODULE.select_conda_lock(receipt, "cucim", "linux-64", "12.9", "host")

    assert selected == {"path": target_lock["path"], "sha256": "a" * 64}


def test_conda_lock_selector_rejects_major_only_wrong_environment_and_duplicates():
    receipt = {
        "input_locks": {
            "conda": [
                _conda_lock("locks/host.txt", "cucim", "linux-64", "12.9", "host"),
                _conda_lock("locks/build.txt", "cucim", "linux-64", "12.9", "build"),
            ]
        }
    }

    with pytest.raises(ValueError, match=r"CUDA 12, host\); found 0"):
        _SELECTOR_MODULE.select_conda_lock(receipt, "cucim", "linux-64", "12", "host")
    with pytest.raises(ValueError, match=r"CUDA 12\.9, host\); found 0"):
        _SELECTOR_MODULE.select_conda_lock(receipt, "other-repo", "linux-64", "12.9", "host")
    with pytest.raises(ValueError, match=r"CUDA 12\.9, host\); found 2"):
        duplicated = {"conda": receipt["input_locks"]["conda"] + [receipt["input_locks"]["conda"][0]]}
        _SELECTOR_MODULE.select_conda_lock(duplicated, "cucim", "linux-64", "12.9", "host")


@pytest.mark.parametrize(
    ("path", "sha256", "message"),
    [
        ("../locks/host.txt", "a" * 64, "unsafe Conda lock path"),
        ("locks/host.txt", "bad", "invalid Conda lock checksum"),
    ],
)
def test_conda_lock_selector_rejects_unsafe_references(path, sha256, message):
    lock = _conda_lock(path, "cucim", "linux-64", "12.9", "host")
    lock["sha256"] = sha256

    with pytest.raises(ValueError, match=message):
        _SELECTOR_MODULE.select_conda_lock({"conda": [lock]}, "cucim", "linux-64", "12.9", "host")


def test_setup_uses_exact_conda_cuda_scope_instead_of_cuda_major():
    setup = (ROOT / "release-candidate-dependencies" / "setup.sh").read_text()

    assert '"${script_directory}/select_conda_lock.py"' in setup
    assert '--cuda-version "${RELEASE_CANDIDATE_CUDA_VERSION}"' in setup
    assert '--cuda "${cuda_major}"' not in setup
