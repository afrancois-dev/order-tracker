import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "order-tracker")
ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

_configured = False


def _resource() -> Resource:
    return Resource.create({"service.name": SERVICE_NAME})


def _use_otlp() -> bool:
    return bool(ENDPOINT)


def setup_telemetry() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    resource = _resource()
    otlp = _use_otlp()

    trace_provider = TracerProvider(resource=resource)
    trace_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=ENDPOINT) if otlp else ConsoleSpanExporter()
        )
    )
    trace.set_tracer_provider(trace_provider)

    metric_exporter = OTLPMetricExporter(endpoint=ENDPOINT) if otlp else ConsoleMetricExporter()
    metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=2000)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    logs_exporter = OTLPLogExporter(endpoint=ENDPOINT) if otlp else ConsoleLogExporter()
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(logs_exporter))
    set_logger_provider(logger_provider)

    handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
    if not otlp:
        logging.getLogger(__name__).info(
            "OpenTelemetry exporting to console (set OTEL_EXPORTER_OTLP_ENDPOINT for OTLP)"
        )


_request_counter = None


def request_counter():
    global _request_counter
    if _request_counter is None:
        _request_counter = metrics.get_meter("order-tracker.requests").create_counter(
            "http.server.requests",
            unit="1",
            description="Number of HTTP requests handled by the API",
        )
    return _request_counter
