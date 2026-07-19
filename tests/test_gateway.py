"""Юниты шлюза без живых сервисов: fail-fast секретов, отказ старта, извлечение текста (spec/40).

Извлечение текста и проверка секретов — детерминированный код, тестируется без шины и БД.
Отказ старта проверяется через lifespan приложения: секрет-плейсхолдер валит подъём до соединений.
"""

import pytest
from gateway import SecretsError, build, check_secrets, extract_text
from gateway.secrets import PLACEHOLDER_MARKER

PLACEHOLDER = f"{PLACEHOLDER_MARKER}-authorization-key"


# --- fail-fast секретов ------------------------------------------------------------------


def _env_example(tmp_path, body: str):
    path = tmp_path / "env.example"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_check_secrets_refuses_surviving_placeholder(tmp_path):
    example = _env_example(tmp_path, f"GIGACHAT_CREDENTIALS={PLACEHOLDER}\nRABBITMQ_URL=amqp://x\n")
    with pytest.raises(SecretsError, match="GIGACHAT_CREDENTIALS"):
        check_secrets(
            environ={"GIGACHAT_CREDENTIALS": PLACEHOLDER}, env_example_path=example
        )


def test_check_secrets_passes_real_value_and_unset(tmp_path):
    example = _env_example(tmp_path, f"GIGACHAT_CREDENTIALS={PLACEHOLDER}\n")
    # реальное значение — тихо проходит
    check_secrets(environ={"GIGACHAT_CREDENTIALS": "real-key-abc123"}, env_example_path=example)
    # ключ вообще не задан — тоже проходит: конфиг без LLM (echo-цепочка) секрета не требует
    check_secrets(environ={}, env_example_path=example)


def test_check_secrets_ignores_non_placeholder_defaults(tmp_path):
    # реальные дефолты env.example (без маркера) проверке не подлежат — совпадение это норма
    example = _env_example(tmp_path, "RABBITMQ_URL=amqp://agentarium:agentarium@rabbitmq:5672/\n")
    check_secrets(
        environ={"RABBITMQ_URL": "amqp://agentarium:agentarium@rabbitmq:5672/"},
        env_example_path=example,
    )


async def test_startup_refuses_on_placeholder(monkeypatch):
    """Плейсхолдер в живом окружении → подъём приложения валится SecretsError до соединений."""
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", PLACEHOLDER)
    app = build()
    with pytest.raises(SecretsError):
        async with app.router.lifespan_context(app):
            pass  # до сюда не дойдём: check_secrets стоит первым в lifespan


# --- извлечение текста детерминированным порядком (spec/40) -------------------------------


def test_extract_text_prefers_nested_request():
    # payload.request.text — первым в порядке (продуктовый тип несёт исходную заявку вложенной)
    assert extract_text({"request": {"text": "исходный"}, "text": "иной"}) == "исходный"


def test_extract_text_falls_back_to_plain_text():
    assert extract_text({"text": "плоский"}) == "плоский"


def test_extract_text_dives_into_original_payload_for_task_failed():
    failed = {
        "failed_type": "request.parsed",
        "reason": "boom",
        "attempts": 4,
        "original_payload": {"request": {"text": "внутри"}},
    }
    assert extract_text(failed) == "внутри"
    assert extract_text({"original_payload": {"text": "внутри2"}}) == "внутри2"


def test_extract_text_returns_none_when_absent():
    assert extract_text({"foo": 1}) is None
    assert extract_text({"request": {"no_text": 1}}) is None
