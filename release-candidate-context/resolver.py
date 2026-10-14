# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
"""Resolve candidate metadata for unchanged caller workflows from an exact run binding."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # nosec B404 - invokes AWS CLI with an argument list, never a shell.
import time
from collections.abc import Mapping
from pathlib import Path

CONTROLLER_BINDING_PREFIX = "controller-state/run-bindings"


def validate_binding(binding: Mapping, environment: Mapping[str, str], source_sha: str, script: str) -> dict[str, str]:
    """Reject unrelated runs/sources and custom jobs not enrolled by the controller."""
    try:
        run_attempt = int(environment["GITHUB_RUN_ATTEMPT"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("candidate run attempt is missing or invalid") from error
    if run_attempt < 1 or str(run_attempt) != environment["GITHUB_RUN_ATTEMPT"]:
        raise ValueError("candidate run attempt is missing or invalid")

    expected = {
        "schema_version": 2,
        "repository": environment["GITHUB_REPOSITORY"],
        "run_id": environment["GITHUB_RUN_ID"],
        "run_attempt": run_attempt,
        "source_sha": source_sha,
        "event_name": "workflow_dispatch",
    }
    if type(binding.get("run_attempt")) is not int:
        raise ValueError("candidate binding run attempt must be an integer")
    if any(binding.get(key) != value for key, value in expected.items()):
        raise ValueError("candidate binding does not match this repository, run, attempt, or source SHA")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_sha):
        raise ValueError("candidate source SHA must be a full object ID")
    if binding.get("workflow") != "build.yaml":
        raise ValueError("candidate binding has an unsupported workflow")
    workflow_ref = environment["GITHUB_WORKFLOW_REF"]
    expected_prefix = f"{binding['repository']}/.github/workflows/build.yaml@refs/heads/"
    if workflow_ref != expected_prefix + binding.get("workflow_ref", ""):
        raise ValueError("candidate binding does not match the caller workflow ref")
    if environment["GITHUB_EVENT_NAME"] != "workflow_dispatch":
        raise ValueError("candidate binding requires a workflow dispatch")
    digest = binding.get("train_sha256", "")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("candidate binding has no valid train digest")
    mode = "build"
    if script:
        mode = binding.get("custom_jobs", {}).get(script)
        if mode not in {"test", "artifact"}:
            raise ValueError(f"custom candidate job has no reviewed policy: {script}")
    return {"train-sha256": digest, "custom-mode": mode}


def read_binding(bucket: str, key: str, run=subprocess.run, sleep=time.sleep, attempts: int = 12) -> Mapping:
    """Wait at most 55 seconds for the controller's post-dispatch binding write."""
    for attempt in range(attempts):
        response = run(("aws", "s3", "cp", f"s3://{bucket}/{key}", "-"), text=True, capture_output=True, check=False)
        if response.returncode == 0:
            binding = json.loads(response.stdout)
            if not isinstance(binding, dict):
                raise ValueError("candidate binding must be a JSON object")
            return binding
        if "404" not in response.stderr and "NoSuchKey" not in response.stderr:
            raise ValueError("unable to read candidate binding; check candidate-store access")
        if attempt + 1 < attempts:
            sleep(5)
    raise ValueError("candidate run has no controller binding; refusing to guess a train")


def binding_key(repository: str, run_id: str, run_attempt: str) -> str:
    """Build the exact controller-owned key that producers can only read."""
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise ValueError("invalid repository identity")
    if re.fullmatch(r"[1-9][0-9]*", run_id) is None:
        raise ValueError("invalid run identity")
    if re.fullmatch(r"[1-9][0-9]*", run_attempt) is None:
        raise ValueError("invalid run attempt")
    owner, repository_name = repository.split("/", maxsplit=1)
    return f"{CONTROLLER_BINDING_PREFIX}/{owner}/{repository_name}/{run_id}.{run_attempt}.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", default="rapids-release-candidates")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--script", default="")
    args = parser.parse_args()
    repository = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    key = binding_key(repository, run_id, run_attempt)
    binding = read_binding(args.bucket, key)
    outputs = validate_binding(binding, os.environ, args.source_sha, args.script)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for name, value in outputs.items():
            output.write(f"{name}={value}\n")


if __name__ == "__main__":
    main()
