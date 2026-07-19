# Образ шлюза agentarium. Контракт: spec/40 (HTTP + консюмеры), spec/30 (бандл contract-модулей).
# Build-контекст — КОРЕНЬ репо (нужны core/agentarium, agents/ и каталог): собирать так, как эмитит
# генератор compose (tools/gen_compose.py): build.context=., build.dockerfile=core/gateway/Dockerfile.
FROM python:3.12-slim

# uv — быстрый и воспроизводимый установщик (spec/05); версия запинена образом-донором.
COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Зависимости — отдельным слоем: правка кода шлюза не рушит кэш установки.
# core/agentarium копируется до sync: проект (agentarium) собирается из него editable-инсталлом.
COPY pyproject.toml uv.lock ./
COPY core/agentarium ./core/agentarium
RUN uv sync --frozen --no-dev   # main-зависимости + agentarium; dev и extra `smoke` (LangChain) — мимо

# Код шлюза, каталог и contract-модули ВСЕХ типов (spec/30: бандл в каждом образе, включая шлюз),
# env.example — для fail-fast секретов на старте (secrets.check_secrets).
COPY core/gateway ./gateway
COPY agents ./agents
COPY infra/certs ./infra/certs
COPY env.example ./env.example

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    AGENTARIUM_CATALOG=/app/agents/catalog.yaml \
    AGENTARIUM_ENV_EXAMPLE=/app/env.example

EXPOSE 8000
# uvicorn из .venv (PATH выше); gateway.app:app сам поднимает шину/пул/консюмеры в lifespan.
CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
