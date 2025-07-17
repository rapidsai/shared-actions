# Copyright (c) 2024-2025, NVIDIA CORPORATION.

"""Check whether a GHA workflow has run successfully in the last N days."""
# ruff: noqa: INP001

import argparse
import os
import re
import sys
from collections import defaultdict
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
    workflow_active_for_max_days = {}
    # Rather frustratingly, the workflow runs returned from the GitHub API can
    # have alternating ordering of `head_branch`
    # e.g.
    #   run[0]['head_branch'] == "branch-25.02"
    #   run[1]['head_branch'] == "branch-25.04"
    #   run[2]['head_branch'] == "branch-25.02"
    #
    # In this situation, the behavior of `itertools.groupby` (previously used
    # here) is to only group _consecutive_ runs, so the results of the
    # subsequent branch match (i.e.  the second group of `branch-25.02` runs)
    # will overwrite the results of the first one, potentially overwriting a
    # previous success. The snippet below unifies the groups so it's more like a
    # SQL groupby and there is no chance of overwriting.
    branch_dict = defaultdict(list)
    for run in runs:
        branch_dict[run["head_branch"]].append(run)

    for branch, branch_runs in branch_dict.items():
        # Only consider RAPIDS development branches, which are named 'main' or
        # have versions like '25.02' (RAPIDS CalVer) or '0.42' (ucxx, ucx-py)
        if branch != "main" and not re.match("branch-[0-9]{1,2}.[0-9]{2}", branch):
            continue

        latest_success[branch] = None
        runs = sorted(branch_runs, key=lambda r: r["run_started_at"], reverse=True)
        for run in runs:
            days_since_run = (now - datetime.fromisoformat(run["run_started_at"])).days
            if days_since_run > max_days_without_success:
                break
            if run["conclusion"] in GOOD_STATUSES:
                latest_success[branch] = run
                break

        workflow_active_for_max_days[branch] = False
        if len(runs) > 0:
            run = runs[-1]
            days_since_run = (now - datetime.fromisoformat(run["run_started_at"])).days
            if days_since_run > max_days_without_success:
                workflow_active_for_max_days[branch] = True

        # Edge case where a `main` branch exists but has _no_ runs.
        # In that scenario, remove the branch from consideration
        if latest_success[branch] is None:
            latest_success.pop(branch)

    latest_branch = max(latest_success)
    has_latest_success = latest_success[latest_branch] is not None

    # We are producing Unix return codes so success/failure is inverted from the
    # expected Python boolean values.
    if has_latest_success:
        print(  # noqa: T201
            f"The most recent successful run of the {workflow_id} workflow on "
            f"{latest_branch} was "
            f"{datetime.fromisoformat(latest_success[latest_branch]['run_started_at'])}, "
            f"which is within the last {max_days_without_success} days. View logs:"
            f"\n  - {latest_success[latest_branch]['html_url']}"
        )
        return 0
    elif not workflow_active_for_max_days[latest_branch]:
        print(  # noqa: T201
            f"The oldest run of the {workflow_id} workflow on {latest_branch} was less "
            f"than {max_days_without_success} days ago. This exempts the workflow from "
            "check-nightly-success because the workflow has not been running for very long."
        )
        return 0

    print(  # noqa: T201
        f"{latest_branch} has no successful runs of {workflow_id} in the last {max_days_without_success} days"
    )
    return 1


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
