"""Юнит-тесты наблюдаемости — контракты spec/50, без живых сервисов.

Проверяем два свойства платформы:
- лог обработки — структурный JSON с обязательными ключами;
- нет OTLP endpoint → тихий no-op, спан создаётся без падения (старт без Jaeger не рушится).
"""

import io
import json

import structlog
from agentarium import observability


def test_log_line_is_json_with_mandatory_keys():
    """Каждая строка лога обработки несёт ts, level, agent, trace_id, type, event (spec/50)."""
    stream = io.StringIO()
    observability.configure_logging(stream=stream)
    log = observability.get_logger("parser")  # ключ agent привязан на логгер экземпляра

    # trace_id и type биндит петля обработки через contextvars (в SDK — в _process)
    with structlog.contextvars.bound_contextvars(trace_id="trace-123", type="request.parsed"):
        log.info("handled")

    record = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert set(record) >= {"ts", "level", "agent", "trace_id", "type", "event"}
    assert record["agent"] == "parser"
    assert record["trace_id"] == "trace-123"
    assert record["type"] == "request.parsed"
    assert record["event"] == "handled"
    assert record["level"] == "info"


def test_no_otlp_endpoint_is_graceful_noop(monkeypatch):
    """Endpoint не задан → нет экспортёра; спан всё равно создаётся и не роняет процесс."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert observability.otlp_span_exporter() is None

    provider = observability.build_tracer_provider("agentarium")
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("agent.handle") as span:
        span.set_attribute("trace_id", "trace-123")  # без Jaeger — не падает


def test_configure_without_endpoint_installs_no_provider(monkeypatch):
    """configure() без endpoint не ставит провайдер — трейсер остаётся no-op (spec/50)."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    observability.reset_tracing()
    observability.configure("agentarium")
    try:
        # get_tracer не падает и спан открывается даже когда провайдера нет
        with observability.get_tracer().start_as_current_span("agent.handle"):
            pass
    finally:
        observability.reset_tracing()


def test_otlp_endpoint_present_builds_exporter(monkeypatch):
    """Endpoint задан → экспортёр строится (лениво, без соединения на конструкции)."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    exporter = observability.otlp_span_exporter()
    assert exporter is not None
    exporter.shutdown()  # закрыть grpc-канал, не течь в соседние тесты
