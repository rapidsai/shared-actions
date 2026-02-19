# Copyright (c) 2024-2026, NVIDIA CORPORATION.

"""Check whether a GHA workflow has run successfully in the last N days."""

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Constants
GITHUB_TOKEN = os.environ["GH_TOKEN"]


@dataclass
class _WorkflowRun:
    """GitHub workflow run data, filtered to only the fields this action cares about."""

    html_url: str
    run_started_at: datetime


@dataclass
class _ResponseData:
    data: list[_WorkflowRun]
    next_url: str | None


# We are producing Unix return codes so success/failure is inverted from the
# expected Python boolean values.
@dataclass
class ExitCode:
    FAILURE = 1
    SUCCESS = 0


class GitHubClient:
    def __init__(
        self,
        *,
        max_retries: int,
        retry_backoff_seconds: float,
        request_timeout_seconds: float,
    ) -> None:
        self.request_timeout_seconds = request_timeout_seconds
        retry = Retry(
            total=max_retries - 1,  # 1 initial attempt + (total) retries = max_retries attempts
            backoff_factor=retry_backoff_seconds,
            status_forcelist=(403, 404, 429, 500, 502, 503, 504),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session = requests.Session()
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _get_next_page(
        self,
        *,
        url: str,
        headers: dict[str, str],
        params: dict[str, int | str] | None,
    ) -> _ResponseData:
        """Get one page of results"""
        response = self._session.get(
            url,
            headers=headers,
            params=params,
            timeout=self.request_timeout_seconds,
        )
        response.raise_for_status()

        return _ResponseData(
            data=[
                _WorkflowRun(
                    html_url=workflow_run["html_url"],
                    run_started_at=datetime.fromisoformat(workflow_run["run_started_at"]),
                )
                for workflow_run in response.json()["workflow_runs"]
            ],
            next_url=response.links.get("next", dict()).get("url", None),
        )

    def get_all_runs(
        self,
        *,
        url: str,
        headers: dict[str, str],
        params: dict[str, int | str],
    ) -> list[_WorkflowRun]:
        """
        Paginate over requests to api.github.com/repos/{repo_owner}/{repo}/actions/workflows/{workflow_id}/runs
        and return all the results.
        """
        data = []
        page_num = 1
        while True:
            print(f"requesting page {page_num} of results")
            page = self._get_next_page(
                url=url,
                headers=headers,
                params=params,
            )
            data.extend(page.data)
            if page.next_url is None:
                break
            # just use the pagination URL, not the original query one
            url = page.next_url
            params = None  # type: ignore[assignment]
            page_num += 1
        return data


def main(
    *,
    repo: str,
    target_branch: str,
    workflow_id: str,
    max_days_without_success: int,
    num_attempts: int,
    request_page_size: int,
    request_timeout_seconds: float,
    retry_backoff_seconds: float,
) -> int:
    """Check whether a GHA workflow has run successfully in the last N days.

    Returns True if the workflow has not run successfully in the last N days, False
    otherwise (values are inverted for use as a return code).
    """
    # Timezones in GitHub API responses are guaranteed to be in UTC time.
    #
    # ref: https://docs.github.com/en/rest/using-the-rest-api/timezones-and-the-rest-api?apiVersion=2022-11-28
    #
    # This code is a little imprecise (doing the math in 'days' means that moving from 11:59p to 12:01a buys you
    # another 23 hours and 58 minutes of time), but that difference shouldn't be important for this action.
    #
    # Dealing with day-precision date-times makes filtering in the GitHub API simpler, see
    # https://docs.github.com/en/search-github/getting-started-with-searching-on-github/understanding-the-search-syntax#query-for-dates
    #
    oldest_date_to_pull = datetime.now(timezone.utc) - timedelta(days=max_days_without_success)

    # get all the matching runs
    client = GitHubClient(
        max_retries=num_attempts,
        request_timeout_seconds=request_timeout_seconds,
        retry_backoff_seconds=retry_backoff_seconds,
    )
    successful_runs = client.get_all_runs(
        url=f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_id}/runs",
        headers={"Authorization": f"token {GITHUB_TOKEN}"},
        params={
            # only care about runs from one branch (usually, the PR target branch)
            "branch": target_branch,
            # only care about successful runs
            "status": "success",
            # pull as many results per page as possible
            "per_page": request_page_size,
            # filter to recent-enough runs
            "created": f">={oldest_date_to_pull.strftime('%Y-%m-%d')}",
        },
    )

    # recent-enough, successful run = exit 0
    if successful_runs:
        most_recent_successful_run = max(successful_runs, key=lambda r: r.run_started_at)
        print(
            f"Found {len(successful_runs)} successful runs of workflow '{workflow_id}' on branch '{target_branch}' "
            f"in the previous {max_days_without_success} days (most recent: '{most_recent_successful_run.run_started_at}'). "  # noqa: E501
            f"View logs:\n - {most_recent_successful_run.html_url}"
        )
        return ExitCode.SUCCESS

    # It's ok for there to be 0 successful runs if the branch is fairly new or the workflow hasn't been running on it
    # very long.
    #
    # Code below looks for runs in the last `max_days_without_success * 2` days, to get an
    # approximation of the entire history without having an unbounded "list all runs from all time" type of query
    # (which could get expensive for very-active branches).
    lookback_days = max_days_without_success * 2
    oldest_date_to_pull = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    all_runs = client.get_all_runs(
        url=f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_id}/runs",
        headers={"Authorization": f"token {GITHUB_TOKEN}"},
        params={
            # only care about runs from one branch (usually, the PR target branch)
            "branch": target_branch,
            # pull as many results per page as possible
            "per_page": request_page_size,
            # filter to recent-enough runs
            "created": f">={oldest_date_to_pull.strftime('%Y-%m-%d')}",
        },
    )

    # Fail if there have not been any runs at all (to avoid silently skipping this check).
    if not all_runs:
        print(
            f"There were 0 runs (successful or unsuccessful) of workflow '{workflow_id}' on branch "
            f"'{target_branch}' in the last {lookback_days} days. "
            "To resolve this, run the workflow at least once or increase 'max-days-without-success'."
        )
        return ExitCode.FAILURE

    # If the oldest run on the branch was less than {max_days_without_success} ago, warn but allow the check to pass.
    oldest_run = min(all_runs, key=lambda r: r.run_started_at)
    days_since_oldest_run = (datetime.now(tz=timezone.utc) - oldest_run.run_started_at).days
    print(
        f"The oldest run of workflow '{workflow_id}' on branch '{target_branch}' was "
        f"{days_since_oldest_run} days ago ({oldest_run.run_started_at})."
    )
    if days_since_oldest_run < max_days_without_success:
        print(
            f"Because the latest run was less than 'max-days-without-success = {max_days_without_success}' days ago, "
            "this workflow is exempted from check-nightly-success. The check will start failing if there is not a "
            "successful run in the next few days."
        )
        return ExitCode.SUCCESS

    # There isn't a recent-enough success and the branch isn't exempted... fail.
    print(
        f"There were 0 successful runs of workflow '{workflow_id}' on branch '{target_branch}' in the last "
        f"{max_days_without_success} days."
    )
    return ExitCode.FAILURE


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo",
        type=str,
        required=True,
        help="Repository name with owner (e.g. 'rapidsai/cudf' not 'cudf')",
    )
    parser.add_argument(
        "--branch",
        type=str,
        required=True,
        help="Branch to check for recent workflow runs.",
    )
    parser.add_argument(
        "--workflow-id",
        type=str,
        required=True,
        help="Workflow ID (e.g. 'test.yaml')",
    )
    parser.add_argument(
        "--max-days-without-success",
        type=int,
        required=True,
        help="Maximum number of days without a successful run",
    )
    parser.add_argument(
        "--request-page-size",
        type=int,
        default=100,
        required=False,
        help="Number of responses per page of data. Decrease this to reduce memory usage.",
    )
    args = parser.parse_args()

    sys.exit(
        main(
            repo=args.repo,
            target_branch=args.branch,
            workflow_id=args.workflow_id,
            max_days_without_success=args.max_days_without_success,
            num_attempts=5,
            request_page_size=args.request_page_size,
            request_timeout_seconds=10,
            retry_backoff_seconds=0.5,
        ),
    )
