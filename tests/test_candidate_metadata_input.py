# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
"""Metadata source is an output-affecting candidate build tool input."""

import json
import subprocess  # nosec B404
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "release-candidate-dependencies/write-source-inputs.sh"


@pytest.mark.parametrize("revision", [None, "a" * 40, "main"])
def test_metadata_revision_is_frozen_or_rejected(tmp_path, revision):
    train = {
        "release_units": [
            {"id": "container:example", "artifact_family": "container", "dependencies": []},
        ],
        "source_revisions": {},
        "github_repositories": {},
        "build_tool_revisions": {
            name: "b" * 40 for name in ("dependency-file-generator", "rapids-build-backend", "rapids-cmake")
        },
    }
    if revision is not None:
        train["rapids_metadata_revision"] = revision
    source, output = tmp_path / "train.json", tmp_path / "inputs.json"
    source.write_text(json.dumps(train))
    result = subprocess.run(  # noqa: S603 # nosec B603 B607 - fixed checked-in script, explicit temporary paths.
        [
            "/bin/bash",
            str(SCRIPT),
            "--train",
            str(source),
            "--output",
            str(output),
            "--target-unit",
            "container:example",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if revision == "main":
        if result.returncode == 0:
            pytest.fail("mutable metadata revision was admitted")
        return
    if result.returncode:
        pytest.fail(result.stderr)
    tools = json.loads(output.read_text())["build_tools"]
    if revision is None:
        if "rapids-metadata" in tools:
            pytest.fail("legacy train invented a metadata revision")
    elif tools.get("rapids-metadata") != {"repository": "rapidsai/rapids-metadata", "revision": revision}:
        pytest.fail("metadata source revision is absent from candidate build identity")
