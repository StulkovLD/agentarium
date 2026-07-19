"""topology apply: чертёж → живая AMQP-топология. Единственный, кто разговаривает с брокером.

Контракт: spec/40 «Как чертёж становится системой». Владелец объявления exchange/очередей/
биндингов — только apply; SDK агента делает лишь passive-проверку своей очереди.

Объявляет: topic-exchange `agentarium`, fanout-DLX `agentarium.dlx`, quorum-DLQ `agentarium.dlq`
(конец линии: без delivery-limit и без DLX), рабочие очереди агентов и шлюза с полным набором
аргументов (эталон перечня — bus.QUEUE_ARGUMENTS), биндинги по routes + служебный сток
`*.failed → dlq`. Реконсиляция: diff текущих биндингов через Management API — лишние снять,
но очередь-сироту с конвертами не сносить (дренаж, spec/40 п.3).
"""

from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

import aio_pika
import httpx

from agentarium.bus import DLQ, DLX, EXCHANGE, QUEUE_ARGUMENTS
from agentarium.topology import GATEWAY, Topology

GATEWAY_QUEUE = "agentarium.gateway"
FAILED_WILDCARD = "*.failed"  # служебный сток: любой X.failed дублируется в dlq для разбора
MANAGEMENT_PORT = 15672

# Служебные стоки — в защищённом списке: diff реконсиляции их не трогает (spec/40 п.3).
_PROTECTED_QUEUES = frozenset({DLQ, GATEWAY_QUEUE})

# dlq — исключение из QUEUE_ARGUMENTS: конец линии, без delivery-limit и без DLX (spec/40 п.2).
_DLQ_ARGUMENTS = {"x-queue-type": "quorum"}


def queue_name(instance: str) -> str:
    """Имя рабочей очереди экземпляра. gateway — своё зарезервированное имя."""
    return GATEWAY_QUEUE if instance == GATEWAY else f"agentarium.{instance}"


class ApplyError(Exception):
    """Apply отказывается менять топологию: дренаж не пройден (в сироте лежат конверты)."""


@dataclass
class ApplyReport:
    """Что apply сделал с брокером — для человека и для make."""

    exchanges: list[str] = field(default_factory=list)
    queues: list[str] = field(default_factory=list)
    bindings_declared: list[tuple[str, str]] = field(default_factory=list)
    bindings_removed: list[tuple[str, str]] = field(default_factory=list)
    reconciled: bool = False

    def render(self) -> str:
        lines = [
            f"exchanges: {', '.join(self.exchanges)}",
            f"очереди:   {', '.join(self.queues)}",
            f"биндинги объявлены: {len(self.bindings_declared)}",
        ]
        if self.reconciled:
            lines.append(f"биндинги-сироты сняты: {len(self.bindings_removed)}")
            for qn, rk in self.bindings_removed:
                lines.append(f"  − {qn} ⇠ {rk}")
        else:
            lines.append("реконсиляция пропущена: идемпотентное объявление без снятия сирот")
        return "\n".join(lines)


async def apply_topology(
    topo: Topology,
    *,
    amqp_url: str,
    reconcile: bool = True,
) -> ApplyReport:
    """Привести брокер к чертежу. reconcile=False — страховка S3: объявить, сирот не трогать."""
    report = ApplyReport(reconciled=reconcile)
    connection = await aio_pika.connect_robust(amqp_url)
    try:
        channel = await connection.channel()
        main = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
        dlx = await channel.declare_exchange(DLX, aio_pika.ExchangeType.FANOUT, durable=True)
        report.exchanges = [EXCHANGE, DLX]

        # dlq — конец линии; привязан к dlx (fanout) и к основному exchange по `*.failed`
        dlq = await channel.declare_queue(DLQ, durable=True, arguments=dict(_DLQ_ARGUMENTS))
        await dlq.bind(dlx, routing_key="")
        await dlq.bind(main, routing_key=FAILED_WILDCARD)

        # рабочие очереди: экземпляры + шлюз, полный набор аргументов
        by_name: dict[str, aio_pika.abc.AbstractQueue] = {DLQ: dlq}
        for name in topo.agents:
            qn = queue_name(name)
            by_name[qn] = await channel.declare_queue(
                qn, durable=True, arguments=dict(QUEUE_ARGUMENTS)
            )
        by_name[GATEWAY_QUEUE] = await channel.declare_queue(
            GATEWAY_QUEUE, durable=True, arguments=dict(QUEUE_ARGUMENTS)
        )
        report.queues = sorted(by_name)

        # биндинги = маршруты (тип → очередь экземпляра/шлюза)
        desired: set[tuple[str, str]] = {(DLQ, FAILED_WILDCARD)}
        for msg_type, names in topo.routes.items():
            for name in names:
                qn = queue_name(name)
                await by_name[qn].bind(main, routing_key=msg_type)
                desired.add((qn, msg_type))
        report.bindings_declared = sorted(desired)

        if reconcile:
            await _reconcile(channel, main, amqp_url, desired, report)
    finally:
        await connection.close()
    return report


async def _reconcile(
    channel: aio_pika.abc.AbstractChannel,
    main: aio_pika.abc.AbstractExchange,
    amqp_url: str,
    desired: set[tuple[str, str]],
    report: ApplyReport,
) -> None:
    """diff биндингов основного exchange через Management API: лишние снять после дренажа."""
    base_url, auth, vhost = _mgmt_from_amqp(amqp_url)
    async with httpx.AsyncClient(base_url=base_url, auth=auth, timeout=10.0) as http:
        mgmt = _Management(http, vhost)
        current = await mgmt.source_bindings(EXCHANGE)
        orphans = [
            (qn, rk)
            for (qn, rk) in current
            if (qn, rk) not in desired and qn not in _PROTECTED_QUEUES
        ]

        # дренаж: снимать биндинг у очереди с конвертами нельзя — иначе заявка зависнет в сироте
        blocked = []
        for qn, _rk in orphans:
            depth = await mgmt.queue_depth(qn)
            if depth > 0:
                blocked.append((qn, depth))
        if blocked:
            detail = "; ".join(f"{qn}: {depth} конвертов" for qn, depth in blocked)
            raise ApplyError(
                f"дренаж не пройден — в очередях-сиротах лежат конверты ({detail}). "
                f"Дождись обработки или разбери dlq: снимать биндинг сейчас значит бросить заявку."
            )

        for qn, rk in orphans:
            queue = await channel.get_queue(qn, ensure=False)
            await queue.unbind(main, routing_key=rk)
            report.bindings_removed.append((qn, rk))


# --- Management API -----------------------------------------------------------------------


class _Management:
    """Тонкий клиент RabbitMQ Management API: только чтение diff-а и глубины очереди."""

    def __init__(self, http: httpx.AsyncClient, vhost: str):
        self._http = http
        self._vhost = quote(vhost, safe="")

    async def source_bindings(self, exchange: str) -> list[tuple[str, str]]:
        """Биндинги, где exchange — источник. Только назначения-очереди: (очередь, routing_key)."""
        resp = await self._http.get(
            f"/api/exchanges/{self._vhost}/{quote(exchange, safe='')}/bindings/source"
        )
        resp.raise_for_status()
        return [
            (b["destination"], b["routing_key"])
            for b in resp.json()
            if b.get("destination_type") == "queue"
        ]

    async def queue_depth(self, queue: str) -> int:
        resp = await self._http.get(f"/api/queues/{self._vhost}/{quote(queue, safe='')}")
        resp.raise_for_status()
        return int(resp.json().get("messages", 0))


def _mgmt_from_amqp(amqp_url: str) -> tuple[str, tuple[str, str], str]:
    """amqp://user:pass@host:5672/vhost → (http://host:15672, (user, pass), vhost)."""
    parts = urlsplit(amqp_url)
    host = parts.hostname or "localhost"
    user = unquote(parts.username or "guest")
    password = unquote(parts.password or "guest")
    raw_vhost = parts.path
    vhost = "/" if raw_vhost in ("", "/") else unquote(raw_vhost.lstrip("/"))
    return f"http://{host}:{MANAGEMENT_PORT}", (user, password), vhost
