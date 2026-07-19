"""E2E-live конфигурации B: заявка доходит до audit.done с блоком аудита (`make test-e2e`).

Чёрный ящик: только HTTP к шлюзу (localhost:8000), без импорта мозгов — модуль собирается и в CI, но
пропускается, пока не задан AGENTARIUM_E2E (его ставит `make test-e2e`) и не поднята расширенная
конфигурация (`make up CONFIG=dba-extended`). Живого дубля GigaChat нет (spec/05): либо живой ключ
поверх поднятой системы, либо честный skip. Проверяем маршрут и форму финала конфигурации B: заявка
идёт parser → knowledge → executor → auditor, финал audit.done несёт блок audit.warnings (spec/55).
"""

import os

import httpx
import pytest

GATEWAY = os.environ.get("AGENTARIUM_GATEWAY_URL", "http://localhost:8000")
POLL_TIMEOUT_S = 150.0  # цепочка длиннее базовой на аудитора (ещё вектор + LLM-сверка)

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENTARIUM_E2E"),
    reason=(
        "e2e-live конфигурации B: задай AGENTARIUM_E2E=1 и подними расширенную систему "
        "(make up CONFIG=dba-extended) — `make test-e2e`"
    ),
)


def _poll(client: httpx.Client, trace_id: str) -> dict:
    import time

    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        resp = client.get(f"/requests/{trace_id}")
        resp.raise_for_status()
        body = resp.json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(1.0)
    raise AssertionError(f"заявка {trace_id} не финализировалась за {POLL_TIMEOUT_S}s")


@pytest.fixture
def client():
    with httpx.Client(base_url=GATEWAY, timeout=30.0) as c:
        yield c


def test_request_reaches_audit_done_with_audit_block(client):
    """Заявка №2 spec/55 на конфигурации B: финал — audit.done с блоком audit.warnings (spec/55)."""
    resp = client.post(
        "/requests",
        json={"text": "Подготовь план обновления PostgreSQL на db-billing с 16.1 до 16.3"},
    )
    assert resp.status_code == 202, resp.text
    result = _poll(client, resp.json()["trace_id"])
    assert result["status"] == "done"
    final = result["result"]
    # финал конфигурации B — audit.done: план + проверки прокинуты, добавлен блок аудита (spec/55)
    assert final["request"]["intent"] == "update_db_version"
    assert final["plan"] and isinstance(final["plan"], list)
    assert "audit" in final, "финал конфигурации B обязан нести блок audit (spec/55)"
    assert isinstance(final["audit"]["warnings"], list)
