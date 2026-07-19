# Образ типа auditor. Контракт: spec/30 (SDK-разъём, бандл contract-модулей), spec/55 (мозги агента).
# Build-контекст — КОРЕНЬ репо (нужны core/agentarium + agents/ всех типов): собирать так, как эмитит
# генератор compose (tools/gen_compose): build.context=., build.dockerfile=agents/auditor/Dockerfile.
FROM python:3.12-slim

# uv — быстрый воспроизводимый установщик (spec/05); версия запинена образом-донором.
COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Зависимости отдельным слоем; core/agentarium — до sync (проект agentarium собирается из него).
# --extra brains: langchain-gigachat живёт ТОЛЬКО в образах агентов, не в ядре (spec/00). auditor
# берёт из него и Embeddings (поиск по incidents), и chat-LLM (сверка «на этих граблях уже стояли»).
# TLS к GigaChat: цепочка НУЦ Минцифры вшита в пакет gigachat (зависимость langchain-gigachat), spec/05.
COPY pyproject.toml uv.lock ./
COPY core/agentarium ./core/agentarium
RUN uv sync --frozen --no-dev --extra brains

# Код всех типов + бандл contract-модулей каталога (spec/30). Мозги других типов — ленивы, не тянутся.
COPY agents ./agents

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    AGENTARIUM_CATALOG=/app/agents/catalog.yaml

EXPOSE 8000
# SDK поднимает /health на 8000 в Agent.run(); имя экземпляра и чертёж — из env (compose, spec/40).
CMD ["python", "-m", "agentarium", "run"]
