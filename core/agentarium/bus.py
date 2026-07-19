"""Клиент шины: надёжная публикация поверх aio-pika. Контракты: spec/30, spec/50.

Гарантия: durable + persistent + publisher confirms + mandatory. Confirm без mandatory
не заметил бы конверт, улетевший в exchange без подходящего биндинга.
Объявление AMQP-объектов — не здесь: владелец — topology apply (spec/40).
"""

import aio_pika
from aio_pika.abc import AbstractRobustChannel, AbstractRobustConnection

from agentarium.envelope import Envelope

EXCHANGE = "agentarium"
DLX = "agentarium.dlx"
DLQ = "agentarium.dlq"

# Полный набор аргументов рабочей очереди — проекция единственного владельца перечня, spec/40 п.2.
QUEUE_ARGUMENTS = {
    "x-queue-type": "quorum",
    "x-delivery-limit": 3,
    "x-dead-letter-exchange": DLX,
    "x-dead-letter-strategy": "at-least-once",
    "x-overflow": "reject-publish",
}


class UnroutableEnvelope(Exception):
    """Конверт подтверждён exchange-ем, но не нашёл ни одного биндинга — это ошибка чертежа."""


class Bus:
    """Соединение + канал с confirms; publish с mandatory. prefetch=1 — spec/30."""

    def __init__(self, url: str):
        self._url = url
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractRobustChannel | None = None

    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel(
            publisher_confirms=True, on_return_raises=True
        )
        await self._channel.set_qos(prefetch_count=1)

    @property
    def channel(self) -> AbstractRobustChannel:
        if self._channel is None:
            raise RuntimeError("Bus не подключён: сначала await connect()")
        return self._channel

    @property
    def is_alive(self) -> bool:
        return bool(
            self._connection
            and not self._connection.is_closed
            and self._channel
            and not self._channel.is_closed
        )

    async def publish(self, envelope: Envelope, *, exchange: str = EXCHANGE) -> None:
        """Подтверждённая публикация. Возврат из метода = брокер принял и смаршрутизировал."""
        ex = await self.channel.get_exchange(exchange, ensure=False)
        message = aio_pika.Message(
            body=envelope.model_dump_json().encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=str(envelope.id),
            headers={"trace_id": str(envelope.trace_id)},
        )
        try:
            await ex.publish(message, routing_key=envelope.type, mandatory=True)
        except aio_pika.exceptions.DeliveryError as exc:
            raise UnroutableEnvelope(
                f"конверт '{envelope.type}' не нашёл маршрута: чертёж не ведёт его никуда — spec/40"
            ) from exc

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
