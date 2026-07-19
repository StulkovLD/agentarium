# Образ типа echo — тривиальный, без мозгов. Контракт: spec/30 (SDK-разъём, бандл contract-модулей).
# Build-контекст — КОРЕНЬ репо (нужны core/agentarium + agents/ всех типов): как эмитит gen_compose
# (build.context=., build.dockerfile=agents/echo/Dockerfile). brains не ставится — echo без LLM;
# мозги dba-типов, чьи agent.py грузятся при регистрации схем, импортируются лениво и тут не нужны.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
COPY core/agentarium ./core/agentarium
RUN uv sync --frozen --no-dev

COPY agents ./agents

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    AGENTARIUM_CATALOG=/app/agents/catalog.yaml

EXPOSE 8000
CMD ["python", "-m", "agentarium", "run"]
