const opentelemetry = require('@opentelemetry/api');

const { Resource } = require('@opentelemetry/resources');
const { SEMRESATTRS_SERVICE_NAME } = require('@opentelemetry/semantic-conventions');
const { BasicTracerProvider, ConsoleSpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');

module.exports = async ({github, context, values, HOST, EXPORTERS})  => {

    console.log(github)
    console.log(context)
    const exporters = EXPORTERS.split(",")

    const tp = new BasicTracerProvider({
        resource: new Resource({
            [SEMRESATTRS_SERVICE_NAME]: 'basic-service',
        }),
    });

    if (exporters.includes("otlp")) {
        // Configure span processor to send spans to the exporter
        const trace_exporter = new OTLPTraceExporter({
            endpoint: HOST,
        });
        tp.addSpanProcessor(new SimpleSpanProcessor(trace_exporter));
    }
    if (exporters.includes("console")) {
        tp.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
    }

    tp.register();

    const tracer = opentelemetry.trace.getTracer(values["job_name"]);
    const job_info = values["job_info_json"]

    // Outer span for the whole job
    // Create a span. A span must be closed.
    const parentSpan = tracer.startSpan(values["span_id"], {startTime: job_info["started_at"]});
    parentSpan.setAttribute("startDelay", job_info["started_at"]-job_info["created_at"])
    //v For each step, create a span with create/start/stop times
    for (const step in job_info.steps) {
        const step_span = tracer.startSpan(step.name, {startTime: step.start_time})
        step_span.setAttribute("startDelay", step["started_at"]-step["created_at"])
        step_span.end(step["completed_at"])
    }
    // Close outer span
    parentSpan.end(job_info["completed_at"]);

    // flush and close the connection.
    if (exporters.includes("otlp")) {
        trace_exporter.shutdown();
    }
}