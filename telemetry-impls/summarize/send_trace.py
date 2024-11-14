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
"""Processes a GitHub Actions workflow log record and outputs OpenTelemetry span data."""


from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional, Dict

from opentelemetry import trace
from opentelemetry.context import attach, detach
from opentelemetry.propagate import extract
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.status import StatusCode
from opentelemetry.sdk.trace.id_generator import IdGenerator

match os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL"):
    case "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    case "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    case _:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter as OTLPSpanExporter


import logging
logging.basicConfig(level=logging.WARNING)

SpanProcessor = BatchSpanProcessor


def parse_attribute_file(filename: str) -> Dict[str, str]:
    attributes = {}
    with open(filename, "r") as attribute_file:
        for line in attribute_file.readlines():
            key, value = line.strip().split('=', 1)
            attributes[key] = value
    return attributes


def date_str_to_epoch(date_str: str, value_if_not_set: Optional[int] = 0) -> int:
    if date_str:
        # replace bit is to attach the UTC timezone to our datetime object, so
        # that it doesn't "help" us by adjusting our string value, which is
        # already in UTC
        timestamp_ns = int(datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1e9)
    else:
        timestamp_ns = value_if_not_set or 0
    return timestamp_ns


def map_conclusion_to_status_code(conclusion: str) -> StatusCode:
    if conclusion == "success":
        return StatusCode.OK
    elif conclusion == "failure":
        return StatusCode.ERROR
    else:
        return StatusCode.UNSET

def load_env_vars():
    env_vars = {}
    with open('telemetry-tools-env-vars/telemetry-env-vars') as f:
        for line in f.readlines():
            k, v = line.split("=", 1)
            env_vars[k] = v.strip().strip('"')
    return env_vars

class LoadTraceParentGenerator(IdGenerator):
    def __init__(self, traceparent) -> None:
        # purpose of this is to keep the trace ID constant if the same data is sent different times,
        # which mainly happens during testing. Having the trace ID be something that we control
        # will also be useful for tying together logs and metrics with our traces.
        ctx = extract(
            carrier={'traceparent': traceparent},
        )
        self.context = list(ctx.values())[0].get_span_context()


    def generate_span_id(self) -> int:
        """Get a new span ID.

        Returns:
            A 64-bit int for use as a span ID
        """
        return self.context.span_id

    def generate_trace_id(self) -> int:
        """Get a new trace ID.

        Implementations should at least make the 64 least significant bits
        uniformly random. Samplers like the `TraceIdRatioBased` sampler rely on
        this randomness to make sampling decisions.

        See `the specification on TraceIdRatioBased <https://github.com/open-telemetry/opentelemetry-specification/blob/main/specification/trace/sdk.md#traceidratiobased>`_.

        Returns:
            A 128-bit int for use as a trace ID
        """
        return self.context.trace_id

class RapidsSpanIdGenerator(IdGenerator):
    def __init__(self, trace_id, job_name) -> None:
        self.trace_id = trace_id
        self.job_name = job_name
        self.step_name = None

    def update_step_name(self, step_name):
        self.step_name = step_name

    def generate_span_id(self) -> int:
        """Get a new span ID.

        Returns:
            A 64-bit int for use as a span ID
        """
        span_id = hashlib.sha256()
        span_id.update(str(self.trace_id).encode())
        span_id.update(bytes(self.job_name.encode()))
        if self.step_name:
            span_id.update(bytes(self.step_name.encode()))
        return int(span_id.hexdigest()[:16], 16)

    def generate_trace_id(self) -> int:
        """Get a new trace ID.

        Implementations should at least make the 64 least significant bits
        uniformly random. Samplers like the `TraceIdRatioBased` sampler rely on
        this randomness to make sampling decisions.

        See `the specification on TraceIdRatioBased <https://github.com/open-telemetry/opentelemetry-specification/blob/main/specification/trace/sdk.md#traceidratiobased>`_.

        Returns:
            A 128-bit int for use as a trace ID
        """
        return self.trace_id


class GithubActionsParserGenerator(IdGenerator):
    def __init__(self, traceparent) -> None:
        # purpose of this is to keep the trace ID constant if the same data is sent different times,
        # which mainly happens during testing. Having the trace ID be something that we control
        # will also be useful for tying together logs and metrics with our traces.
        ctx = extract(
            carrier={'traceparent': traceparent},
        )
        self.context = list(ctx.values())[0].get_span_context()

    def update_span_job_name(self, new_name):
        self.job_name = new_name

    def update_span_step_name(self, new_name):
        self.step_name = new_name


    def generate_span_id(self) -> int:
        """Get a new span ID.

        Returns:
            A 64-bit int for use as a span ID
        """
        return self.context.span_id

    def generate_trace_id(self) -> int:
        """Get a new trace ID.

        Implementations should at least make the 64 least significant bits
        uniformly random. Samplers like the `TraceIdRatioBased` sampler rely on
        this randomness to make sampling decisions.

        See `the specification on TraceIdRatioBased <https://github.com/open-telemetry/opentelemetry-specification/blob/main/specification/trace/sdk.md#traceidratiobased>`_.

        Returns:
            A 128-bit int for use as a trace ID
        """
        return self.context.trace_id


def main(args):
    with open("all_jobs.json") as f:
        jobs = json.loads(f.read())

    env_vars = load_env_vars()

    first_timestamp = date_str_to_epoch(jobs[0]["created_at"])
    # track the latest timestamp observed and use it for any unavailable times.
    last_timestamp = date_str_to_epoch(jobs[0]["completed_at"])

    attribute_files = list(Path.cwd().glob(f"telemetry-tools-attrs-*/*"))
    if attribute_files:
        attribute_file = attribute_files[0]
        attributes = parse_attribute_file(attribute_file.as_posix())
    else:
        attributes = {}
    global_attrs = {}
    for k, v in attributes.items():
        if k.startswith('git.'):
            global_attrs[k] = v

    global_attrs['service.name'] = env_vars['OTEL_SERVICE_NAME']

    provider = TracerProvider(resource=Resource(global_attrs), id_generator=LoadTraceParentGenerator(env_vars["TRACEPARENT"]))
    provider.add_span_processor(span_processor=SpanProcessor(OTLPSpanExporter()))
    tracer = trace.get_tracer("GitHub Actions parser", "0.0.1", tracer_provider=provider)

    with tracer.start_as_current_span("workflow root", start_time=first_timestamp, end_on_exit=False) as root_span:
        for job in jobs:
            job_name = job["name"]
            job_id = job["id"]
            logging.info(f"Processing job '{job_name}'")
            job_create = date_str_to_epoch(job["created_at"], first_timestamp)
            job_start = date_str_to_epoch(job["started_at"], first_timestamp)
            # this may get later in time as we progress through the steps. It is
            # common to have the job completion time be earlier than the end of
            # the final cleanup steps
            job_last_timestamp = date_str_to_epoch(job["completed_at"], job_start)

            if job_start == 0:
                logging.info(f"Job is empty (no start time) - bypassing")
                continue

            attribute_file = Path.cwd() / f"telemetry-tools-attrs-{job_id}/attrs-{job_id}"
            attributes = {}
            if attribute_file.exists():
                logging.debug(f"Found attribute file for job '{job_id}'")
                attributes = parse_attribute_file(attribute_file.as_posix())
            else:
                logging.debug(f"No attribute metadata found for job '{job_id}'")

            attributes["service.name"] = job_name

            job_id_generator = RapidsSpanIdGenerator(trace_id=root_span.get_span_context().trace_id, job_name=job_name)

            job_provider = TracerProvider(resource=Resource(attributes=attributes), id_generator=job_id_generator)
            job_provider.add_span_processor(span_processor=SpanProcessor(OTLPSpanExporter()))
            job_tracer = trace.get_tracer("GitHub Actions parser", "0.0.1", tracer_provider=job_provider)

            with job_tracer.start_as_current_span(job['name'], start_time=job_create, end_on_exit=False) as job_span:
                job_span.set_status(map_conclusion_to_status_code(job["conclusion"]))

                job_id_generator.update_step_name('start delay time')
                with job_tracer.start_as_current_span(
                        name="start delay time",
                        start_time=job_create,
                        end_on_exit=False,
                        ) as delay_span:
                    delay_span.end(job_start)

                for step in job["steps"]:
                    start = date_str_to_epoch(step["started_at"], job_last_timestamp)
                    end = date_str_to_epoch(step["completed_at"], start)
                    job_id_generator.update_step_name(step['name'])

                    if (end - start) / 1e9 > 1:
                        logging.info(f"processing step: '{step['name']}'")
                        with job_tracer.start_as_current_span(
                                name=step['name'],
                                start_time=start,
                                end_on_exit=False,
                            ) as step_span:
                            step_span.set_status(map_conclusion_to_status_code(step["conclusion"]))
                            step_span.end(end)

                        job_last_timestamp = max(end, job_last_timestamp)

                job_end = max(date_str_to_epoch(job["completed_at"], job_last_timestamp), job_last_timestamp)
                last_timestamp = max(job_end, last_timestamp)
                job_span.end(job_end)
        root_span.end(last_timestamp)


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])
