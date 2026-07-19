"""Шлюз: FastAPI по HTTP-контракту spec/40 + консюмеры финалов/dlq. Root-спан заявки — spec/50.

Универсален: предметных имён типов в коде нет. entry и finals берутся из чертежа (смонтирован по
env AGENTARIUM_CONFIG), карту finals исполняет консюмер. Порядок POST — publish→insert:
подтверждённая публикация entry-конверта, затем INSERT `accepted`, затем 202; публикация не удалась
→ 503 и заявки нет. GET читает статус/результат заявки или 404.
"""

import contextlib
import os
import uuid

from agentarium import observability
from agentarium.bus import Bus
from agentarium.envelope import Envelope
from agentarium.storage import postgres_pool
from agentarium.topology import Topology, load_catalog, load_topology
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .consumers import GatewayConsumers
from .secrets import check_secrets
from .store import get_request, insert_accepted

DEFAULT_AMQP = "amqp://agentarium:agentarium@localhost:5672/"
DEFAULT_DSN = "postgresql://agentarium:agentarium@localhost:5432/agentarium"
DEFAULT_CATALOG = os.environ.get("AGENTARIUM_CATALOG", "agents/catalog.yaml")
GATEWAY_PRODUCER = "gateway"  # служебное имя входа/выхода (spec/40), не предметный тип


class SubmitRequest(BaseModel):
    """Тело POST /requests — контракт entry-типа {text: str} (spec/40)."""

    text: str = Field(min_length=1)


def _register_routes(app: FastAPI) -> None:
    @app.post("/requests", status_code=202)
    async def submit(body: SubmitRequest, request: Request) -> dict[str, str]:
        state = request.app.state
        trace_id = uuid.uuid4()
        entry = Envelope(
            trace_id=trace_id,
            producer=GATEWAY_PRODUCER,
            type=state.topology.entry,
            payload={"text": body.text},
        )
        # Root-спан заявки: бизнес-trace_id атрибутом, traceparent инжектит bus.publish (spec/50).
        with observability.get_tracer().start_as_current_span("gateway.request") as span:
            span.set_attribute("trace_id", str(trace_id))
            try:
                # Подтверждённая публикация; любой отказ (unroutable/обрыв) → 503, заявки нет.
                await state.bus.publish(entry)
            except Exception as exc:  # noqa: BLE001 — граница HTTP: любой отказ публикации = 503
                raise HTTPException(
                    status_code=503,
                    detail=f"публикация entry-конверта не удалась, заявка не рождена: {exc}",
                ) from exc
            # Публикация подтверждена — заявка родилась на шине. INSERT после публикации (spec/40);
            # его отказ не теряет заявку: финал сам создаст строку условным UPSERT (store.finalize).
            try:
                await insert_accepted(state.pool, trace_id, body.text)
            except Exception as exc:  # noqa: BLE001 — заявка уже на шине; строку восстановит финал
                observability.get_logger(GATEWAY_PRODUCER).warning(
                    "accepted_insert_failed_will_self_heal",
                    trace_id=str(trace_id),
                    error=repr(exc),
                )
        return {"trace_id": str(trace_id)}

    @app.get("/requests/{trace_id}")
    async def status(trace_id: uuid.UUID, request: Request) -> dict[str, object]:
        found = await get_request(request.app.state.pool, trace_id)
        if found is None:
            raise HTTPException(status_code=404, detail="нет такой заявки")
        return found

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        # «Жив» = соединение с шиной живо (spec/50): без шины шлюз не примет и не завершит заявку.
        if not request.app.state.bus.is_alive:
            raise HTTPException(status_code=503, detail="шина недоступна")
        return {"status": "ok"}


def make_app(*, topology: Topology, bus: Bus, pool: object) -> FastAPI:
    """Собрать приложение на готовых зависимостях — путь тестов (шина/пул уже подняты и связаны)."""
    app = FastAPI(title="agentarium gateway")
    app.state.topology = topology
    app.state.bus = bus
    app.state.pool = pool
    _register_routes(app)
    return app


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    """Прод-обвязка: fail-fast секретов → чертёж → шины → пул(+миграция) → консюмеры финалов/dlq.

    Две шины: POST публикует конкурентно с колбэками консюмеров, а один канал aio-pika не рассчитан
    на конкурентный доступ из разных задач — поэтому публикация и потребление на разных соединениях.
    """
    check_secrets()  # первым: уцелевший плейсхолдер env.example → отказ старта до любых соединений
    observability.configure(GATEWAY_PRODUCER)
    config_path = os.environ.get("AGENTARIUM_CONFIG")
    if not config_path:
        raise SystemExit("gateway: не задан AGENTARIUM_CONFIG (путь к смонтированному чертежу)")
    topo = load_topology(config_path, load_catalog(DEFAULT_CATALOG))

    amqp_url = os.environ.get("RABBITMQ_URL", DEFAULT_AMQP)
    publish_bus = Bus(amqp_url)
    consume_bus = Bus(amqp_url)
    await publish_bus.connect()
    await consume_bus.connect()
    pool = await postgres_pool(os.environ.get("POSTGRES_DSN", DEFAULT_DSN))
    consumers = GatewayConsumers(bus=consume_bus, pool=pool, finals=topo.finals)
    await consumers.start()

    app.state.topology = topo
    app.state.bus = publish_bus
    app.state.pool = pool
    app.state.consumers = consumers
    try:
        yield
    finally:
        await consumers.stop()
        await consume_bus.close()
        await publish_bus.close()
        await pool.close()


def build() -> FastAPI:
    """Прод-приложение: зависимости поднимает lifespan из окружения (env + чертёж по mount)."""
    app = FastAPI(title="agentarium gateway", lifespan=_lifespan)
    _register_routes(app)
    return app


app = build()
