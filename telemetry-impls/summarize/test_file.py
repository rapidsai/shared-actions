from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from typing import Dict

import logging
logging.basicConfig(level=logging.DEBUG)
from time import time_ns, sleep
from pathlib import Path

SpanProcessor = SimpleSpanProcessor

# Global tracer provider which can be set only once
trace.set_tracer_provider(
    TracerProvider(resource=Resource.create({"service.name": "service1"}))
)
trace.get_tracer_provider().add_span_processor(SpanProcessor(OTLPSpanExporter()))

tracer = trace.get_tracer("tracer.one")
with tracer.start_as_current_span("some-name") as span:
    span.set_attribute("key", "value")



another_tracer_provider = TracerProvider(
    resource=Resource.create({"service.name": "service2"})
)
another_tracer_provider.add_span_processor(SpanProcessor(OTLPSpanExporter()))

another_tracer = trace.get_tracer("tracer.two", tracer_provider=another_tracer_provider)
with another_tracer.start_as_current_span("name-here") as span:
    span.set_attribute("another-key", "another-value")

def parse_attribute_file(filename: str) -> Dict[str, str]:
    attributes = {}
    with open(filename, "r") as attribute_file:
        for line in attribute_file.readlines():
            key, value = line.strip().split('=', 1)
            attributes[key] = value
    return attributes

attribute_file = list(Path.cwd().glob(f"telemetry-tools-attrs-*/*"))[0]
attributes = parse_attribute_file(attribute_file.as_posix())
global_attrs = {}
for k, v in attributes.items():
    if k.startswith('git.') or k.startswith('service.'):
        global_attrs[k] = v

third_provider = TracerProvider(resource=Resource(global_attrs))
third_provider.add_span_processor(span_processor=SpanProcessor(OTLPSpanExporter()))
third_tracer = trace.get_tracer("GitHub Actions parser", "0.0.1", tracer_provider=third_provider)
root_span = third_tracer.start_span("workflow root", start_time=time_ns())
root_context = trace.set_span_in_context(root_span)

sleep(0.5)


step_span = third_tracer.start_span(
    name="steppy",
    start_time=time_ns(),
    context=root_context,
)


sleep(0.5)

step_span.end()

# job_span.set_status(map_conclusion_to_status_code(job["conclusion"]))
root_span.end(time_ns())
