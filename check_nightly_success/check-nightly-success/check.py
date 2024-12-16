"""Check whether a GHA workflow has run successfully in the last N days."""
# ruff: noqa: INP001

import argparse
import itertools
import os
import sys
from datetime import datetime

import requests

# Constants
GITHUB_TOKEN = os.environ["RAPIDS_GH_TOKEN"]
GOOD_STATUSES = {"success"}


def main(
    repo: str,
    repo_owner: str,
    workflow_id: str,
    max_days_without_success: int,
    num_attempts: int = 5,
) -> bool:
    """Check whether a GHA workflow has run successfully in the last N days."""
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    url = f"https://api.github.com/repos/{repo_owner}/{repo}/actions/workflows/{workflow_id}/runs"
    exceptions = []
    for _ in range(num_attempts):
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            break
        except requests.RequestException as e:
            exceptions.append(e)
    else:
        sep = "\n\t"
        msg = (
            f"Failed to fetch {url} after {num_attempts} attempts with the following "
            f"errors: {sep}{'{sep}'.join(exceptions)}"
        )
        raise RuntimeError(msg)

    runs = response.json()["workflow_runs"]
    tz = datetime.fromisoformat(runs[0]["run_started_at"]).tzinfo
    now = datetime.now(tz=tz)

    branch_ok = {}
    for branch, branch_runs in itertools.groupby(runs, key=lambda r: r["head_branch"]):
        if branch.startswith("v"):  # Skip tags
            continue

        branch_ok[branch] = False
        for run in sorted(branch_runs, key=lambda r: r["run_started_at"], reverse=True):
            if (
                now - datetime.fromisoformat(run["run_started_at"])
            ).days > max_days_without_success:
                break
            if run["conclusion"] in GOOD_STATUSES:
                branch_ok[branch] = True
                break

    all_success = all(branch_ok.values())
    if not all_success:
        print(  # noqa: T201
            f"Branches with no successful runs of {workflow_id} in the last "
            f"{max_days_without_success} days: "
            f"{', '.join(k for k, v in branch_ok.items() if not v)}",
        )
    return not all(branch_ok.values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=str, help="Repository name")
    parser.add_argument(
        "--repo-owner",
        default="rapidsai",
        help="Repository organziation/owner",
    )
    parser.add_argument("--workflow-id", default="test.yaml", help="Workflow ID")
    parser.add_argument(
        "--max-days-without-success",
        type=int,
        default=7,
        help="Maximum number of days without a successful run",
    )
    args = parser.parse_args()

    sys.exit(
        main(
            args.repo,
            args.repo_owner,
            args.workflow_id,
            args.max_days_without_success,
        ),
    )
