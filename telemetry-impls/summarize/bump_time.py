#!/usr/bin/env python
# Copyright (c) 2024-2026, NVIDIA CORPORATION.

# This script is meant to act on an 'all_jobs.json' file that comes from
# the summarize job when debug info is enabled. Bumping the time makes
# it easier to re-run the span-sending python script and check results
# in either Jaeger or Grafana

import datetime
import json

with open("all_jobs.json") as f:
    jobs = json.load(f)


def _parse_time(x: str) -> int:
    return int(datetime.datetime.strptime(x, "%Y-%m-%dT%H:%M:%SZ").timestamp() * 1e9)  # noqa: DTZ007


start_time = _parse_time(jobs[0]["created_at"])
needed_time = _parse_time(jobs[-3]["completed_at"]) - _parse_time(jobs[0]["created_at"])
new_start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=60)

for idx, job in enumerate(jobs):
    if job["created_at"]:
        job["created_at"] = (
            new_start_time + datetime.timedelta(seconds=(_parse_time(job["created_at"]) - start_time) / 1e9)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    if job["started_at"]:
        job["started_at"] = (
            new_start_time + datetime.timedelta(seconds=(_parse_time(job["started_at"]) - start_time) / 1e9)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    if job["completed_at"]:
        job["completed_at"] = (
            new_start_time + datetime.timedelta(seconds=(_parse_time(job["completed_at"]) - start_time) / 1e9)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    steps = []
    for step in job["steps"]:
        if step["started_at"]:
            step["started_at"] = (
                new_start_time + datetime.timedelta(seconds=(_parse_time(step["started_at"]) - start_time) / 1e9)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        if step["completed_at"]:
            step["completed_at"] = (
                new_start_time + datetime.timedelta(seconds=(_parse_time(step["completed_at"]) - start_time) / 1e9)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        steps.append(step)
    job["steps"] = steps

    jobs[idx] = job


with open("all_jobs.json", "w") as f:
    json.dump(jobs, f)
