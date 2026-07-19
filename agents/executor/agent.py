"""Тип executor: регламент + заявка → план работ + реальные проверки против target-db. Спека: 55.

Мозги — LangGraph-граф (executor.graph) на GigaChat-2-Max: план → выбор проверок → исполнение →
вердикт, с настоящей развилкой. Безопасность по-взрослому (аудитория ревью — DBA):
- LLM не пишет SQL — выбирает проверки из allowlist (executor.tools);
- подключение к target-db — отдельная read-only роль readonly_executor (TARGET_DB_DSN), транзакции
  READ ONLY, statement_timeout выставлен. Сырого SQL от LLM не существует.

Пул asyncpg, LLM и граф собираются лениво на первом handle (async/тянут extra `brains`): импорт
модуля для реестра схем и сверки каталога остаётся дешёвым и без мозгов (spec/30).
"""

import os

import asyncpg
from agentarium.agent import Agent
from agentarium.envelope import Envelope, Reply

from agents.executor import (
    contract,  # noqa: F401 — регистрирует схему plan.ready (spec/30)
    tools,
)
from agents.rag.contract import KnowledgeFoundPayload

STATEMENT_TIMEOUT_MS = "5000"  # верхний предел одной проверки — target-db не держат вечно


class ExecutorAgent(Agent):
    consumes = ["knowledge.found"]
    produces = ["plan.ready"]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._pool: asyncpg.Pool | None = None
        self._graph = None

    async def _ensure(self):
        """Лениво поднять пул к target-db (read-only + statement_timeout), LLM и граф."""
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self.config["target_db"],
                min_size=1,
                max_size=4,
                server_settings={
                    "statement_timeout": STATEMENT_TIMEOUT_MS,
                    "default_transaction_read_only": "on",  # оборона в глубину поверх READ ONLY tx
                },
            )
        if self._graph is None:
            from langchain_gigachat import GigaChat

            from agents.executor.graph import build_graph

            model = self.config["model"]
            common = {
                "credentials": os.environ["GIGACHAT_CREDENTIALS"],
                "scope": os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
                "model": model["name"],
            }
            base_url = model.get("base_url") or os.environ.get("GIGACHAT_BASE_URL")
            if base_url:
                common["base_url"] = base_url
            ca_bundle = os.environ.get("GIGACHAT_CA_BUNDLE_FILE")
            if ca_bundle:
                common["ca_bundle_file"] = ca_bundle
            self._graph = build_graph(GigaChat(**common), self._run_check)

    async def _run_check(self, name: str, args: dict) -> list[dict]:
        """Исполнить проверку allowlist под READ ONLY транзакцией (только чтение, $N)."""
        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            return await tools.run_check(conn, name, args)

    async def handle(self, envelope: Envelope) -> Reply | None:
        payload = KnowledgeFoundPayload.model_validate(envelope.payload)
        await self._ensure()
        final = await self._graph.ainvoke(
            {
                "request": payload.request.model_dump(),
                "chunks": [c.model_dump() for c in payload.chunks],
                "plan": [],
                "selected": [],
                "checks": [],
                "verdict": "",
                "consistent": True,
                "replans": 0,
            }
        )
        sources = sorted({c.source for c in payload.chunks})
        return Reply(
            type="plan.ready",
            payload={
                # request доезжает до финала нетронутым (труба на всех типах, spec/40, spec/55)
                "request": envelope.payload["request"],
                "plan": final["plan"],
                "checks": final["checks"],
                "verdict": final["verdict"],
                "sources": sources,
            },
        )
