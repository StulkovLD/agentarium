"""E2E шлюза на echo-цепочке против живых RabbitMQ + Postgres. Контракт: spec/40, spec/50.

Топология собирается чертёжом configs/echo-pair.yaml (apply объявляет очереди/биндинги), агенты
echo/reverse поднимаются программно, шлюз (FastAPI) гоняется в процессе через ASGI-транспорт httpx,
консюмеры финалов/dlq — на своей шине. Проверяем весь путь: POST → шина → финал → GET, плюс
task.failed, идемпотентность повторного финала, 404, финал раньше INSERT, dlq-консюмер.

Бегут с хоста: Postgres на localhost:5433 (compose пробрасывает 5432→5433), RabbitMQ на 5672.
"""

import asyncio
import os
import uuid
from urllib.parse import quote, unquote, urlsplit

import aio_pika
import httpx
import pytest_asyncio
from agentarium import Bus, Envelope
from agentarium.__main__ import load_agent_class
from agentarium.apply import apply_topology, queue_name
from agentarium.bus import DLQ
from agentarium.storage import postgres_pool
from agentarium.topology import load_catalog, load_topology
from gateway import GatewayConsumers, make_app

CATALOG = load_catalog("agents/catalog.yaml")
ECHO_PAIR = "configs/echo-pair.yaml"
POSTGRES_DSN = os.environ.get(
    "POSTGRES_DSN", "postgresql://agentarium:agentarium@localhost:5433/agentarium"
)

_KNOWN_QUEUES = ["agentarium.echo", "agentarium.reverse", "agentarium.gateway", DLQ]
_KNOWN_EXCHANGES = ["agentarium", "agentarium.dlx"]


# --- чистка брокера через Management API (независимо от кода под тестом) --------------------


def _mgmt(url: str):
    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    auth = (unquote(parts.username or "guest"), unquote(parts.password or "guest"))
    vhost = parts.path
    vhost = "/" if vhost in ("", "/") else unquote(vhost.lstrip("/"))
    return f"http://{host}:15672", auth, quote(vhost, safe="")


async def _purge(url: str) -> None:
    base, auth, vhost = _mgmt(url)
    async with httpx.AsyncClient(base_url=base, auth=auth, timeout=10.0) as c:
        for q in _KNOWN_QUEUES:
            await c.delete(f"/api/queues/{vhost}/{quote(q, safe='')}")
        for ex in _KNOWN_EXCHANGES:
            await c.delete(f"/api/exchanges/{vhost}/{quote(ex, safe='')}")


# --- фикстуры инфраструктуры ---------------------------------------------------------------


@pytest_asyncio.fixture
async def applied(rabbitmq_url):
    """Чистый брокер по чертежу echo-pair: очереди echo/reverse/gateway/dlq + биндинги."""
    await _purge(rabbitmq_url)
    topo = load_topology(ECHO_PAIR, CATALOG, environ={})
    await apply_topology(topo, amqp_url=rabbitmq_url)
    yield topo
    await _purge(rabbitmq_url)


@pytest_asyncio.fixture
async def pool():
    p = await postgres_pool(POSTGRES_DSN)  # применяет миграцию requests
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM requests")
    yield p
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM requests")
    await p.close()


@pytest_asyncio.fixture
async def publish_bus(applied, rabbitmq_url):
    b = Bus(rabbitmq_url)
    await b.connect()
    yield b
    await b.close()


@pytest_asyncio.fixture
async def consume_bus(applied, rabbitmq_url):
    b = Bus(rabbitmq_url)
    await b.connect()
    yield b
    await b.close()


@pytest_asyncio.fixture
async def consumers(consume_bus, pool, applied):
    c = GatewayConsumers(bus=consume_bus, pool=pool, finals=applied.finals, db_retry_pause_s=0.1)
    await c.start()
    yield c
    await c.stop()


@pytest_asyncio.fixture
async def client(applied, publish_bus, pool):
    app = make_app(topology=applied, bus=publish_bus, pool=pool)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
        yield c


# --- помощники агентов и опроса ------------------------------------------------------------


async def _start_agents(url: str, specs):
    started = []
    for instance, cls in specs:
        bus = Bus(url)
        await bus.connect()
        agent = cls(
            instance=instance, bus=bus, queue=queue_name(instance), retry_delays_s=(0, 0, 0)
        )
        started.append((agent, asyncio.create_task(agent.run()), bus))
    return started


async def _stop_agents(started) -> None:
    for agent, _task, _bus in started:
        agent.stop()
    await asyncio.gather(*(task for _agent, task, _bus in started))
    for _agent, _task, bus in started:
        await bus.close()


async def _poll(client, trace_id, want, timeout=20.0):
    async def loop():
        while True:
            resp = await client.get(f"/requests/{trace_id}")
            if resp.status_code == 200 and resp.json()["status"] == want:
                return resp.json()
            await asyncio.sleep(0.1)

    return await asyncio.wait_for(loop(), timeout)


def _final(type_name: str, trace_id: uuid.UUID, **payload) -> Envelope:
    return Envelope(trace_id=trace_id, producer="test", type=type_name, payload=payload)


# --- happy path: POST → шина → финал → GET отдаёт перевёрнутый text -------------------------


async def test_e2e_happy_path_echo_reverse(client, consumers, rabbitmq_url):
    echo_cls = load_agent_class(CATALOG["echo"].build)
    reverse_cls = load_agent_class(CATALOG["reverse"].build)
    agents = await _start_agents(rabbitmq_url, [("echo", echo_cls), ("reverse", reverse_cls)])
    try:
        text = "привет мир"
        resp = await client.post("/requests", json={"text": text})
        assert resp.status_code == 202
        trace_id = resp.json()["trace_id"]

        done = await _poll(client, trace_id, "done")
        assert done["status"] == "done"
        assert done["result"]["text"] == text[::-1]  # echo прокинул, reverse перевернул
    finally:
        await _stop_agents(agents)


# --- task.failed: сломанный агент → заявка failed ------------------------------------------


async def test_e2e_task_failed(client, consumers, rabbitmq_url):
    echo_cls = load_agent_class(CATALOG["echo"].build)

    class BrokenEcho(echo_cls):
        async def handle(self, envelope):
            raise RuntimeError("сломанный агент")

    agents = await _start_agents(rabbitmq_url, [("echo", BrokenEcho)])
    try:
        resp = await client.post("/requests", json={"text": "уронись"})
        trace_id = resp.json()["trace_id"]

        # task.failed летит и в gateway (финал fail), и по *.failed в dlq — оба ставят failed;
        # «первый выигрывает», статус детерминирован (failed); содержимое result — гонка, мимо.
        failed = await _poll(client, trace_id, "failed")
        assert failed["status"] == "failed"
        assert failed["result"] is not None
    finally:
        await _stop_agents(agents)


# --- идемпотентность: повторный финал того же trace_id игнорируется -------------------------


async def test_repeated_final_is_idempotent(client, consumers, publish_bus, pool):
    trace_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO requests (trace_id, text, status) VALUES ($1, $2, 'accepted')",
            trace_id,
            "исходный текст",
        )

    await publish_bus.publish(_final("reverse.done", trace_id, text="первый", n=1))
    done = await _poll(client, trace_id, "done")
    assert done["result"]["n"] == 1

    # второй финал с иным payload — «первый выигрывает», строка уже терминальна → не трогаем
    await publish_bus.publish(_final("reverse.done", trace_id, text="второй", n=2))
    await asyncio.sleep(1.5)  # дать финалу пройти консюмер (и быть проигнорированным)
    again = await client.get(f"/requests/{trace_id}")
    assert again.json()["result"]["n"] == 1  # результат не перезаписан


# --- 404 на неизвестный trace_id -----------------------------------------------------------


async def test_get_unknown_trace_id_returns_404(client):
    resp = await client.get(f"/requests/{uuid.uuid4()}")
    assert resp.status_code == 404


# --- финал раньше INSERT: recovery-UPSERT создаёт строку ------------------------------------


async def test_final_before_insert_creates_row(client, consumers, publish_bus, pool):
    trace_id = uuid.uuid4()  # НИКАКОГО accepted-INSERT: шлюз «упал» между публикацией и записью
    await publish_bus.publish(_final("reverse.done", trace_id, text="восстановлено"))

    done = await _poll(client, trace_id, "done")
    assert done["status"] == "done"
    assert done["result"]["text"] == "восстановлено"

    # строку создал сам финал условным UPSERT-ом, текст — из конверта финала (spec/40)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT text, status FROM requests WHERE trace_id = $1", trace_id
        )
    assert row is not None
    assert row["status"] == "done"
    assert row["text"] == "восстановлено"


# --- dlq-консюмер: мёртвый конверт → заявка failed по trace_id ------------------------------


async def test_dlq_consumer_marks_failed(client, consumers, publish_bus, pool):
    trace_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO requests (trace_id, text, status) VALUES ($1, $2, 'accepted')",
            trace_id,
            "исходный текст",
        )

    # конверт, погибший мимо task.failed, кладём прямо в agentarium.dlq (через default exchange)
    dead = _final("echo.done", trace_id, text="исходный текст")
    await publish_bus.channel.default_exchange.publish(
        aio_pika.Message(dead.model_dump_json().encode(), content_type="application/json"),
        routing_key=DLQ,
    )

    failed = await _poll(client, trace_id, "failed")
    assert failed["status"] == "failed"
    assert failed["result"] is not None  # причина «конверт мёртв» записана
