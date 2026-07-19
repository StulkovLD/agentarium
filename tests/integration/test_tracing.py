"""Интеграция наблюдаемости: путь конверта через двух агентов — один связанный трейс (spec/50).

Живой RabbitMQ, но экспорт спанов — InMemorySpanExporter вместо OTLP: детерминизм без Jaeger.
Доказываем водопад: gateway → agent.handle(stage1) → agent.handle(stage2), склеенный traceparent-ом
из AMQP-заголовков (в теле конверта контекста нет — spec/20), и что бизнес-trace_id дожил до конца.
"""

import asyncio
import sys
import uuid
from pathlib import Path

import aio_pika
import pytest
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agents" / "echo"))
from agent import EchoAgent, EchoPayload  # noqa: E402 — тип echo из каталога агентов
from agentarium import Agent, Bus, Envelope, Reply, observability  # noqa: E402
from agentarium.bus import DLX, EXCHANGE, QUEUE_ARGUMENTS  # noqa: E402
from agentarium.contracts import register  # noqa: E402

# Вторая ступень цепочки: echo.done → echo.final. Схему приносит сам тип (реестр собирается).
register("echo.final", EchoPayload)


class Stage2(Agent):
    consumes = ["echo.done"]
    produces = ["echo.final"]

    async def handle(self, envelope: Envelope) -> Reply | None:
        return Reply(type="echo.final", payload=envelope.payload)


@pytest.fixture
def traces():
    """Провайдер платформы с синхронным InMemory-экспортёром: спан доступен сразу на end."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: "agentarium-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    observability.use_tracer_provider(provider)
    yield exporter
    observability.reset_tracing()
    provider.shutdown()


@pytest.fixture
async def bus(rabbitmq_url):
    b = Bus(rabbitmq_url)
    await b.connect()
    yield b
    await b.close()


@pytest.fixture
async def wired(bus):
    """Ручная топология: exchange + dlx + очереди echo/done/final с биндингами (S2-стиль)."""
    ch = bus.channel
    ex = await ch.declare_exchange(EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
    dlx = await ch.declare_exchange(DLX, aio_pika.ExchangeType.FANOUT, durable=True)
    suffix = uuid.uuid4().hex[:8]
    names = {k: f"tr-{k}-{suffix}" for k in ("echo", "done", "final", "dlq")}
    queues = {}
    for key in ("echo", "done", "final"):
        q = await ch.declare_queue(names[key], durable=True, arguments=dict(QUEUE_ARGUMENTS))
        queues[key] = q
    queues["dlq"] = await ch.declare_queue(
        names["dlq"], durable=True, arguments={"x-queue-type": "quorum"}
    )
    await queues["echo"].bind(ex, "echo.request")
    await queues["done"].bind(ex, "echo.done")
    await queues["final"].bind(ex, "echo.final")
    await queues["dlq"].bind(dlx, "#")
    yield queues
    for q in queues.values():
        await q.delete(if_unused=False, if_empty=False)


async def get_json(queue, timeout=10.0):
    async def _poll():
        while True:
            msg = await queue.get(fail=False)
            if msg is not None:
                return msg
            await asyncio.sleep(0.1)

    return await asyncio.wait_for(_poll(), timeout)


async def wait_for_handles(exporter, n, timeout=10.0):
    async def _poll():
        while True:
            handles = [s for s in exporter.get_finished_spans() if s.name == "agent.handle"]
            if len(handles) >= n:
                return handles
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(_poll(), timeout)


async def test_two_agent_chain_is_one_connected_trace(bus, wired, traces):
    stage1 = EchoAgent(instance="echo-1", bus=bus, queue=wired["echo"].name)
    stage2 = Stage2(instance="echo-2", bus=bus, queue=wired["done"].name)
    t1 = asyncio.create_task(stage1.run())
    t2 = asyncio.create_task(stage2.run())

    env = Envelope(trace_id=uuid.uuid4(), producer="gateway", type="echo.request",
                   payload={"text": "живой водопад"})

    # Шлюз-подобный root-спан: он инжектит traceparent в entry-конверт (spec/50)
    with observability.get_tracer().start_as_current_span("gateway.request"):
        await bus.publish(env)

    final = await get_json(wired["final"])
    out = Envelope.model_validate_json(final.body)
    await final.ack()
    assert out.type == "echo.final"
    assert out.trace_id == env.trace_id  # бизнес-нить дожила от входа до конца

    handles = await wait_for_handles(traces, n=2)
    spans_by_type = {s.attributes["type"]: s for s in handles}
    s1 = spans_by_type["echo.request"]  # stage1 обработал вход
    s2 = spans_by_type["echo.done"]  # stage2 обработал выход stage1

    # Обязательные атрибуты спана обработки (spec/50)
    for span in (s1, s2):
        assert set(span.attributes) >= {"producer", "type", "envelope.id", "trace_id", "attempt"}
        assert span.attributes["trace_id"] == str(env.trace_id)  # trace_id = конвертному
        assert span.attributes["attempt"] == 1

    # Один трейс: оба спана несут один и тот же OTel trace_id (склейка traceparent-ом)
    assert s1.context.trace_id == s2.context.trace_id

    # Водопад родитель→потомок: stage2 — дитя stage1, stage1 — дитя gateway-root
    gateway = next(s for s in traces.get_finished_spans() if s.name == "gateway.request")
    assert s1.parent is not None and s1.parent.span_id == gateway.context.span_id
    assert s2.parent is not None and s2.parent.span_id == s1.context.span_id
    assert gateway.context.trace_id == s1.context.trace_id

    stage1.stop()
    stage2.stop()
    await t1
    await t2
