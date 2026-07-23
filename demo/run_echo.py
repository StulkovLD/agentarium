"""Прогон технического демо echo-pair: текст проходит цепочку echo → reverse без единого LLM.

Чёрный ящик поверх того же HTTP-контракта шлюза, что и run_demo (spec/40): POST → poll GET.
Доказательство «агентом может быть любая логика»: та же шина, те же гарантии доставки, тот же
трейсинг — а мозги агентов здесь тривиальный код. Ключ GigaChat не нужен.
"""

import sys
import time

import httpx

GATEWAY = "http://localhost:8000"
POLL_TIMEOUT_S = 30.0  # без LLM цепочка отрабатывает за доли секунды

TEXT = "Привет, шина!"

_LINE = "─" * 78


def main() -> int:
    gateway = sys.argv[1] if len(sys.argv) > 1 else GATEWAY
    print(f"agentarium demo (echo-pair, без LLM) · шлюз {gateway}")
    with httpx.Client(base_url=gateway, timeout=10.0) as client:
        resp = client.post("/requests", json={"text": TEXT})
        if resp.status_code != 202:
            print(f"ОШИБКА POST: {resp.status_code} {resp.text}")
            return 1
        trace_id = resp.json()["trace_id"]
        deadline = time.monotonic() + POLL_TIMEOUT_S
        body = {"status": "timeout"}
        while time.monotonic() < deadline:
            body = client.get(f"/requests/{trace_id}").json()
            if body["status"] in ("done", "failed"):
                break
            time.sleep(0.3)
    print(_LINE)
    print(f"Заявка:    {TEXT}")
    if body["status"] == "done":
        result = body.get("result") or {}
        print("  путь:    gateway → echo → reverse → gateway")
        print(f"  ответ:   {result.get('text')}")
        print(f"  trace:   {trace_id}  (путь виден в Jaeger — как у любой заявки)")
    else:
        print(f"  СТАТУС:  {body['status']}")
        return 1
    print(_LINE)
    print("демо завершено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
