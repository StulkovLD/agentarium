"""Наблюдаемость платформы — сквозной слой: структурные логи и трейсы. Контракт: spec/50.

Свойство платформы, не агента: живёт в SDK, достаётся любому агенту бесплатно (spec/50).

Две нити:
- **логи** — structlog JSON в stdout с обязательными ключами `ts, level, agent, trace_id, type,
  event`. `ts/level/event` ставят процессоры; `agent` биндит логгер экземпляра, `trace_id/type` —
  контекст обработки конверта (см. Agent).
- **трейсы** — OpenTelemetry, экспорт по OTLP gRPC в Jaeger (env `OTEL_EXPORTER_OTLP_ENDPOINT`/
  `OTEL_EXPORTER_OTLP_PROTOCOL`). Endpoint не задан → тихий no-op: спаны не пишутся, старт агента
  без Jaeger не падает. `traceparent` (W3C) едет в AMQP-заголовках — склейка спанов в один водопад.
"""

import os
import sys
from typing import TextIO

import structlog
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Tracer

# Имя инструментирующего модуля — то, чем помечены все спаны платформы в Jaeger.
INSTRUMENTATION = "agentarium"

# Провайдер трейсинга платформы. None → спаны no-op (никто не настроил / нет Jaeger).
# Владелец — этот модуль, а не глобальный OTel: set-once глобали ломает изоляцию тестов.
_provider: TracerProvider | None = None


# --- логи -----------------------------------------------------------------------------------


def configure_logging(stream: TextIO | None = None) -> None:
    """structlog → JSON в поток (по умолчанию stdout). Обязательные ключи — spec/50.

    `agent` биндится на логгер экземпляра (get_logger), `trace_id/type` — через contextvars
    в петле обработки, поэтому каждая строка лога обработки несёт все шесть ключей.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,  # trace_id, type из контекста обработки
            structlog.processors.add_log_level,  # -> level
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),  # -> ts
            structlog.processors.JSONRenderer(),  # -> event + весь словарь одной JSON-строкой
        ],
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stdout),
        cache_logger_on_first_use=False,  # тесты переконфигурируют поток — кэш нельзя
    )


def get_logger(agent: str) -> structlog.stdlib.BoundLogger:
    """Логгер экземпляра: ключ `agent` уже привязан ко всем его строкам."""
    return structlog.get_logger().bind(agent=agent)


# --- трейсы ---------------------------------------------------------------------------------


def otlp_span_exporter() -> SpanExporter | None:
    """OTLP gRPC экспортёр из env, или None если endpoint не задан → тихий no-op (spec/50).

    Конструкция ленивая: соединение с Jaeger откладывается до первого экспорта, а его отказ
    BatchSpanProcessor логирует в фоне, не роняя агент. grpc-импорт тоже ленив — no-op не тянет его.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return None
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=endpoint, insecure=endpoint.startswith("http://"))


def build_tracer_provider(service_name: str) -> TracerProvider:
    """Провайдер с ресурсом service.name; экспортёр цепляется, только если задан OTLP endpoint."""
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    exporter = otlp_span_exporter()
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def use_tracer_provider(provider: TracerProvider) -> None:
    """Установить провайдер платформы. Используют configure() и тесты (InMemory-экспортёр)."""
    global _provider
    _provider = provider


def reset_tracing() -> None:
    """Снять провайдер (→ no-op). Для изоляции тестов между собой."""
    global _provider
    _provider = None


def get_tracer() -> Tracer:
    """Трейсер платформы. Провайдер не настроен → no-op-трейсер (спаны не пишутся, но не падают)."""
    if _provider is not None:
        return _provider.get_tracer(INSTRUMENTATION)
    return trace.get_tracer(INSTRUMENTATION)


def configure(service_name: str) -> None:
    """Бутстрап наблюдаемости SDK: логи всегда, трейсы — если задан OTLP endpoint. Идемпотентно.

    Провайдер уже установлен (тест или прошлый вызов) — не перетираем.
    """
    configure_logging()
    if _provider is None and os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        use_tracer_provider(build_tracer_provider(service_name))


# --- W3C-контекст в транспорте (spec/20: в теле конверта его нет, только в AMQP-заголовках) ---


def inject_headers(headers: dict) -> None:
    """traceparent/tracestate текущего спана → AMQP-заголовки при публикации (spec/50)."""
    inject(headers)


def extract_context(headers: dict | None) -> Context:
    """Достать родительский W3C-контекст из AMQP-заголовков потреблённого конверта (spec/50)."""
    carrier = {
        k: (v.decode() if isinstance(v, bytes) else v)
        for k, v in (headers or {}).items()
        if isinstance(v, str | bytes)
    }
    return extract(carrier)
