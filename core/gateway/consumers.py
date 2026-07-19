"""Консюмеры шлюза: финалы (agentarium.gateway) и тихая смерть (agentarium.dlq). Спека: spec/40, 50.

Финал-консюмер исполняет карту finals чертежа («первый выигрывает»), dlq-консюмер переводит
мёртвый конверт в `failed` по trace_id — заявок, навсегда зависших в `accepted`, не существует.
Оба универсальны: предметных имён типов в коде шлюза нет, вердикт берётся из finals, текст —
детерминированным порядком spec/40. Оба ack-ают ТОЛЬКО после успешного коммита; ошибка БД →
конверт держится unacked, запись повторяется с паузой; nack — только при shutdown.
"""

import asyncio
import uuid
from typing import Any

import structlog
from agentarium import observability
from agentarium.apply import GATEWAY_QUEUE
from agentarium.bus import DLQ, DLX, Bus
from agentarium.envelope import Envelope
from aio_pika.abc import AbstractIncomingMessage
from opentelemetry.trace import SpanKind

from .store import DB_ERRORS, finalize

# Статус заявки по вердикту finals чертежа: complete → done, любой иной (fail) → failed (spec/45).
_STATUS_BY_VERDICT = {"complete": "done", "fail": "failed"}
DB_RETRY_PAUSE_S = 1.0  # пауза между повторами записи при транзиентном отказе БД (spec/40)


def extract_text(payload: dict[str, Any]) -> str | None:
    """Текст заявки из payload детерминированным порядком, одним на все типы (spec/40).

    Порядок: payload.request.text → payload.text → (для task.failed) тот же порядок внутри
    original_payload. Его несёт каждый продуктовый тип по правилу трубы; None — контрактный баг.
    """
    direct = _text_of(payload)
    if direct is not None:
        return direct
    original = payload.get("original_payload")
    if isinstance(original, dict):
        return _text_of(original)
    return None


def _text_of(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    request = node.get("request")
    if isinstance(request, dict) and isinstance(request.get("text"), str):
        return request["text"]
    text = node.get("text")
    return text if isinstance(text, str) else None


class GatewayConsumers:
    """Две петли потребления на общей шине шлюза: финалы и dlq. Контракт записи — store.finalize."""

    def __init__(
        self,
        *,
        bus: Bus,
        pool: Any,
        finals: dict[str, str],
        db_retry_pause_s: float = DB_RETRY_PAUSE_S,
    ):
        self._bus = bus
        self._pool = pool
        self._finals = finals
        self._db_retry_pause_s = db_retry_pause_s
        self._stopping = asyncio.Event()
        self._subs: list[tuple[Any, str]] = []
        self._log = observability.get_logger("gateway")

    async def start(self) -> None:
        """Подписаться на очередь финалов и на dlq. Очереди объявил владелец — topology apply."""
        gateway_queue = await self._bus.channel.get_queue(GATEWAY_QUEUE, ensure=False)
        dlq = await self._bus.channel.get_queue(DLQ, ensure=False)
        self._subs.append((gateway_queue, await gateway_queue.consume(self._on_final)))
        self._subs.append((dlq, await dlq.consume(self._on_dlq)))
        self._log.info("gateway_consuming", queues=[GATEWAY_QUEUE, DLQ])

    async def stop(self) -> None:
        self._stopping.set()
        for queue, tag in self._subs:
            await queue.cancel(tag)
        self._subs.clear()

    # --- финал-консюмер -----------------------------------------------------------------

    async def _on_final(self, message: AbstractIncomingMessage) -> None:
        envelope = self._parse(message)
        if envelope is None:
            await message.ack()  # тело нечитаемо, trace_id неизвестен — делать нечего, кроме лога
            return
        verdict = self._finals.get(envelope.type)
        if verdict is None:
            # В очередь шлюза ведут только типы из finals (двусторонняя проверка рубежа 1, spec/40);
            # сюда — только при рассинхроне. Контрактный баг: в dlq с громким логом, не тихо теряем.
            self._log.error(
                "final_type_not_in_finals",
                type=envelope.type,
                trace_id=str(envelope.trace_id),
            )
            await self._escape_to_dlq(envelope, message)
            return
        status = _STATUS_BY_VERDICT[verdict]
        text = extract_text(envelope.payload)
        if text is None:
            self._log.error(
                "text_not_found_contract_bug",
                type=envelope.type,
                trace_id=str(envelope.trace_id),
            )
            await self._escape_to_dlq(envelope, message)
            return
        await self._commit(message, envelope, status=status, result=envelope.payload, text=text)

    # --- dlq-консюмер: тихая смерть → failed --------------------------------------------

    async def _on_dlq(self, message: AbstractIncomingMessage) -> None:
        envelope = self._parse(message)
        if envelope is None:
            await message.ack()
            return
        result = {
            "reason": "конверт мёртв — переведён в failed dlq-консюмером шлюза (spec/50)",
            "dead_letter_type": envelope.type,
        }
        # Конец линии: текст не извлёкся — обновляем существующую строку, новую не выдумываем.
        text = extract_text(envelope.payload)
        if text is None:
            self._log.error(
                "dlq_text_not_found_contract_bug",
                type=envelope.type,
                trace_id=str(envelope.trace_id),
            )
        await self._commit(message, envelope, status="failed", result=result, text=text)

    # --- общая запись: ack только после коммита, ошибка БД → держим unacked --------------

    async def _commit(
        self,
        message: AbstractIncomingMessage,
        envelope: Envelope,
        *,
        status: str,
        result: dict[str, Any],
        text: str | None,
    ) -> None:
        parent = observability.extract_context(message.headers)
        with observability.get_tracer().start_as_current_span(
            "gateway.finalize", context=parent, kind=SpanKind.CONSUMER
        ) as span:
            span.set_attribute("type", envelope.type)
            span.set_attribute("trace_id", str(envelope.trace_id))
            span.set_attribute("status", status)
            with structlog.contextvars.bound_contextvars(
                trace_id=str(envelope.trace_id), type=envelope.type
            ):
                while not self._stopping.is_set():
                    try:
                        await finalize(
                            self._pool,
                            envelope.trace_id,
                            status=status,
                            result=result,
                            text=text,
                        )
                        await message.ack()  # только после успешного коммита — spec/40
                        self._log.info("finalized", status=status)
                        return
                    except DB_ERRORS as exc:
                        # Не отпускаем: остаётся unacked, повтор в процессе с паузой (spec/40).
                        self._log.error("db_error_holding_unacked", error=repr(exc))
                        await asyncio.sleep(self._db_retry_pause_s)
                await message.nack(requeue=True)  # nack — только при shutdown

    async def _escape_to_dlq(
        self, envelope: Envelope, message: AbstractIncomingMessage
    ) -> None:
        """Контрактный баг финала: конверт в dlq через DLX с громким логом, затем ack (не nack)."""
        await self._bus.publish(envelope, exchange=DLX)
        await message.ack()

    def _parse(self, message: AbstractIncomingMessage) -> Envelope | None:
        try:
            return Envelope.model_validate_json(message.body)
        except ValueError as exc:
            self._log.error("unparseable_envelope", reason=str(exc), id=str(uuid.uuid4()))
            return None
