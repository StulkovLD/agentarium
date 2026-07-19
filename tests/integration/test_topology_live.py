"""Интеграция слайса S3 на живом RabbitMQ: apply, реконсиляция, дренаж, fan-out, цепочка.

Топология объявляется чертежом (не руками, в отличие от S2). Состояние брокера сверяется
независимо — через Management API, а не через код под тестом.
"""

import asyncio
import uuid
from urllib.parse import quote, unquote, urlsplit

import aio_pika
import httpx
import pytest
import yaml
from agentarium import Bus, Envelope
from agentarium.__main__ import load_agent_class
from agentarium.apply import (
    GATEWAY_QUEUE,
    ApplyError,
    apply_topology,
    queue_name,
)
from agentarium.bus import DLQ, DLX, EXCHANGE
from agentarium.topology import load_catalog, load_topology

CATALOG = load_catalog("agents/catalog.yaml")
ECHO_PAIR = "configs/echo-pair.yaml"

# Все объекты, которые эти тесты могут создать — для чистки между прогонами.
_KNOWN_QUEUES = [
    "agentarium.echo",
    "agentarium.echo2",
    "agentarium.reverse",
    GATEWAY_QUEUE,
    DLQ,
    "agentarium.ghost",
]
_KNOWN_EXCHANGES = [EXCHANGE, DLX]


# --- независимая сверка через Management API ------------------------------------------------


def _mgmt(url: str):
    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    auth = (unquote(parts.username or "guest"), unquote(parts.password or "guest"))
    vhost = parts.path
    vhost = "/" if vhost in ("", "/") else unquote(vhost.lstrip("/"))
    return f"http://{host}:15672", auth, quote(vhost, safe="")


async def _bindings(url: str, exchange: str) -> set[tuple[str, str]]:
    base, auth, vhost = _mgmt(url)
    async with httpx.AsyncClient(base_url=base, auth=auth, timeout=10.0) as c:
        r = await c.get(f"/api/exchanges/{vhost}/{quote(exchange, safe='')}/bindings/source")
        r.raise_for_status()
        return {
            (b["destination"], b["routing_key"])
            for b in r.json()
            if b.get("destination_type") == "queue"
        }


async def _queue(url: str, name: str) -> dict | None:
    base, auth, vhost = _mgmt(url)
    async with httpx.AsyncClient(base_url=base, auth=auth, timeout=10.0) as c:
        r = await c.get(f"/api/queues/{vhost}/{quote(name, safe='')}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def _purge(url: str) -> None:
    base, auth, vhost = _mgmt(url)
    async with httpx.AsyncClient(base_url=base, auth=auth, timeout=10.0) as c:
        for q in _KNOWN_QUEUES:
            await c.delete(f"/api/queues/{vhost}/{quote(q, safe='')}")
        for ex in _KNOWN_EXCHANGES:
            await c.delete(f"/api/exchanges/{vhost}/{quote(ex, safe='')}")


async def _wait_depth(url: str, name: str, want: int, timeout: float = 10.0) -> int:
    async def poll():
        while True:
            info = await _queue(url, name)
            if info is not None and info.get("messages", 0) >= want:
                return info["messages"]
            await asyncio.sleep(0.2)

    return await asyncio.wait_for(poll(), timeout)


@pytest.fixture
async def clean(rabbitmq_url):
    await _purge(rabbitmq_url)
    yield rabbitmq_url
    await _purge(rabbitmq_url)


def _env(type_name: str, **payload) -> Envelope:
    return Envelope(trace_id=uuid.uuid4(), producer="test", type=type_name, payload=payload)


async def _get_json(queue, timeout=15.0):
    async def poll():
        while True:
            msg = await queue.get(fail=False)
            if msg is not None:
                return msg
            await asyncio.sleep(0.1)

    return await asyncio.wait_for(poll(), timeout)


# --- apply объявляет очереди и биндинги -----------------------------------------------------


async def test_apply_declares_queues_bindings_and_args(clean):
    url = clean
    topo = load_topology(ECHO_PAIR, CATALOG, environ={})
    report = await apply_topology(topo, amqp_url=url)

    assert set(report.exchanges) == {EXCHANGE, DLX}

    # очереди реально созданы
    for name in ("agentarium.echo", "agentarium.reverse", GATEWAY_QUEUE, DLQ):
        assert await _queue(url, name) is not None, f"{name} не создана"

    # рабочая очередь несёт ПОЛНЫЙ набор аргументов (эталон — bus.QUEUE_ARGUMENTS)
    echo_args = (await _queue(url, "agentarium.echo"))["arguments"]
    assert echo_args["x-queue-type"] == "quorum"
    assert echo_args["x-delivery-limit"] == 3
    assert echo_args["x-dead-letter-exchange"] == DLX
    assert echo_args["x-dead-letter-strategy"] == "at-least-once"
    assert echo_args["x-overflow"] == "reject-publish"

    # dlq — конец линии: quorum, но БЕЗ delivery-limit и БЕЗ DLX
    dlq_args = (await _queue(url, DLQ))["arguments"]
    assert dlq_args["x-queue-type"] == "quorum"
    assert "x-delivery-limit" not in dlq_args
    assert "x-dead-letter-exchange" not in dlq_args

    # биндинги по routes + служебный сток *.failed → dlq
    main = await _bindings(url, EXCHANGE)
    assert ("agentarium.echo", "echo.request") in main
    assert ("agentarium.reverse", "echo.done") in main
    assert (GATEWAY_QUEUE, "reverse.done") in main
    assert (GATEWAY_QUEUE, "task.failed") in main
    assert (DLQ, "*.failed") in main

    # dlq привязана и к fanout-DLX (конец мёртвой линии)
    assert any(dst == DLQ for dst, _ in await _bindings(url, DLX))


# --- реконсиляция снимает биндинг-сироту ----------------------------------------------------


async def test_reconcile_removes_orphan_binding(clean):
    url = clean
    topo = load_topology(ECHO_PAIR, CATALOG, environ={})
    await apply_topology(topo, amqp_url=url)

    # руками навешиваем лишний биндинг на живую (пустую) очередь
    conn = await aio_pika.connect_robust(url)
    ch = await conn.channel()
    ex = await ch.get_exchange(EXCHANGE, ensure=False)
    q = await ch.get_queue("agentarium.echo", ensure=False)
    await q.bind(ex, "ghost.route")
    await conn.close()
    assert ("agentarium.echo", "ghost.route") in await _bindings(url, EXCHANGE)

    report = await apply_topology(topo, amqp_url=url)  # реконсиляция снимает сироту
    assert ("agentarium.echo", "ghost.route") in report.bindings_removed
    assert ("agentarium.echo", "ghost.route") not in await _bindings(url, EXCHANGE)
    # штатный биндинг не тронут
    assert ("agentarium.echo", "echo.request") in await _bindings(url, EXCHANGE)


# --- дренаж: сироту с конвертами не сносим, отказ громкий ------------------------------------


async def test_reconcile_refuses_when_orphan_holds_envelopes(clean):
    url = clean
    topo = load_topology(ECHO_PAIR, CATALOG, environ={})
    await apply_topology(topo, amqp_url=url)

    # очередь-сирота с конвертом внутри
    conn = await aio_pika.connect_robust(url)
    ch = await conn.channel()
    ex = await ch.get_exchange(EXCHANGE, ensure=False)
    ghost = await ch.declare_queue(
        "agentarium.ghost", durable=True, arguments={"x-queue-type": "quorum"}
    )
    await ghost.bind(ex, "ghost.msg")
    await conn.close()

    bus = Bus(url)
    await bus.connect()
    await bus.publish(_env("ghost.msg", text="стой"))
    await bus.close()
    await _wait_depth(url, "agentarium.ghost", 1)

    with pytest.raises(ApplyError) as exc:
        await apply_topology(topo, amqp_url=url)
    assert "дренаж" in str(exc.value)
    assert "agentarium.ghost" in str(exc.value)
    # конверт цел — сироту не снесли
    assert (await _queue(url, "agentarium.ghost"))["messages"] >= 1


# --- fan-out: один конверт двум получателям -------------------------------------------------


async def test_fanout_delivers_to_both(clean, tmp_path):
    url = clean
    fanout = {
        "system": "fanout",
        "entry": "echo.request",
        "agents": {"echo": {"type": "echo"}, "echo2": {"type": "echo"}},
        "routes": {
            "echo.request": ["echo", "echo2"],
            "echo.done": ["gateway"],
            "task.failed": ["gateway"],
        },
        "finals": {"echo.done": "complete", "task.failed": "fail"},
    }
    path = tmp_path / "fanout.yaml"
    path.write_text(yaml.safe_dump(fanout, allow_unicode=True), encoding="utf-8")
    topo = load_topology(str(path), CATALOG, environ={})
    await apply_topology(topo, amqp_url=url)

    bus = Bus(url)
    await bus.connect()
    await bus.publish(_env("echo.request", text="hi"))
    for name in ("agentarium.echo", "agentarium.echo2"):
        q = await bus.channel.get_queue(name, ensure=False)
        msg = await _get_json(q)
        env = Envelope.model_validate_json(msg.body)
        await msg.ack()
        assert env.type == "echo.request"  # оба получателя получили один конверт
    await bus.close()


# --- цепочка echo → reverse собирается чертежом и гоняет конверт -----------------------------


async def test_chain_echo_reverse_over_blueprint(clean):
    url = clean
    topo = load_topology(ECHO_PAIR, CATALOG, environ={})
    await apply_topology(topo, amqp_url=url)

    echo_cls = load_agent_class(CATALOG["echo"].build)
    reverse_cls = load_agent_class(CATALOG["reverse"].build)

    bus_echo, bus_reverse, bus_pub = Bus(url), Bus(url), Bus(url)
    for b in (bus_echo, bus_reverse, bus_pub):
        await b.connect()

    echo = echo_cls(
        instance="echo", bus=bus_echo, queue=queue_name("echo"), retry_delays_s=(0, 0, 0)
    )
    reverse = reverse_cls(
        instance="reverse", bus=bus_reverse, queue=queue_name("reverse"), retry_delays_s=(0, 0, 0)
    )
    tasks = [asyncio.create_task(echo.run()), asyncio.create_task(reverse.run())]

    text = "привет мир"
    await bus_pub.publish(_env("echo.request", text=text))

    gateway = await bus_pub.channel.get_queue(GATEWAY_QUEUE, ensure=False)
    msg = await _get_json(gateway)
    out = Envelope.model_validate_json(msg.body)
    await msg.ack()

    assert out.type == "reverse.done"  # финал цепочки долетел до очереди шлюза
    assert out.payload["text"] == text[::-1]  # echo прокинул, reverse перевернул
    assert out.producer == "reverse"

    echo.stop()
    reverse.stop()
    await asyncio.gather(*tasks)
    for b in (bus_echo, bus_reverse, bus_pub):
        await b.close()
