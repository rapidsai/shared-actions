# Copyright (c) 2019-2024, NVIDIA CORPORATION.
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
"""Process a GitHub Actions workflow log and send OpenTelemetry span data."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.context import attach, detach
from opentelemetry.propagate import extract
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from opentelemetry.trace.status import StatusCode

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

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


logging.basicConfig(level=logging.WARNING)

SpanProcessor = BatchSpanProcessor


def _parse_attribute_or_env_var_file(filename: str) -> dict[str, str]:
    with Path(filename).open() as f:
        return {
            line.split("=", 1)[0]: line.split("=", 1)[1].strip().strip('"')
            for line in f
        }


def _date_str_to_epoch(date_str: str, value_if_not_set: int = 0) -> int:
    if date_str:
        # replace bit is to attach the UTC timezone to our datetime object, so
        # that it doesn't "help" us by adjusting our string value, which is
        # already in UTC
        timestamp_ns = int(
            datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
            * 1e9
        )
    else:
        timestamp_ns = value_if_not_set or 0
    return timestamp_ns


def _map_conclusion_to_status_code(conclusion: str) -> StatusCode:
    match conclusion:
        case "success":
            return StatusCode.OK
        case "failure":
            return StatusCode.ERROR
        case _:
            return StatusCode.UNSET


class LoadTraceParentGenerator(IdGenerator):
    """W3C trace context generator that loads trace ID from provided string.

    Having the trace ID be something that we control and compute externally
    to this script is useful for tying together logs and metrics with our
    traces.
    """

    def __init__(self, traceparent: str, **kwargs: Mapping[str, Any]) -> None:
        """Load the trace ID and span ID from the user-provided string."""
        ctx = extract(
            carrier={"traceparent": traceparent},
        )
        self.context = next(iter(ctx.values()))
        self.kwargs = kwargs

    def generate_span_id(self) -> int:
        """Get a new span ID.

        The OpenTelemetry SDK calls this function when it is creating a span.
        We use the value that we computed in the workflow, which is a
        deterministic combination of github actions environment variables and
        the job and (optionally) step name. In our case, this is only the job
        name, and it is set in workflows by setting OTEL_SERVICE_NAME when
        stashing base env vars.
        https://github.com/rapidsai/shared-actions/blob/main/telemetry-impls/traceparent.sh
        """
        return self.context.get_span_context().span_id

    def generate_trace_id(self) -> int:
        """Get a new trace ID.

        The OpenTelemetry SDK calls this function when it is creating a span.
        We use the value that we computed in the workflow, which is a
        deterministic combination of github actions environment variables.
        https://github.com/rapidsai/shared-actions/blob/main/telemetry-impls/traceparent.sh
        """
        return self.context.get_span_context().trace_id


class RapidsSpanIdGenerator(LoadTraceParentGenerator):
    """W3C trace context generator that varies span ID with job_name and step_name."""

    def __init__(
        self, traceparent: str, job_name: str, **kwargs: Mapping[str, Any]
    ) -> None:
        """Load traceparent and set job_name."""
        super().__init__(traceparent)
        self.job_name = job_name
        self.step_name = None
        self.kwargs = kwargs

    def update_step_name(self, step_name: str) -> None:
        """Update the step name that will be used for the next generate_span_id call."""
        self.step_name = step_name

    def generate_span_id(self) -> int:
        """Get a span ID.

        This method gets called by the OpenTelemetry SDK when creating a span.

        Our span IDs are deterministic, but unique within a github actions run.
        This assumes that the key of (trace_id, job_name, step_name) is unique.
        In other words, jobs can have steps that have the same name as other
        jobs, but no 2 jobs can have the same name.
        """
        span_id = hashlib.sha256()
        span_id.update(str(self.generate_trace_id()).encode())
        span_id.update(bytes(self.job_name.encode()))
        if self.step_name:
            span_id.update(bytes(self.step_name.encode()))
        return int(span_id.hexdigest()[:16], 16)


class SpanCreator:
    """One SpanCreator object per job."""

    def __init__(
        self,
        job_dict: dict[str, Any],
        id_generator: Callable,
        job_span_name: str = "child workflow root",
        attr_include_filter: str | None = None,
    ) -> None:
        """Read some data from the input dict and env file, then create a span."""
        self._load_env_vars()
        self._load_attr_file(attr_include_filter=attr_include_filter)

        self.attributes["service.name"] = job_dict["name"]
        self.id = job_dict["id"]
        self.steps: list[dict[str, str]] = job_dict["steps"]
        self.created = _date_str_to_epoch(job_dict["created_at"])
        self.started = _date_str_to_epoch(job_dict["started_at"], self.created)
        self.status = _map_conclusion_to_status_code(job_dict["conclusion"])
        # this may get later in time as we progress through the steps. It is
        # common to have the job completion time be earlier than the end of
        # the final cleanup steps
        self.last_timestamp = self.created

        self.id_generator = id_generator(
            traceparent=self.env_vars["TRACEPARENT"], job_name=job_dict["name"]
        )
        provider = TracerProvider(
            resource=Resource(attributes=self.attributes),
            id_generator=self.id_generator,
        )
        provider.add_span_processor(span_processor=SpanProcessor(OTLPSpanExporter()))
        self.tracer = trace.get_tracer(
            "GitHub Actions parser", "0.0.1", tracer_provider=provider
        )
        self.job_span = self.tracer.start_span(
            name=job_span_name,
            start_time=self.created,
        )
        self.job_span.set_status(_map_conclusion_to_status_code(job_dict["conclusion"]))
        # The context here makes the step spans be children of this job
        self.old_context_token = self._activate_context()

    def _load_env_vars(self) -> None:
        self.env_vars = _parse_attribute_or_env_var_file(
            "telemetry-artifacts/telemetry-env-vars"
        )

    def _load_attr_file(
        self,
        attr_include_filter: str | None = None,
    ) -> None:
        attribute_files = list(Path.cwd().glob("telemetry-artifacts/attrs-*"))
        if attribute_files:
            attribute_file = attribute_files[0]
            self.attributes = _parse_attribute_or_env_var_file(
                attribute_file.as_posix()
            )
        else:
            self.attributes = {}
        if attr_include_filter:
            self.attributes = {
                k: v
                for k, v in self.attributes.items()
                if k.startswith(attr_include_filter)
            }

    def _activate_context(self) -> object:
        span_context = SpanContext(
            trace_id=self.id_generator.generate_trace_id(),
            span_id=self.id_generator.generate_span_id(),
            is_remote=True,
            trace_flags=TraceFlags(0x01),
        )
        self.context = trace.set_span_in_context(NonRecordingSpan(span_context))
        return attach(self.context)

    def process_steps(self) -> None:
        """Loop over steps and outputs span data to endpoint."""
        for step in self.steps:
            start = _date_str_to_epoch(step["started_at"], self.last_timestamp)
            end = _date_str_to_epoch(step["completed_at"], start)
            if not hasattr(self.id_generator, "update_step_name"):
                return
            self.id_generator.update_step_name(step["name"])

            if (end - start) / 1e9 > 1:
                with self.tracer.start_as_current_span(
                    name=step["name"],
                    start_time=start,
                    end_on_exit=False,
                ) as step_span:
                    step_span.set_status(
                        _map_conclusion_to_status_code(step["conclusion"])
                    )
                    step_span.end(end)

            self.last_timestamp = max(end, self.last_timestamp)

    def _add_start_delay_time_span(self) -> None:
        if not hasattr(self.id_generator, "update_step_name"):
            return
        self.id_generator.update_step_name("start delay time")
        with self.tracer.start_as_current_span(
            name="Start delay time",
            start_time=self.created,
            end_on_exit=False,
        ) as delay_span:
            delay_span.set_status(StatusCode.OK)
            delay_span.end(self.started)

    def end(self) -> None:
        """Add delay span and end current job span."""
        self._add_start_delay_time_span()
        self.job_span.end(self.last_timestamp)
        detach(self.old_context_token)


def main() -> None:
    """Run the core logic."""
    with Path("all_jobs.json").open() as f:
        jobs = json.loads(f.read())

    root_span_creator = SpanCreator(
        job_dict=jobs[0],
        id_generator=LoadTraceParentGenerator,
        job_span_name="Workflow root",
        attr_include_filter="git.",
    )

    for job in jobs:
        job_span_creator = SpanCreator(job_dict=job, id_generator=RapidsSpanIdGenerator)
        job_span_creator.process_steps()
        job_span_creator.end()
        root_span_creator.last_timestamp = max(
            root_span_creator.last_timestamp, job_span_creator.last_timestamp
        )
        if job_span_creator.status == StatusCode.ERROR:
            root_span_creator.job_span.set_status(StatusCode.ERROR)
    root_span_creator.end()


if __name__ == "__main__":
    main()
