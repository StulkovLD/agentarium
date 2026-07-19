"""E2E-live: три демо-заявки spec/55 против ПОДНЯТОЙ системы на живом GigaChat. `make test-e2e`.

Чёрный ящик: только HTTP к шлюзу (localhost:8000), без импорта мозгов — потому модуль собирается и в
CI, но пропускается, пока не задан AGENTARIUM_E2E (его ставит `make test-e2e`). Живого дубля LLM нет
(spec/05): либо живой GigaChat поверх make up, либо честный skip. Порог/точный текст ответа LLM не
проверяем — проверяем маршрут и форму финала (parser → knowledge → executor, spec/55).
"""

import os

import httpx
import pytest

GATEWAY = os.environ.get("AGENTARIUM_GATEWAY_URL", "http://localhost:8000")
POLL_TIMEOUT_S = 120.0  # исполнитель ходит в LLM несколько раз (план → проверки → вердикт)

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENTARIUM_E2E"),
    reason="e2e-live: задай AGENTARIUM_E2E=1 и подними систему (make up) — `make test-e2e`",
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


def _submit(client: httpx.Client, text: str) -> str:
    resp = client.post("/requests", json={"text": text})
    assert resp.status_code == 202, resp.text
    return resp.json()["trace_id"]


@pytest.fixture
def client():
    with httpx.Client(base_url=GATEWAY, timeout=30.0) as c:
        yield c


def test_check_access_request_reaches_plan_ready(client):
    """Заявка №1: проверка доступов → plan.ready с проверками против target-db (spec/55)."""
    trace = _submit(client, "Проверь, какие доступы у пользователя ivanov в базе billing на проде")
    result = _poll(client, trace)
    assert result["status"] == "done"
    plan = result["result"]
    assert plan["request"]["intent"] == "check_access"
    assert plan["plan"] and isinstance(plan["plan"], list)
    assert plan["checks"] and any(c["name"] == "user_roles" for c in plan["checks"])
    assert plan["verdict"]


def test_update_version_request_reaches_plan_ready(client):
    """Заявка №2: обновление версии → план по регламенту + проверка текущей версии (spec/55)."""
    trace = _submit(client, "Подготовь план обновления PostgreSQL на db-billing с 16.1 до 16.3")
    result = _poll(client, trace)
    assert result["status"] == "done"
    plan = result["result"]
    assert plan["request"]["intent"] == "update_db_version"
    assert plan["plan"]
    assert any(c["name"] == "pg_version" for c in plan["checks"])


def test_non_request_is_rejected_and_observable(client):
    """Заявка №3: не-заявка → request.rejected → failed, путь виден в трейсе (spec/55)."""
    trace = _submit(client, "Что за ерунда:))")
    result = _poll(client, trace)
    assert result["status"] == "failed"
    assert result["result"] is not None  # причина отказа записана
