"""SDK: базовый класс Agent — разъём агента к платформе. Контракт: spec/30.

Розетка: прибор любой, разъём один. SDK не знает про LLM-фреймворки и предметку.
Петля: конверт → валидация → handle (под вотчдогом, с ретраями) → publish+confirm → ack.
Ack входа — только после подтверждённой публикации исходящего: потерянных конвертов нет.
"""

import asyncio
import os
import uuid

import structlog
from aio_pika.abc import AbstractIncomingMessage
from opentelemetry.trace import SpanKind

from agentarium import contracts, observability
from agentarium.bus import Bus
from agentarium.envelope import Envelope, Reply
from agentarium.health import HealthServer

WATCHDOG_DEFAULT_S = 120.0
RETRY_DELAYS_S = (1.0, 3.0, 9.0)


class Agent:
    """Наследник реализует handle() и объявляет манифест consumes/produces (spec/30)."""

    consumes: list[str] = []
    produces: list[str] = []

    def __init__(
        self,
        *,
        instance: str,
        bus: Bus,
        queue: str,
        config: dict | None = None,
        watchdog_s: float = WATCHDOG_DEFAULT_S,
        retry_delays_s: tuple[float, ...] = RETRY_DELAYS_S,
        health_port: int | None = None,
    ):
        if not self.consumes or not self.produces:
            raise contracts.ContractError(
                f"тип {type(self).__name__} обязан объявить манифест consumes/produces — spec/30"
            )
        self.instance = instance
        self.config = config or {}
        self._bus = bus
        self._queue_name = queue
        self._watchdog_s = watchdog_s
        self._retry_delays_s = retry_delays_s
        self._stopping = asyncio.Event()
        self._current: asyncio.Task | None = None
        self._log = observability.get_logger(instance)  # ключ agent привязан ко всем строкам
        # HTTP /health поднимается в run(), если задан порт: launcher его задаёт (spec/40 п.4),
        # программные тесты — нет (иначе два агента в одном процессе дрались бы за порт 8000).
        self._health = (
            HealthServer(self.health, port=health_port) if health_port is not None else None
        )

    async def handle(self, envelope: Envelope) -> Reply | None:  # мозги — дело наследника
        raise NotImplementedError

    # --- жизненный цикл -----------------------------------------------------------------

    async def run(self) -> None:
        """Петля потребления. Очередь уже объявлена владельцем (topology apply) — passive."""
        observability.configure(self.instance)  # логи + трейсы бесплатно любому агенту (spec/50)
        if self._health is not None:
            await self._health.start()  # healthcheck образа становится правдой (spec/30, spec/40)
        queue = await self._bus.channel.get_queue(self._queue_name, ensure=False)
        consumer_tag = await queue.consume(self._on_message, no_ack=False)
        self._log.info("consuming", queue=self._queue_name)
        await self._stopping.wait()
        await queue.cancel(consumer_tag)
        if self._current is not None:
            await asyncio.shield(self._current)  # изящное завершение: начатое доводится
        if self._health is not None:
            await self._health.stop()

    def stop(self) -> None:
        self._stopping.set()

    def health(self) -> bool:
        """Жив = процесс жив и соединение с шиной живо (spec/30)."""
        return self._bus.is_alive and not self._stopping.is_set()

    # --- обработка одного конверта ------------------------------------------------------

    async def _on_message(self, message: AbstractIncomingMessage) -> None:
        self._current = asyncio.current_task()
        try:
            await self._process(message)
        finally:
            self._current = None

    async def _process(self, message: AbstractIncomingMessage) -> None:
        # Родительский спан — из AMQP-заголовков конверта: N обработок склеиваются в один
        # водопад через traceparent (spec/50). В теле конверта контекста нет (spec/20).
        parent = observability.extract_context(message.headers)
        with observability.get_tracer().start_as_current_span(
            "agent.handle", context=parent, kind=SpanKind.CONSUMER
        ) as span:
            try:
                envelope = Envelope.model_validate_json(message.body)
            except ValueError as exc:
                # Тело нечитаемо — контрактная ошибка: task.failed без ретраев (spec/50)
                span.set_attribute("attempt", 1)
                self._log.warning("contract_error", reason=str(exc), attempt=1)
                await self._publish_failed(message, reason=str(exc), attempts=1)
                await message.ack()
                return

            span.set_attribute("producer", envelope.producer)
            span.set_attribute("type", envelope.type)
            span.set_attribute("envelope.id", str(envelope.id))
            span.set_attribute("trace_id", str(envelope.trace_id))

            with structlog.contextvars.bound_contextvars(
                trace_id=str(envelope.trace_id), type=envelope.type
            ):
                try:
                    contracts.validate_payload(envelope.type, envelope.payload)
                except contracts.ContractError as exc:
                    # Невалидный payload не ретраится — это баг, повтор не чинит (spec/50)
                    span.set_attribute("attempt", 1)
                    self._log.warning("contract_error", reason=str(exc), attempt=1)
                    await self._publish_failed(message, reason=str(exc), attempts=1)
                    await message.ack()
                    return

                attempts = 0
                while True:
                    attempts += 1
                    span.set_attribute("attempt", attempts)
                    try:
                        reply = await asyncio.wait_for(
                            self.handle(envelope), timeout=self._watchdog_s
                        )
                        break
                    except TimeoutError:
                        self._log.critical("watchdog_timeout", timeout_s=self._watchdog_s)
                        self._fatal_exit()
                        return  # достижимо только в тестах, где _fatal_exit подменён
                    except Exception as exc:  # noqa: BLE001 — граница ретраев SDK: любое исключение
                        if attempts > len(self._retry_delays_s):
                            self._log.error(
                                "handle_exhausted", error=repr(exc), attempts=attempts
                            )
                            await self._publish_failed(
                                message, reason=repr(exc), attempts=attempts
                            )
                            await message.ack()
                            return
                        delay = self._retry_delays_s[attempts - 1]
                        self._log.warning(
                            "handle_retry", attempt=attempts, error=repr(exc), retry_in_s=delay
                        )
                        await asyncio.sleep(delay)

                if reply is not None:
                    self._check_produces(reply.type)
                    contracts.validate_payload(reply.type, reply.payload)
                    out = envelope.child(
                        producer=self.instance, type=reply.type, payload=reply.payload
                    )
                    # Публикация внутри спана: traceparent исходящего указывает на этот handle;
                    # confirm внутри publish — исключение оставит конверт неподтверждённым
                    await self._bus.publish(out)
                await message.ack()  # только после подтверждённой публикации — spec/30

    def _check_produces(self, type_name: str) -> None:
        if type_name not in self.produces:
            raise contracts.ContractError(
                f"тип '{type_name}' не объявлен в produces {self.produces} — манифест spec/30"
            )

    async def _publish_failed(
        self, message: AbstractIncomingMessage, *, reason: str, attempts: int
    ) -> None:
        """Ошибка — тоже конверт: подтверждённый task.failed, затем ack входа (spec/50)."""
        try:
            original = Envelope.model_validate_json(message.body)
            trace_id, failed_type = original.trace_id, original.type
            original_payload = original.payload
        except ValueError:
            trace_id, failed_type, original_payload = uuid.uuid4(), "unparseable", {}
        failed = Envelope(
            trace_id=trace_id,
            causation_id=None,
            producer=self.instance,
            type="task.failed",
            payload={
                "failed_type": failed_type,
                "reason": reason,
                "attempts": attempts,
                "original_payload": original_payload,
            },
        )
        await self._bus.publish(failed)

    def _fatal_exit(self) -> None:  # в тестах подменяется; в проде поднимет docker restart
        os._exit(70)
