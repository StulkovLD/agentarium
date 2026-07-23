"""Прогон трёх демо-заявок spec/55 через живую систему: заявка → путь → ответ. Контракт: spec/55.

Чёрный ящик поверх HTTP-контракта шлюза (spec/40): POST /requests → poll GET /requests/{trace_id}.
Красивый вывод показывает путь заявки (parser → knowledge → executor) и финал: план + проверки
против target-db + вердикт, либо честный отказ. Требует поднятой системы и живого GigaChat.
"""

import sys
import time

import httpx

GATEWAY = "http://localhost:8000"
POLL_TIMEOUT_S = 180.0  # исполнитель ходит в LLM несколько раз (план → проверки → вердикт)

# Три демо-заявки дословно из spec/55 (включая отказ №3 — путь виден в трейсе как нормальный).
REQUESTS = [
    "Проверь, какие доступы у пользователя ivanov в базе billing на проде",
    "Подготовь план обновления PostgreSQL на db-billing с 16.1 до 16.3",
    "Что за ерунда:))",
]

_LINE = "─" * 78


def _poll(client: httpx.Client, trace_id: str) -> dict:
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        body = client.get(f"/requests/{trace_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(1.0)
    return {"status": "timeout", "result": None}


def _render(text: str, body: dict) -> None:
    print(_LINE)
    print(f"Заявка: {text}")
    status = body["status"]
    result = body.get("result") or {}
    if status == "done":
        req = result.get("request", {})
        audit = result.get("audit")  # конфигурация B добавляет блок audit (spec/55)
        path = "parser → knowledge → executor" + (" → auditor" if audit is not None else "")
        print(f"  путь:    {path}   (интент: {req.get('intent')})")
        print(f"  план:    {result.get('plan')}")
        for check in result.get("checks", []):
            print(f"  проверка {check['name']}({check['args']}) → {check['result']}")
        print(f"  источники: {result.get('sources')}")
        print(f"  ВЕРДИКТ:  {result.get('verdict')}")
        if audit is not None:  # обогащённый финал: замечания «на этих граблях уже стояли»
            warnings = audit.get("warnings") or []
            print(f"  АУДИТ:    {'; '.join(warnings) if warnings else 'план граблей не повторяет'}")
    elif status == "failed":
        reason = result.get("reason", result)
        print("  путь:    parser → gateway   (отказ — не заявка / ошибка)")
        print(f"  ОТКАЗ:   {reason}")
    else:
        print(f"  СТАТУС:  {status} — заявка не финализировалась за {POLL_TIMEOUT_S:.0f}s")


def main() -> int:
    gateway = sys.argv[1] if len(sys.argv) > 1 else GATEWAY
    print(f"agentarium demo · шлюз {gateway}")
    failures = 0
    with httpx.Client(base_url=gateway, timeout=30.0) as client:
        for text in REQUESTS:
            resp = client.post("/requests", json={"text": text})
            if resp.status_code != 202:
                print(_LINE)
                print(f"Заявка: {text}\n  ОШИБКА POST: {resp.status_code} {resp.text}")
                failures += 1
                continue
            trace_id = resp.json()["trace_id"]
            body = _poll(client, trace_id)
            _render(text, body)
            if body["status"] == "timeout":
                failures += 1
            # Смерть конверта в dlq — провал прогона, в отличие от честного отказа parser'а:
            # без этого демо маскирует беду («failed» бывает и легитимным — request.rejected).
            reason = (body.get("result") or {}).get("reason", "")
            if isinstance(reason, str) and reason.startswith("конверт мёртв"):
                failures += 1
    print(_LINE)
    print("демо завершено" if not failures else f"демо завершено с ошибками: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
