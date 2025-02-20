# Copyright (c) 2019-2025, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Process a GitHub workflow log record and output OpenTelemetry span data."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace.status import StatusCode

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

match os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL"):
    case "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    case "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
    case _:
        from opentelemetry.sdk.trace.export import (
            ConsoleSpanExporter as OTLPSpanExporter,
        )


logging.basicConfig(level=logging.DEBUG)

SpanProcessor = BatchSpanProcessor


def parse_attributes(attrs: os.PathLike | str | None) -> dict[str, str]:
    """Attempt to parse attributes in a given list `attrs`."""
    attrs_list: list[str]
    if not attrs:
        return {}
    try:
        with Path(attrs).open() as attribute_file:
            attrs_list = attribute_file.readlines()
    except FileNotFoundError:
        attrs_list = str(attrs).split(",")
    attributes = {}
    for attr in attrs_list:
        key, value = attr.split("=", 1)
        attributes[key] = value.strip().strip('"')
        logging.debug("Attribute parsed: Key: %s, Value: %s", key, value)
    return attributes


def date_str_to_epoch(date_str: str, value_if_not_set: int | None = 0) -> int:
    """Github logs come in RFC 3339; this converts to nanoseconds since epoch."""
    if date_str:
        # replace bit is to attach the UTC timezone to our datetime object, so
        # that it doesn't "help" us by adjusting our string value, which is
        # already in UTC
        timestamp_ns = int(
            datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1e9
        )
    else:
        timestamp_ns = value_if_not_set or 0
    return timestamp_ns


def map_conclusion_to_status_code(conclusion: str) -> StatusCode:
    """Map string conclusion from logs to OTel enum."""
    if conclusion == "success":
        return StatusCode.OK
    if conclusion == "failure":
        return StatusCode.ERROR
    return StatusCode.UNSET


class RapidsSpanIdGenerator(IdGenerator):
    """ID Generator that generates span IDs. Trace IDs come from elsewhere."""

    def __init__(self, trace_id: int, job_name: str) -> None:
        """Initialize generator with trace ID and initial job_name."""
        self.trace_id = trace_id
        self.job_name = job_name
        self.step_name = None

    def update_job_name(self, job_name: str) -> None:
        """Update the job name, which feeds into computing the span name."""
        self.job_name = job_name
        logging.debug("Job name updated: %s", self.job_name)

    def update_step_name(self, step_name: str) -> None:
        """Update the step name, which feeds into computing the span name."""
        self.step_name = step_name
        logging.debug("Step name updated: %s", self.step_name)

    def generate_span_id(self) -> int:
        """Get a new span ID.

        This is called by the OpenTelemetry SDK as-is, without arguments.
        To change the generated value here, use the update methods.
        """
        span_id = hashlib.sha256()
        span_id.update(str(self.trace_id).encode())
        span_id.update(bytes(self.job_name.encode()))
        if self.step_name:
            span_id.update(bytes(self.step_name.encode()))
        return int(span_id.hexdigest()[:16], 16)

    def generate_trace_id(self) -> int:
        """Return the trace ID that is stored at initialization."""
        return self.trace_id


@dataclass
class Compiler:
    hits: int = 0
    misses: int = 0
    errors: int = 0

    @property
    def requests(self) -> int:
        """Calculate the total requests."""
        return self.hits + self.misses + self.errors

    @property
    def hit_rate(self) -> float:
        """Calculate the cache hit rate."""
        return self.hits / self.requests if self.requests > 0 else 0

    @property
    def miss_rate(self) -> float:
        """Calculate the cache miss rate."""
        return self.misses / self.requests if self.requests > 0 else 0

    @property
    def error_rate(self) -> float:
        """Calculate the cache error rate."""
        return self.errors / self.requests if self.requests > 0 else 0


class SccacheStats:
    requests: int = 0
    compilers: dict[str, Compiler]

    def __init__(self, requests: int, compilers: dict[str, Compiler]) -> None:
        self.requests = requests
        self.compilers = compilers

    @property
    def hits(self) -> int:
        """Calculate the total cache hits."""
        return sum(v.hits for v in self.compilers.values())

    @property
    def misses(self) -> int:
        """Calculate the total cache misses."""
        return sum(v.misses for v in self.compilers.values())

    @property
    def errors(self) -> int:
        """Calculate the total cache errors."""
        return sum(v.errors for v in self.compilers.values())

    @property
    def hit_rate(self) -> float:
        """Calculate the total cache hit rate."""
        return self.hits / self.requests if self.requests > 0 else 0

    @property
    def miss_rate(self) -> float:
        """Calculate the total cache miss rate."""
        return self.misses / self.requests if self.requests > 0 else 0

    @property
    def error_rate(self) -> float:
        """Calculate the total cache error rate."""
        return self.errors / self.requests if self.requests > 0 else 0


def get_sccache_stats(artifact_folder: Path) -> dict[str, str]:
    """Get sccache stats from the artifact folder."""
    stats_files = artifact_folder.glob("sccache-stats*.txt")
    logging.debug("SCCache stats files: %s", stats_files)
    parsed_stats = {}
    lang_line_match = re.compile(r"Cache (?P<result>\w+) \((?P<lang>\w+)[^)]*\)\s*(?P<count>\d+)")
    for file in stats_files:
        with file.open() as f:
            stats = SccacheStats(requests=0, compilers={"c": Compiler(), "cpp": Compiler(), "cuda": Compiler()})
            for line in f:
                if match := re.match(r"^Compile\srequests\s+(\d+).*", line, re.IGNORECASE):
                    stats.requests = int(match.group(1))
                elif match := lang_line_match.match(line):
                    compiler = stats.compilers[match.group("lang")]
                    setattr(compiler, match.group("result"), int(match.group("count")))
        stats_file_name = re.findall(r"sccache-stats[-]?(?P<name>\w+).txt", file.name)
        if stats_file_name:
            stats_file_name = stats_file_name[0]
        else:
            stats_file_name = "main_process"
        parsed_stats[stats_file_name] = stats
    return parsed_stats


def process_job_blob(  # noqa: PLR0913
    trace_id: int,
    job: Mapping[str, Any],
    env_vars: Mapping[str, str],
    first_timestamp: int,
    last_timestamp: int,
) -> int:
    """Transform job JSON into an OTel span."""
    # This is the top-level workflow, which we account for with the root
    # trace above
    if job["name"] == env_vars["OTEL_SERVICE_NAME"]:
        logging.debug("Job name is the same as the service name: %s", job["name"])
        return last_timestamp
    # this cuts off matrix info from the job name, such that grafana can group
    # these by name
    if "/" in job["name"]:
        job_name, matrix_part = job["name"].split("/", 1)
        job_name = job_name.strip()
        matrix_part = matrix_part.strip()
    else:
        job_name, matrix_part = job["name"], None

    job_id = job["id"]
    logging.info("Processing job: %s", job_name)
    job_create = date_str_to_epoch(job["created_at"], first_timestamp)
    job_start = date_str_to_epoch(job["started_at"], first_timestamp)
    # this may get later in time as we progress through the steps. It is
    # common to have the job completion time be earlier than the end of
    # the final cleanup steps
    job_last_timestamp = date_str_to_epoch(job["completed_at"], job_start)

    if job_start == 0:
        logging.info("Job is empty (no start time) - bypassing")
        return last_timestamp

    artifact_folder = Path.cwd() / f"telemetry-artifacts/telemetry-tools-artifacts-{job_id}"
    attributes = {}
    if (artifact_folder / "attrs").exists():
        logging.debug("Found attribute file for job: %s", job_id)
        attributes = parse_attributes(artifact_folder / "attrs")
    else:
        logging.debug("No attribute metadata found for job: %s", job_id)

    attributes["service.name"] = job_name

    sccache_stats = get_sccache_stats(artifact_folder)

    job_provider = TracerProvider(
        resource=Resource(attributes),
        id_generator=RapidsSpanIdGenerator(trace_id=trace_id, job_name=job["name"]),
    )
    job_provider.add_span_processor(span_processor=SpanProcessor(OTLPSpanExporter()))
    job_tracer = trace.get_tracer("GitHub Actions parser", "0.0.1", tracer_provider=job_provider)

    with job_tracer.start_as_current_span(
        name=matrix_part or job["name"],
        start_time=job_create,
        end_on_exit=False,
    ) as job_span:
        logging.debug("Job span created: %s", job_span)
        job_span.set_status(map_conclusion_to_status_code(job["conclusion"]))

        job_provider.id_generator.update_step_name("Start delay time")
        delay_span = job_tracer.start_span(name="Start delay time", start_time=job_create)
        delay_span.end(job_start)
        logging.debug("Delay span created: %s", delay_span)

        for step in job["steps"]:
            step_start = date_str_to_epoch(step["started_at"], job_last_timestamp)
            step_end = date_str_to_epoch(step["completed_at"], step_start)
            job_last_timestamp = max(step_end, job_last_timestamp)
            job_provider.id_generator.update_step_name(step["name"])
            # Reset attributes for each step
            span_attributes = {}

            if (step_end - step_start) / 1e9 > 1:
                logging.debug("processing step: %s", step["name"])
                job_provider.id_generator.update_step_name(step["name"])
                # Only add sccache attributes if this is a build step
                if re.match(r"(?:[\w+]+\sbuild$)|(?:Build\sand\srepair.*)", step["name"], re.IGNORECASE):
                    logging.debug("Adding sccache attributes for step: %s", step["name"])
                    for file_name, stats in sccache_stats.items():
                        span_attributes[f"sccache.{file_name}.hit_rate"] = stats.hit_rate
                        span_attributes[f"sccache.{file_name}.miss_rate"] = stats.miss_rate
                        span_attributes[f"sccache.{file_name}.error_rate"] = stats.error_rate
                        span_attributes[f"sccache.{file_name}.requests"] = stats.requests
                        for lang, lang_stats in stats.compilers.items():
                            span_attributes[f"sccache.{file_name}.{lang}.hit_rate"] = lang_stats.hit_rate
                            span_attributes[f"sccache.{file_name}.{lang}.miss_rate"] = lang_stats.miss_rate
                            span_attributes[f"sccache.{file_name}.{lang}.error_rate"] = lang_stats.error_rate
                            span_attributes[f"sccache.{file_name}.{lang}.requests"] = lang_stats.requests

                # TODO: Ninja log files?
                # TODO: file sizes of packages or their contents

                step_span = job_tracer.start_span(
                    name=step["name"],
                    start_time=step_start,
                    attributes=span_attributes,
                )
                logging.debug("Step span created: %s", step_span)
                # TODO: use step_span.record_exception(exc) to capture errors. exc is an exception object,
                # so if we are reading from a log file, we need to read the log file into an exception object.
                step_span.set_status(map_conclusion_to_status_code(step["conclusion"]))
                step_span.end(step_end)
                logging.debug("Step span ended: %s", step_span)
            else:
                logging.debug("Skipping step: %s, because its duration is < 1s", step["name"])
        job_span.end(job_last_timestamp)
        logging.debug("Job span ended: %s", job_span)
    return last_timestamp


def main() -> None:
    """Run core functionality."""
    with Path("all_jobs.json").open() as f:
        jobs = json.loads(f.read())

    first_timestamp = date_str_to_epoch(jobs[0]["created_at"])
    # track the latest timestamp observed and use it for any unavailable times.
    last_timestamp = date_str_to_epoch(jobs[0]["completed_at"], 0)

    attribute_folders = list(Path.cwd().glob("telemetry-artifacts/telemetry-tools-artifacts-*"))
    logging.debug("Attribute folders: %s", attribute_folders)
    if attribute_folders:
        attribute_file = attribute_folders[0] / "attrs"
        attributes = parse_attributes(attribute_file.as_posix())
        env_vars = parse_attributes(attribute_folders[0] / "telemetry-env-vars")
        logging.debug("Env vars parsed from first attribute folder: %s", env_vars)
    else:
        attributes = {}
    global_attrs = {k: v for k, v in attributes.items() if k.startswith("git.")}
    global_attrs["service.name"] = env_vars["OTEL_SERVICE_NAME"]

    trace_id = int(env_vars["TRACEPARENT"].split("-")[1], 16)

    provider = TracerProvider(
        resource=Resource(global_attrs),
        id_generator=RapidsSpanIdGenerator(trace_id=trace_id, job_name=env_vars["OTEL_SERVICE_NAME"]),
    )
    provider.add_span_processor(span_processor=SpanProcessor(OTLPSpanExporter()))
    tracer = trace.get_tracer("GitHub Actions parser", "0.0.1", tracer_provider=provider)

    with tracer.start_as_current_span(
        name="Top-level workflow root", start_time=first_timestamp, end_on_exit=False
    ) as root_span:
        logging.debug("Root span created: %s", root_span)
        for job in jobs:
            last_timestamp = process_job_blob(
                trace_id=trace_id,
                job=job,
                env_vars=env_vars,
                first_timestamp=first_timestamp,
                last_timestamp=last_timestamp,
            )
        root_span.end(last_timestamp)
        logging.debug("Root span ended: %s", root_span)


if __name__ == "__main__":
    main()
