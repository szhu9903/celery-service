# ==============================================================================
# src/celery_app/utils/tracing.py
# OpenTelemetry distributed tracing — fully optional.
#
# All OTel imports are lazy (inside functions) so the service starts normally
# even when the otel packages are not installed.
#
# Enable by setting:
#   OBS_OTEL_ENABLED=true
#   OBS_OTEL_EXPORTER_OTLP_ENDPOINT=http://your-collector:4317
#
# Required extras (only when OBS_OTEL_ENABLED=true):
#   pip install ".[otel]"
#   — opentelemetry-sdk
#   — opentelemetry-instrumentation-celery
#   — opentelemetry-exporter-otlp-proto-grpc  (production)
# ==============================================================================
from __future__ import annotations

import logging

import structlog

from config.settings import get_settings

logger = structlog.get_logger(__name__)


def setup_tracing() -> None:
    """
    Initialise OpenTelemetry tracing.
    No-op (and no ImportError) when OBS_OTEL_ENABLED=false or packages missing.
    """
    s = get_settings()

    if not s.observability.otel_enabled:
        logger.debug("tracing.disabled")
        return

    # ── Lazy imports — only executed when tracing is actually enabled ──────
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except ImportError as exc:
        logger.warning(
            "tracing.sdk_missing",
            hint="pip install opentelemetry-sdk opentelemetry-instrumentation-celery",
            error=str(exc),
        )
        return

    resource = Resource.create({
        "service.name": s.observability.otel_service_name,
        "service.version": s.service_version,
        "deployment.environment": s.environment,
    })
    provider = TracerProvider(resource=resource)

    if s.environment in ("development", "test"):
        # Print spans to stdout — no collector needed locally
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        logger.info("tracing.console_exporter")
    else:
        # Production: send spans to an OTLP collector (Jaeger / Tempo / Datadog)
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        except ImportError as exc:
            logger.warning(
                "tracing.otlp_exporter_missing",
                hint="pip install opentelemetry-exporter-otlp-proto-grpc",
                error=str(exc),
            )
            return

        exporter = OTLPSpanExporter(
            endpoint=s.observability.otel_exporter_otlp_endpoint,
            insecure=True,   # set False and configure credentials for TLS
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)

    # Auto-instrument Celery: wraps task.__call__ with a span automatically
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor
        CeleryInstrumentor().instrument()
    except ImportError as exc:
        logger.warning(
            "tracing.celery_instrumentor_missing",
            hint="pip install opentelemetry-instrumentation-celery",
            error=str(exc),
        )
        return

    logger.info(
        "tracing.enabled",
        endpoint=s.observability.otel_exporter_otlp_endpoint,
        service=s.observability.otel_service_name,
    )


def get_tracer(name: str = __name__) -> "trace.Tracer":  # type: ignore[name-defined]
    """
    Return a named tracer for manual span creation.
    Falls back to a no-op tracer when OTel SDK is not installed.
    """
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except ImportError:
        # Return a no-op object so call sites don't need try/except
        return _NoopTracer()  # type: ignore[return-value]


class _NoopTracer:
    """Minimal no-op tracer used when the OTel SDK is absent."""

    def start_as_current_span(self, name: str, **kwargs):  # noqa: ANN001, ANN201
        from contextlib import contextmanager

        @contextmanager
        def _noop():
            yield None

        return _noop()

    def start_span(self, name: str, **kwargs):  # noqa: ANN001, ANN201
        return _NoopSpan()


class _NoopSpan:
    def set_attribute(self, key: str, value: object) -> None: ...
    def add_event(self, name: str, **kwargs) -> None: ...  # noqa: ANN201
    def __enter__(self): return self  # noqa: ANN201
    def __exit__(self, *args): ...  # noqa: ANN201