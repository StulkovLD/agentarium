"""Юнит: HTTP /health-сервер SDK — 200 при health() истинном, иначе 503. Спека: 30, 50.

Сервер поднимается в Agent.run() и делает сгенерированный healthcheck образа правдой (spec/40 п.4).
Здесь — сам сервер на эфемерном порту, без шины и агента: контракт ответов и путей.
"""

import httpx
from agentarium.health import HealthServer


async def test_health_reports_ok_then_unhealthy_then_404():
    alive = {"ok": True}
    server = HealthServer(lambda: alive["ok"], port=0, host="127.0.0.1")
    await server.start()
    try:
        port = server.bound_port
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            ok = await client.get("/health")
            assert ok.status_code == 200
            assert ok.json() == {"status": "ok"}

            alive["ok"] = False  # соединение с шиной «умерло» — health() ложно
            sick = await client.get("/health")
            assert sick.status_code == 503

            other = await client.get("/metrics")
            assert other.status_code == 404
    finally:
        await server.stop()


async def test_stop_closes_server():
    server = HealthServer(lambda: True, port=0, host="127.0.0.1")
    await server.start()
    port = server.bound_port
    await server.stop()
    assert server.bound_port is None
    with httpx.Client() as client:
        try:
            client.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
            raise AssertionError("сервер должен быть закрыт после stop()")
        except httpx.ConnectError:
            pass  # порт закрыт — ожидаемо
