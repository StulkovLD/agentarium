"""Интеграция S8: apply конфигурации B объявляет очередь auditor и переключает биндинги.

Топология объявляется чертежом dba-extended (не руками). Состояние брокера сверяется независимо —
через Management API, а не через код под тестом. Плана-финала больше нет: план идёт в очередь
auditor, финалом становится audit.done → gateway (spec/55).
"""

from urllib.parse import quote, unquote, urlsplit

import httpx
import pytest
from agentarium.apply import GATEWAY_QUEUE, apply_topology, queue_name
from agentarium.bus import DLQ, DLX, EXCHANGE
from agentarium.topology import load_catalog, load_topology

CATALOG = load_catalog("agents/catalog.yaml")
DBA_EXTENDED = "configs/dba-extended.yaml"
ENV = {"TARGET_DB_DSN": "postgresql://readonly_executor@localhost/billing"}

# Очереди конфигурации B — для чистки между прогонами.
_KNOWN_QUEUES = [
    "agentarium.parser",
    "agentarium.knowledge",
    "agentarium.executor",
    "agentarium.auditor",
    GATEWAY_QUEUE,
    DLQ,
]
_KNOWN_EXCHANGES = [EXCHANGE, DLX]


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


@pytest.fixture
async def clean(rabbitmq_url):
    await _purge(rabbitmq_url)
    yield rabbitmq_url
    await _purge(rabbitmq_url)


async def test_apply_extended_declares_auditor_queue_and_switched_bindings(clean):
    url = clean
    topo = load_topology(DBA_EXTENDED, CATALOG, environ=ENV)
    report = await apply_topology(topo, amqp_url=url)

    # очередь auditor реально создана вместе с остальными экземплярами конфигурации B
    assert queue_name("auditor") == "agentarium.auditor"
    for name in ("agentarium.parser", "agentarium.knowledge", "agentarium.executor",
                 "agentarium.auditor", GATEWAY_QUEUE, DLQ):
        assert await _queue(url, name) is not None, f"{name} не создана"

    # рабочая очередь auditor несёт полный набор аргументов quorum
    auditor_args = (await _queue(url, "agentarium.auditor"))["arguments"]
    assert auditor_args["x-queue-type"] == "quorum"
    assert auditor_args["x-dead-letter-exchange"] == DLX

    main = await _bindings(url, EXCHANGE)
    # биндинги переключены на аудит: план уходит аудитору, финал audit.done — в gateway
    assert ("agentarium.auditor", "plan.ready") in main
    assert (GATEWAY_QUEUE, "audit.done") in main
    # план больше НЕ ведёт в gateway (в базе вёл) — финал переключён
    assert (GATEWAY_QUEUE, "plan.ready") not in main
    # цепочка до аудитора цела
    assert ("agentarium.parser", "request.new") in main
    assert ("agentarium.executor", "knowledge.found") in main
    assert report.reconciled


async def test_apply_switch_base_to_extended_removes_stale_gateway_binding(clean):
    """Смена конфигурации A→B на одном брокере: биндинг старого финала снимается.

    Живой прод-сценарий: в A план — финал (plan.ready → gateway), в B план уходит аудитору.
    Без снятия сироты конверт plan.ready фан-аутится и в auditor, и в gateway; шлюз B не ждёт
    plan.ready (рассинхрон finals) → сброс в dlq → dlq-финализация убивает здоровую заявку.
    Чистый брокер этот случай не ловит — сирота существует только после реального перехода.
    """
    url = clean
    base_topo = load_topology("configs/dba-base.yaml", CATALOG, environ=ENV)
    await apply_topology(base_topo, amqp_url=url)
    assert (GATEWAY_QUEUE, "plan.ready") in await _bindings(url, EXCHANGE)  # в A план — финал

    ext_topo = load_topology(DBA_EXTENDED, CATALOG, environ=ENV)
    report = await apply_topology(ext_topo, amqp_url=url)

    main = await _bindings(url, EXCHANGE)
    assert (GATEWAY_QUEUE, "plan.ready") not in main, "сирота старого финала пережила реконсиляцию"
    assert ("agentarium.auditor", "plan.ready") in main
    assert (GATEWAY_QUEUE, "audit.done") in main
    assert (GATEWAY_QUEUE, "plan.ready") in report.bindings_removed
