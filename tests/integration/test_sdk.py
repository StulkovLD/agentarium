"""Интеграционные тесты SDK против живого RabbitMQ — семантика доставки из spec/30/50.

Биндинги здесь объявляются руками (стоп-правило слайса S2: без topology — spec/70).
"""

import asyncio
import sys
import uuid
from pathlib import Path

import aio_pika
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agents" / "echo"))
from agent import EchoAgent  # noqa: E402 — тип echo из каталога агентов
from agentarium import Bus, Envelope  # noqa: E402
from agentarium.bus import DLX, EXCHANGE, QUEUE_ARGUMENTS, UnroutableEnvelope  # noqa: E402


def make_envelope(type_name="echo.request", **payload_extra):
    return Envelope(
        trace_id=uuid.uuid4(),
        producer="test",
        type=type_name,
        payload={"text": "живой прогон", **payload_extra},
    )


@pytest.fixture
async def bus(rabbitmq_url):
    b = Bus(rabbitmq_url)
    await b.connect()
    yield b
    await b.close()


@pytest.fixture
async def wired(bus):
    """Ручная топология S2: exchange + dlx + очереди echo/done/failed с биндингами."""
    ch = bus.channel
    ex = await ch.declare_exchange(EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
    dlx = await ch.declare_exchange(DLX, aio_pika.ExchangeType.FANOUT, durable=True)
    suffix = uuid.uuid4().hex[:8]
    names = {k: f"t-{k}-{suffix}" for k in ("echo", "done", "failed", "dlq")}
    queues = {}
    for key in ("echo", "done", "failed"):
        q = await ch.declare_queue(names[key], durable=True, arguments=dict(QUEUE_ARGUMENTS))
        queues[key] = q
    # dlq — конец линии: без delivery-limit и без DLX (spec/40)
    queues["dlq"] = await ch.declare_queue(
        names["dlq"], durable=True, arguments={"x-queue-type": "quorum"}
    )
    await queues["echo"].bind(ex, "echo.request")
    await queues["done"].bind(ex, "echo.done")
    await queues["failed"].bind(ex, "task.failed")
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


def run_agent(bus, queues, agent_cls=EchoAgent, **kwargs):
    agent = agent_cls(
        instance="echo", bus=bus, queue=queues["echo"].name, retry_delays_s=(0, 0, 0), **kwargs
    )
    task = asyncio.create_task(agent.run())
    return agent, task


async def test_happy_path_delivers_and_acks(bus, wired):
    agent, task = run_agent(bus, wired)
    await bus.publish(make_envelope())
    msg = await get_json(wired["done"])
    out = Envelope.model_validate_json(msg.body)
    await msg.ack()
    assert out.type == "echo.done"
    assert out.producer == "echo"
    assert out.payload["text"] == "живой прогон"
    agent.stop()
    await task
    # вход подтверждён: очередь echo пуста
    assert (await wired["echo"].get(fail=False)) is None


async def test_unroutable_publish_raises(bus, wired):
    with pytest.raises(UnroutableEnvelope):
        await bus.publish(make_envelope(type_name="no.route"))


async def test_unacked_survives_consumer_death(bus, wired, rabbitmq_url):
    """Конверт, взятый и не подтверждённый, возвращается в очередь — spec/50, слой «Конверт»."""
    victim = Bus(rabbitmq_url)
    await victim.connect()
    q = await victim.channel.get_queue(wired["echo"].name, ensure=False)
    await bus.publish(make_envelope())
    got = await get_json(q)
    assert got is not None  # взят, unacked
    await victim.close()  # смерть консюмера без ack
    back = await get_json(wired["echo"])
    assert back.redelivered
    await back.ack()


async def test_retries_then_success(bus, wired):
    class Flaky(EchoAgent):
        calls = 0

        async def handle(self, envelope):
            type(self).calls += 1
            if type(self).calls < 3:
                raise RuntimeError("транзиентная ошибка")
            return await super().handle(envelope)

    agent, task = run_agent(bus, wired, agent_cls=Flaky)
    await bus.publish(make_envelope())
    msg = await get_json(wired["done"])
    await msg.ack()
    assert Flaky.calls == 3
    agent.stop()
    await task


async def test_exhausted_retries_publish_task_failed(bus, wired):
    class Broken(EchoAgent):
        async def handle(self, envelope):
            raise RuntimeError("вечная ошибка")

    agent, task = run_agent(bus, wired, agent_cls=Broken)
    await bus.publish(make_envelope())
    msg = await get_json(wired["failed"])
    failed = Envelope.model_validate_json(msg.body)
    await msg.ack()
    assert failed.type == "task.failed"
    assert failed.payload["attempts"] == 4  # 1 + три ретрая
    assert failed.payload["failed_type"] == "echo.request"
    assert failed.payload["original_payload"]["text"] == "живой прогон"
    agent.stop()
    await task
    assert (await wired["echo"].get(fail=False)) is None  # вход ack-нут после task.failed


async def test_contract_error_fails_fast_without_retry(bus, wired):
    class Counting(EchoAgent):
        calls = 0

        async def handle(self, envelope):
            type(self).calls += 1
            return await super().handle(envelope)

    agent, task = run_agent(bus, wired, agent_cls=Counting)
    bad = Envelope(
        trace_id=uuid.uuid4(), producer="test", type="echo.request", payload={"нет": "text"}
    )
    await bus.publish(bad)
    msg = await get_json(wired["failed"])
    failed = Envelope.model_validate_json(msg.body)
    await msg.ack()
    assert failed.payload["attempts"] == 1  # контрактная ошибка не ретраится — spec/50
    assert Counting.calls == 0  # до handle не дошло
    agent.stop()
    await task


async def test_delivery_limit_dead_letters_to_dlq(bus, wired):
    """Ядовитый конверт: 3 доставки исчерпаны → DLX → dlq — spec/50, слой «Доставка»."""
    await bus.publish(make_envelope(text="яд"))
    q = wired["echo"]
    for _ in range(4):  # initial + redeliveries до исчерпания x-delivery-limit=3
        msg = await get_json(q, timeout=5.0)
        await msg.nack(requeue=True)
    dead = await get_json(wired["dlq"])
    env = Envelope.model_validate_json(dead.body)
    await dead.ack()
    assert env.type == "echo.request"


async def test_watchdog_triggers_fatal(bus, wired):
    fired = asyncio.Event()

    class Hanging(EchoAgent):
        async def handle(self, envelope):
            await asyncio.sleep(30)

        def _fatal_exit(self):
            fired.set()
            self.stop()

    agent, task = run_agent(bus, wired, agent_cls=Hanging, watchdog_s=0.3)
    await bus.publish(make_envelope())
    await asyncio.wait_for(fired.wait(), timeout=10)
    await task
