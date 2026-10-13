import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ACTION = yaml.safe_load((ROOT / "build-devcontainer" / "action.yml").read_text())


def _step(name):
    return next(step for step in ACTION["runs"]["steps"] if step.get("name") == name)


def _run_step(step, tmp_path, *, push, output_path=""):
    script = step["run"].replace("${{ runner.temp }}", str(tmp_path))
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir(exist_ok=True)
    arguments_file = tmp_path / "arguments.json"
    stub = executable_dir / "devcontainer"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "pathlib.Path(os.environ['DEVCONTAINER_ARGS']).write_text(json.dumps(args))\n"
        "if '--output' in args:\n"
        "    output = args[args.index('--output') + 1]\n"
        "    if output.startswith('type=oci,dest='):\n"
        "        pathlib.Path(output.removeprefix('type=oci,dest=')).write_bytes(b'oci-image-archive')\n"
        "print('exporting manifest sha256:abcdef1234567890')\n"
    )
    stub.chmod(0o755)

    output_file = tmp_path / "github-output"
    env = {
        **os.environ,
        "PATH": f"{executable_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output_file),
        "DEVCONTAINER_ARGS": str(arguments_file),
        "NODE_NO_WARNINGS": "1",
        "arch": "amd64",
        "output_path": output_path,
        "push": push,
        "repo": "rapidsai/example",
        "retries": "0",
        "tag": "Ubuntu",
        "version": "26.10",
        "workspace_dir": ".",
        "devcontainer_json": "devcontainer.json",
    }
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    values = {}
    if output_file.exists():
        values = dict(line.split("=", 1) for line in output_file.read_text().splitlines())
    args = json.loads(arguments_file.read_text()) if arguments_file.exists() else []
    return result, args, values


def test_false_push_exports_oci_archive_and_reports_metadata(tmp_path):
    archive = tmp_path / "artifacts" / "example.oci.tar"
    result, args, outputs = _run_step(_step("Build ${{ inputs.repo }}:${{ inputs.tag }} (${{ inputs.arch }})"), tmp_path, push="false", output_path=str(archive))

    assert result.returncode == 0, result.stderr
    assert args[args.index("--output") + 1] == f"type=oci,dest={archive}"
    assert not any("push=true" in arg for arg in args)
    assert archive.read_bytes() == b"oci-image-archive"
    assert outputs["archive_path"] == str(archive)
    assert outputs["archive_name"] == archive.name
    assert outputs["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert outputs["archive_size"] == str(archive.stat().st_size)


def test_legacy_false_push_without_output_path_keeps_no_export(tmp_path):
    result, args, outputs = _run_step(_step("Build ${{ inputs.repo }}:${{ inputs.tag }} (${{ inputs.arch }})"), tmp_path, push="false")

    assert result.returncode == 0, result.stderr
    assert "--output" not in args
    assert outputs["archive_path"] == ""
    assert outputs["archive_sha256"] == ""
    assert outputs["hash_amd64"] == ""


def test_legacy_push_without_output_path_keeps_image_push(tmp_path):
    result, args, outputs = _run_step(_step("Build ${{ inputs.repo }}:${{ inputs.tag }} (${{ inputs.arch }})"), tmp_path, push="true")

    assert result.returncode == 0, result.stderr
    assert args[args.index("--output") + 1].startswith("type=image,")
    assert "push=true" in args[args.index("--output") + 1]
    assert outputs["hash_amd64"] == "sha256:abcdef1234567890"
    assert outputs["archive_path"] == ""


@pytest.mark.parametrize(
    ("push", "output_path", "message"),
    [
        ("true", "/tmp/image.oci.tar", "output-path can only be used"),
        ("maybe", "", "push must be 'true' or 'false'"),
        ("false", "/tmp/image,other.oci.tar", "cannot contain newlines or commas"),
    ],
)
def test_invalid_build_mode_fails_before_setup(push, output_path, message, tmp_path):
    step = _step("Validate devcontainer build mode")
    script = step["run"]
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=ROOT,
        env={**os.environ, "push": push, "output_path": output_path},
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert message in result.stderr
