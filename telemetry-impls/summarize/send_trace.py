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
import re
import time
from typing import Optional, Mapping, Union, Iterable, Dict

from opentelemetry import trace
from opentelemetry.sdk.trace import Span
from opentelemetry.trace import SpanKind, NonRecordingSpan, SpanContext, TraceFlags
from opentelemetry.context import Context, attach, detach
from opentelemetry.propagate import extract
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, BatchSpanProcessor
from opentelemetry.trace.status import Status, StatusCode

match os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL"):
    case "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    case "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    case _:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter as OTLPSpanExporter


import logging
logging.basicConfig(level=logging.DEBUG)

SpanProcessor = SimpleSpanProcessor # BatchSpanProcessor


def parse_attribute_file(filename: str) -> Dict[str, str]:
    attributes = {}
    with open(filename, "r") as attribute_file:
        for line in attribute_file.readlines():
            key, value = line.strip().split('=', 1)
            attributes[key] = value
    return attributes


def date_str_to_epoch(date_str: str, value_if_not_set: Optional[int] = 0) -> int:
    if date_str:
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


def main(args):
    # logging.basicConfig(level=logging.DEBUG)
    with open("all_jobs.json") as f:
        jobs = json.loads(f.read())

    env_vars = {}
    with open('telemetry-tools-env-vars/telemetry-env-vars') as f:
        for line in f.readlines():
            k, v = line.split("=", 1)
            env_vars[k] = v.strip().strip('"')

    first_timestamp = date_str_to_epoch(jobs[0]["created_at"])
    # track the latest timestamp observed and use it for any unavailable times.
    last_timestamp = date_str_to_epoch(jobs[0]["completed_at"])

    attribute_file = list(Path.cwd().glob(f"telemetry-tools-attrs-*/*"))[0]
    attributes = parse_attribute_file(attribute_file.as_posix())
    global_attrs = {}
    for k, v in attributes.items():
        if k.startswith('git.'):
            global_attrs[k] = v

    global_attrs['service.name'] = env_vars['OTEL_SERVICE_NAME']

    provider = TracerProvider(resource=Resource(global_attrs))
    provider.add_span_processor(span_processor=SpanProcessor(OTLPSpanExporter()))
    tracer = trace.get_tracer("GitHub Actions parser", "0.0.1", tracer_provider=provider)
    root_span = tracer.start_span("workflow root", start_time=first_timestamp)
    root_context = trace.set_span_in_context(root_span)

    for job in jobs:
        job_name = job["name"]
        job_id = job["id"]

        attribute_file = Path.cwd() / f"telemetry-tools-attrs-{job_id}/attrs-{job_id}"
        attributes = {}
        if attribute_file.exists():
            logging.debug(f"Found attribute file for job '{job_id}'")
            attributes = parse_attribute_file(attribute_file.as_posix())
        else:
            logging.warning(f"No attribute metadata found for job '{job_id}'")

        attributes["service.name"] = job_name

        job_provider = TracerProvider(resource=Resource(attributes=attributes))
        job_provider.add_span_processor(span_processor=SpanProcessor(OTLPSpanExporter()))
        job_tracer = trace.get_tracer("GitHub Actions parser", "0.0.1", tracer_provider=job_provider)

        job_span = job_tracer.start_span(
            name=job_name,
            start_time=date_str_to_epoch(job["created_at"], first_timestamp),
            context=root_context,
        )
        job_context = trace.set_span_in_context(job_span)

        job_span.set_status(map_conclusion_to_status_code(job["conclusion"]))

        for step in job["steps"]:
            start = date_str_to_epoch(step["started_at"], last_timestamp)
            end = date_str_to_epoch(step["completed_at"], last_timestamp)

            if end - start > 0:
                step_span = job_tracer.start_span(
                    name=step['name'],
                    start_time=start,
                    context=job_context,
                )
                step_span.set_status(map_conclusion_to_status_code(step["conclusion"]))
                step_span.end(end)

                last_timestamp = max(end, last_timestamp)

        job_create = date_str_to_epoch(job["created_at"], last_timestamp)
        job_start = date_str_to_epoch(job["started_at"], last_timestamp)
        job_end = max(date_str_to_epoch(job["completed_at"], last_timestamp), last_timestamp)

        delay_span = job_tracer.start_span(
            name="Start delay time",
            start_time=job_create,
            context=job_context,
        )
        delay_span.end(job_start)

        job_span.end(job_end)
    root_span.end(last_timestamp)


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])
