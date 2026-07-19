"""S2 smoke: живой брокер принимает контракт очереди spec/40 и полный цикл publish→confirm→get→ack.

Доказывает, что SDK (пишется отдельно) есть обо что тестироваться: очередь с полным набором
аргументов объявляется, persistent-сообщение публикуется с подтверждением, доставляется, ack-ается,
очередь пустеет.
"""

import aio_pika


async def test_broker_smoke(channel, temp_quorum_queue):
    body = b"agentarium-smoke"

    # publish persistent-сообщения; канал с publisher confirms — await ждёт basic.ack брокера
    await channel.default_exchange.publish(
        aio_pika.Message(body=body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
        routing_key=temp_quorum_queue.name,
    )

    # получаем сообщение
    incoming = await temp_quorum_queue.get(timeout=10)
    assert incoming is not None
    assert incoming.body == body
    assert incoming.delivery_mode == aio_pika.DeliveryMode.PERSISTENT  # persistent долетел

    # ack — снимаем конверт с очереди
    await incoming.ack()

    # очередь пуста после ack (fail=False → при пустой очереди вернётся None, а не исключение)
    leftover = await temp_quorum_queue.get(fail=False, timeout=5)
    assert leftover is None, "очередь должна быть пуста после ack"
