"""Check whether a GHA workflow has run successfully in the last N days."""
# ruff: noqa: INP001

import argparse
import itertools
import os
import re
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
    """Check whether a GHA workflow has run successfully in the last N days.

    Returns True if the workflow has not run successfully in the last N days, False
    otherwise (values are inverted for use as a return code).
    """
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

    latest_success = {}
    for branch, branch_runs in itertools.groupby(runs, key=lambda r: r["head_branch"]):
        if not re.match("branch-[0-9]{2}.[0-9]{2}", branch):
            continue

        latest_success[branch] = None
        for run in sorted(branch_runs, key=lambda r: r["run_started_at"], reverse=True):
            days_since_run = (now - datetime.fromisoformat(run["run_started_at"])).days
            if days_since_run > max_days_without_success:
                break
            if run["conclusion"] in GOOD_STATUSES:
                latest_success[branch] = run
                break

    latest_branch = max(latest_success)
    success = latest_success[latest_branch] is not None

    if success:
        print(  # noqa: T201
            f"The most recent successful run of the {workflow_id} workflow on "
            f"{latest_branch} was "
            f"{datetime.fromisoformat(latest_success[latest_branch]['run_started_at'])}, "
            f"which is within the last {max_days_without_success} days. View logs:"
            f"\n  - {latest_success[latest_branch]['html_url']}"
        )
    else:
        print(  # noqa: T201
            f"{latest_branch} has no successful runs of {workflow_id} in the last "
            f"{max_days_without_success} days"
        )

    # We are producing Unix return codes so success/failure is inverted from the
    # expected Python boolean values.
    return not success


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=str, help="Repository name")
    parser.add_argument(
        "--repo-owner",
        default="rapidsai",
        help="Repository organization/owner",
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
