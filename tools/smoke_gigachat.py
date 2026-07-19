"""Ранний live-smoke GigaChat: авторизация → чат-вызов → эмбеддинг. Контракт: spec/70, слайс S6.

Зачем в S6, а не в S7: TLS (цепочка НУЦ Минцифры), формат ключа и квоты ловим здесь, до того как
на них наткнутся живые агенты демо. Запуск (нужен extra `smoke` = langchain-gigachat):

    uv run --extra smoke python -m tools.smoke_gigachat

Ключа нет или уцелел плейсхолдер — честный отказ с текстом «положи ключ в .env», код возврата 1.
Мозги (langchain-gigachat) импортируются лениво — путь отказа работает и без установленного extra.
"""

import os
import sys

PLACEHOLDER_MARKER = "положи-сюда"  # конвенция env.example (см. secrets.check_secrets шлюза)


def _require_key() -> str:
    """GIGACHAT_CREDENTIALS или честный отказ. Пусто/плейсхолдер → «положи ключ в .env», exit 1."""
    key = os.environ.get("GIGACHAT_CREDENTIALS", "").strip()
    if not key or PLACEHOLDER_MARKER in key:
        print(
            "GigaChat-ключ не задан: положи ключ в .env "
            "(GIGACHAT_CREDENTIALS=<authorization key>).\n"
            "Скопируй env.example в .env и впиши ключ из "
            "https://developers.sber.ru/gigachat, затем:\n"
            "    uv run --extra smoke python -m tools.smoke_gigachat",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return key


def main() -> None:
    key = _require_key()
    # Лениво: путь отказа выше не тянет LangChain — smoke без extra всё равно честно откажет.
    from langchain_gigachat import GigaChat, GigaChatEmbeddings

    scope = os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
    base_url = os.environ.get("GIGACHAT_BASE_URL")
    ca_bundle = os.environ.get("GIGACHAT_CA_BUNDLE_FILE")
    common = {"credentials": key, "scope": scope}
    if base_url:
        common["base_url"] = base_url
    if ca_bundle:
        common["ca_bundle_file"] = ca_bundle

    chat = GigaChat(**common)
    answer = chat.invoke("Ответь одним словом: работает?")
    print(f"чат-вызов ок: {str(answer.content)[:80]}")

    embeddings = GigaChatEmbeddings(**common)
    vector = embeddings.embed_query("проверка эмбеддинга")
    print(f"эмбеддинг ок: размерность {len(vector)}")

    print("smoke GigaChat зелёный: авторизация, чат и эмбеддинг работают.")


if __name__ == "__main__":
    main()
