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
from datetime import datetime
import hashlib
from pathlib import Path
import json
import time
import os
import re

from typing import Optional, Mapping, Union, Iterable
from opentelemetry import trace
from opentelemetry.sdk.trace import Span
from opentelemetry.trace import SpanKind
from opentelemetry.context import Context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.status import Status, StatusCode

match os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL"):
    case "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    case "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    case _:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter as OTLPSpanExporter


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


def create_span(
    span_name: str,
    service_name: str = "otel-cli-python",
    service_version: str = __version__,
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
    kind: Literal["client", "consumer", "internal", "producer", "server"] = "internal",
    traceparent: Optional[str] = None,
    attributes: Optional[Mapping[str, str]] = None,
    status_code: Literal["UNSET", "OK", "ERROR"] = "UNSET",
    status_message: Optional[str] = None,
) -> Span:
    resource = Resource.create(
        attributes={
            "service.name": service_name,
            "service.version": service_version,
        }
    )
    provider = TracerProvider(resource=resource)
    otlp_exporter = OTLPSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    tracer = trace.get_tracer("otel-cli-python", __version__, tracer_provider=provider)

    if trace_id is not None:
        tracer.id_generator.generate_trace_id = lambda: int(trace_id, 16)

    if span_id is not None:
        tracer.id_generator.generate_span_id = lambda: int(span_id, 16)

    if start_time is None:
        start_time = time.time_ns()

    if end_time is None:
        end_time = time.time_ns()

    # Create a new context to avoid reusing context created by pytest
    context = Context()
    if not traceparent:
        traceparent = os.getenv("TRACEPARENT")
    if traceparent is not None:
        carrier = {"traceparent": traceparent}
        context = TraceContextTextMapPropagator().extract(carrier)

    span_kind = SpanKind[kind.upper()]
    statuscode = StatusCode[status_code]
    span_status = Status(statuscode, description=status_message)
    my_span = tracer.start_span(
        span_name,
        start_time=start_time,
        kind=span_kind,
        context=context,
        attributes=attributes,
    )
    my_span._status = span_status
    my_span.end(end_time=end_time)
    return my_span


def trace_id(run_url: str, run_attempt: str):
    trace_id = hashlib.sha256()
    trace_id.update(f"{run_url}+{run_attempt}".encode())
    return trace_id.hexdigest()[:32]


def span_id(trace_id: str, job_name: str, step_name: Optional[str] = None):
    span_id = hashlib.sha256()
    span_id.update(trace_id.encode())
    span_id.update(bytes(job_name.encode()))
    if step_name:
        span_id.update(bytes(step_name.encode()))
    return span_id.hexdigest()[:16]


def traceparent(run_url, run_attempt, job_name, step_name: Optional[str] = None):
    tid = trace_id(run_url=run_url, run_attempt=run_attempt)
    sid = span_id(trace_id=tid, job_name=job_name, step_name=step_name)
    return f"00-{tid}-{sid}-01"


def date_str_to_epoch(date_str: str, value_if_not_set: int):
    if date_str:
        timestamp_ns = int(datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").timestamp() * 1e9)
    else:
        timestamp_ns = value_if_not_set
    return timestamp_ns


def map_conclusion_to_status_code(conclusion: str):
    if conclusion == "success":
        return "OK"
    elif conclusion == "failure":
        return "ERROR"
    else:
        return "UNSET"


def main(args):
    with open("all_jobs.json") as f:
        jobs = json.loads(f.read())
    tid = trace_id(jobs[0]["run_url"], jobs[0]["run_attempt"])
    try:
        top_level_job_name = os.environ["OTEL_SERVICE_NAME"]
    except KeyError:
        print("The `OTEL_SERVICE_NAME` environment variable must be set. Exiting")
        sys.exit(1)
    top_level_span_id = span_id(tid, top_level_job_name)
    top_level_traceparent = f"00-{tid}-{top_level_span_id}-01"
    # track the latest timestamp observed and use it for any unavailable times.
    last_timestamp = date_str_to_epoch(jobs[0]["completed_at"], 0)
    for job in jobs:
        job_name = job["name"]
        job_id = job["id"]
        job_span_id = span_id(tid, job_name=job["name"])
        job_traceparent = f"00-{tid}-{job_span_id}-01"

        attribute_file = Path.cwd() / f"telemetry-tools-attrs-${job_id}/attrs-${job_id}"
        if attribute_file.exists():
            attributes = parse_attribute_file(attribute_file.as_posix())
        elif attributes_env_var := os.getenv("OTEL_RESOURCE_ATTRIBUTES"):
            attributes = parse_attributes(attributes_env_var.split(","))
        else:
            attributes = None

        for step in job["steps"]:
            step_span_id = span_id(trace_id=tid, job_name=job_name, step_name=step["name"])
            start = date_str_to_epoch(step["started_at"], last_timestamp)
            end = date_str_to_epoch(step["completed_at"], last_timestamp)

            if end > last_timestamp:
                last_timestamp = end
            create_span(
                span_name=step["name"],
                service_name=job_name,
                trace_id=tid,
                span_id=step_span_id,
                traceparent=job_traceparent,
                start_time=start,
                end_time=end,
                attributes=attributes,
                status_code=map_conclusion_to_status_code(step["conclusion"]),
            )
        job_create = date_str_to_epoch(job["created_at"], last_timestamp)
        job_start = date_str_to_epoch(job["started_at"], last_timestamp)
        job_end = max(date_str_to_epoch(job["completed_at"], last_timestamp), last_timestamp)

        if job_name == top_level_job_name:
            create_span(
                span_name="workflow root",
                service_name=job_name,
                trace_id=tid,
                span_id=job_span_id,
                start_time=job_create,
                end_time=job_end,
                attributes=attributes,
                status_code=map_conclusion_to_status_code(job["conclusion"]),
            )
        else:
            create_span(
                span_name="Start delay time",
                service_name=job_name,
                trace_id=tid,
                traceparent=job_traceparent,
                start_time=job_create,
                end_time=job_start,
                attributes=attributes,
            )
            create_span(
                span_name="child workflow root",
                service_name=job_name,
                trace_id=tid,
                span_id=job_span_id,
                traceparent=top_level_traceparent,
                start_time=job_create,
                end_time=job_start,
                attributes=attributes,
                status_code=map_conclusion_to_status_code(job["conclusion"]),
            )


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])
