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
"""Processes a GitHub Actions workflow log record and outputs OpenTelemetry span data.

This script is a slight adaptation of the OpenTelemetry CLI project at
https://github.com/dell/opentelemetry-cli. That project's license is:

Copyright 2022 Dell Technologies

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""


from __future__ import annotations
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Optional, Mapping, Union, Iterable

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

try:
    from typing import Literal
except ImportError:  # pragma: no cover
    from typing_extensions import Literal

__version__ = "0.0.1"


def strtobool(val: str) -> bool:
    """
    Convert a string representation of truth to true (1) or false (0).

    True values are y, yes, t, true, on and 1; false values are n, no, f,
    false, off and 0. Raises ValueError if val is anything else.
    """
    val = val.lower()
    if val in ("y", "yes", "t", "true", "on", "1"):
        return True
    elif val in ("n", "no", "f", "false", "off", "0"):
        return False
    else:
        raise ValueError(f"Unable to parse '{val!r}' as boolean")


_attribute_casting = {
    "int": int,
    "float": float,
    "bool": strtobool,
    "str": str,
}


def parse_attributes(attrs: Iterable[str]) -> Mapping[str, Union[str, int, list, bool]]:
    """
    Attempt to parse attributes in a given list `attrs`.
    Special handling is done for attributes which begin with these prefixes:
        'str:' -> Value will be converted to string using str() (default)
        'int:' -> Value will be converted to integer using int()
        'float:' -> Value will be converted to float using float()
        'bool:' -> Value will be converted to bool.
                   True values are 'y', 'yes', 't', 'true', 'on', and '1'
                   False values are 'n', 'no', 'f', 'false', 'off', and '0'.

    In order to pass multiple values, add "[]" to the prefix like so:
        'int[]:my-array=1,2,3,4'

    You can customize the separator like so:
        'int[sep=;]:my-array=1;2;3;4'
        'str[sep=:]:path-array=/usr/bin:/bin'
    """
    attr_pattern = re.compile(r"^(?:(?P<prefix>.*):)?(?P<name>[^=]*)=(?P<value>.*)$")
    prefix_pattern = re.compile(r"^(?P<type>[^\[\n]*)(?P<array>\[(sep=?(?P<sep>.))?.*\])?$")
    attributes = {}
    for attr in attrs:
        attr = attr.strip()
        if not attr:
            continue
        attr_match = attr_pattern.match(attr)
        if attr_match is None:
            raise ValueError(f"Unable to parse attribute: {attr}")

        prefix, key, value = attr_match.group("prefix", "name", "value")

        if prefix is None:
            attributes[key] = value
            continue

        prefix_match = prefix_pattern.match(prefix)
        if prefix_match:
            prefix_type = prefix_match.group("type")
            cast_function = _attribute_casting.get(prefix_type, lambda x: x)

            if prefix_match.group("array") is None:
                attributes[key] = cast_function(value)
                continue

            value_separator = prefix_match.group("sep") or ","
            attributes[key] = tuple([cast_function(item) for item in value.split(value_separator)])

    return attributes


def parse_attribute_file(filename: str) -> Mapping[str, Union[str, int, list, bool]]:
    with open(filename, "r") as attribute_file:
        attributes = parse_attributes(attribute_file.readlines())
    return attributes


def span_id(trace_id: str, job_name: str, step_name: Optional[str] = None):
    span_id = hashlib.sha256()
    span_id.update(trace_id.encode())
    span_id.update(bytes(job_name.encode()))
    if step_name:
        span_id.update(bytes(step_name.encode()))
    return span_id.hexdigest()[:16]


def date_str_to_epoch(date_str: str, value_if_not_set: Optional[int] = 0) -> int:
    if date_str:
        timestamp_ns = int(datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").timestamp() * 1e9)
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

    # track the latest timestamp observed and use it for any unavailable times.
    last_timestamp = date_str_to_epoch(jobs[0]["completed_at"])

    # Create a new context to avoid reusing context created by pytest
    carrier = {"traceparent": os.environ["TRACEPARENT"]}
    outer_context = extract(carrier)
    attach(outer_context)

    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    provider.add_span_processor(span_processor=SimpleSpanProcessor(OTLPSpanExporter()))
    tracer = trace.get_tracer("GitHub Actions parser", __version__, tracer_provider=provider)
    root_span = tracer.start_span("workflow root", start_time=date_str_to_epoch(jobs[0]['created_at']))
    root_context = trace.set_span_in_context(root_span)

    for job in jobs:
        job_name = job["name"]
        job_id = job["id"]
        print(job_name)

        attribute_file = Path.cwd() / f"telemetry-tools-attrs-{job_id}/attrs-{job_id}"
        if attribute_file.exists():
            logging.debug(f"Found attribute file for job '{job_id}'")
            attributes = parse_attribute_file(attribute_file.as_posix())
        elif attributes_env_var := os.getenv("OTEL_RESOURCE_ATTRIBUTES"):
            logging.debug("Found attributes environment variable")
            attributes = parse_attributes(attributes_env_var.split(","))
        else:
            logging.warning(f"No attribute metadata found for job '{job_id}'")
            attributes = {}

        job_span = tracer.start_span(
            name=job_name,
            start_time=date_str_to_epoch(job["started_at"], last_timestamp),
            context=root_context,
            attributes=attributes,
        )
        job_context = trace.set_span_in_context(job_span)

        job_span.set_status(map_conclusion_to_status_code(job["conclusion"]))

        for step in job["steps"]:
            start = date_str_to_epoch(step["started_at"], last_timestamp)
            end = date_str_to_epoch(step["completed_at"], last_timestamp)
            print(step['name'])

            step_span = tracer.start_span(
                name=step['name'],
                start_time=start,
                context=job_context,
                attributes=attributes,
            )
            step_span.set_status(map_conclusion_to_status_code(step["conclusion"]))
            step_span.end(end)

            if end > last_timestamp:
                last_timestamp = end

            job_create = date_str_to_epoch(job["created_at"], last_timestamp)
            job_start = date_str_to_epoch(job["started_at"], last_timestamp)
            job_end = max(date_str_to_epoch(job["completed_at"], last_timestamp), last_timestamp)

            # if job_name != top_level_job_name:
            #     delay_span = tracer.start_span(name="Start delay time", start_time=job_create)
            #     delay_span.end(job_start)

        job_span.end(job_end)
    root_span.end(last_timestamp)


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])
