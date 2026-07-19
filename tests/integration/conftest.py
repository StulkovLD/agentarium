"""Каркас интеграционных тестов: живой RabbitMQ и его контракт очереди из spec/40.

Тесты бегут с хоста, поэтому дефолт RABBITMQ_URL — localhost, а не compose-хост `rabbitmq`.
Переопределяется переменной окружения RABBITMQ_URL (в CI/compose-сети — amqp://…@rabbitmq:5672/).
"""

import os
import uuid
from pathlib import Path

import aio_pika
import pytest
import pytest_asyncio

_INTEGRATION_DIR = Path(__file__).parent

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://agentarium:agentarium@localhost:5672/")

# Полный набор аргументов РАБОЧЕЙ quorum-очереди. Единственный владелец перечня —
# spec/40-topology.md, раздел «Как чертёж становится системой», п.2. Здесь копия для теста.
# (agentarium.dlq — исключение: quorum без delivery-limit и без DLX; тут объявляем рабочую очередь.)
WORKING_QUEUE_ARGS = {
    "x-queue-type": "quorum",
    "x-delivery-limit": 3,
    "x-dead-letter-exchange": "agentarium.dlx",
    "x-dead-letter-strategy": "at-least-once",
    "x-overflow": "reject-publish",
}


@pytest.fixture
def rabbitmq_url() -> str:
    return RABBITMQ_URL


def pytest_collection_modifyitems(items):
    """Всё в tests/integration — интеграционное. Клеим маркер `integration` каждому тесту папки,
    чтобы `-m integration` ловил их без ручной разметки в каждом модуле."""
    for item in items:
        if _INTEGRATION_DIR in item.path.parents:
            item.add_marker("integration")


@pytest_asyncio.fixture
async def connection():
    """Robust-подключение aio-pika к живому брокеру по RABBITMQ_URL."""
    conn = await aio_pika.connect_robust(RABBITMQ_URL)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def channel(connection):
    """Канал с publisher confirms: publish возвращается только после basic.ack брокера."""
    ch = await connection.channel(publisher_confirms=True)
    try:
        yield ch
    finally:
        await ch.close()


@pytest_asyncio.fixture
async def temp_quorum_queue(channel):
    """Временная рабочая quorum-очередь с ПОЛНЫМ набором аргументов spec/40; сносится после теста.

    quorum-очередь не может быть exclusive/auto-delete — потому удаляем явно в teardown.
    """
    name = f"agentarium.test.{uuid.uuid4().hex}"
    queue = await channel.declare_queue(name, durable=True, arguments=dict(WORKING_QUEUE_ARGS))
    try:
        yield queue
    finally:
        await queue.delete(if_unused=False, if_empty=False)
