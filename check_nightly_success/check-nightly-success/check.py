import requests
import itertools
from datetime import datetime
import sys
import argparse
import os

# Constants
GITHUB_TOKEN = os.environ["RAPIDS_GH_TOKEN"]
GOOD_STATUSES = ("success",) # noqa


def main(
    repo: str,
    repo_owner: str,
    workflow_id: str,
    max_days_without_success: int,
):
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    url = f"https://api.github.com/repos/{repo_owner}/{repo}/actions/workflows/{workflow_id}/runs"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    runs = response.json()["workflow_runs"]
    tz = datetime.fromisoformat(runs[0]["run_started_at"]).tzinfo
    NOW = datetime.now(tz=tz)

    branch_ok = {}
    for branch, branch_runs in itertools.groupby(runs, key=lambda r: r["head_branch"]):
        if branch.startswith("v"):  # Skip tags
            continue

        branch_ok[branch] = False
        for run in sorted(branch_runs, key=lambda r: r["run_started_at"], reverse=True):
            if (
                NOW - datetime.fromisoformat(run["run_started_at"])
            ).days > max_days_without_success:
                break
            if run["conclusion"] in GOOD_STATUSES:
                branch_ok[branch] = True
                break

    all_success = all(branch_ok.values())
    if not all_success:
        print(
            f"Branches with no successful runs of {workflow_id} in the last "
            f"{max_days_without_success} days: "
            f"{', '.join(k for k, v in branch_ok.items() if not v)}"
        )
    return not all(branch_ok.values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=str, help="Repository name")
    parser.add_argument(
        "--repo-owner", default="rapidsai", help="Repository organziation/owner"
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
        )
    )
