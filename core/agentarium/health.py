"""Маленький asyncio HTTP-сервер здоровья агента. Контракт: spec/30 «Здоровье», spec/50.

Свойство платформы, не агента: SDK поднимает его в Agent.run() и отдаёт 200, когда health() истинно
(процесс жив и соединение с шиной живо), иначе 503. Так healthcheck образа агента — правда, а не
заглушка (docker бьёт `GET /health` из сгенерированного compose, spec/40 п.4).

Своё, а не FastAPI: ядро остаётся лёгким, зависимость шлюза в SDK не затягивается. Разбор HTTP —
минимальный: нам нужен только метод и путь первой строки, тело запроса игнорируется.
"""

import asyncio
from collections.abc import Callable


def _response(status: str, body: bytes = b"") -> bytes:
    head = (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode() + body


_OK = _response("200 OK", b'{"status":"ok"}')
_UNHEALTHY = _response("503 Service Unavailable", b'{"status":"unhealthy"}')
_NOT_FOUND = _response("404 Not Found")


class HealthServer:
    """HTTP-сервер `/health`: 200 при is_healthy(), иначе 503. Старт/стоп из цикла агента."""

    def __init__(self, is_healthy: Callable[[], bool], *, port: int, host: str = "0.0.0.0"):
        self._is_healthy = is_healthy
        self._host = host
        self._port = port
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port)

    @property
    def bound_port(self) -> int | None:
        """Фактический порт после старта (port=0 → эфемерный; нужно тестам)."""
        if self._server is None:
            return None
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()  # «GET /health HTTP/1.1» — больше не нужно
            parts = request_line.split(b" ")
            path = parts[1] if len(parts) >= 2 else b""
            if path.startswith(b"/health"):
                writer.write(_OK if self._is_healthy() else _UNHEALTHY)
            else:
                writer.write(_NOT_FOUND)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # клиент оборвал проверку — не наша забота, следующий health-пинг придёт снова
        finally:
            writer.close()
