"""LangGraph-граф executor: план → выбор проверок → исполнение → вердикт, с настоящей развилкой.

Спека 55: граф ветвится по-настоящему — узел вердикта сравнивает факты проверок с планом; расходятся
(версия уже целевая, пользователь не существует, прав больше ожидаемого) → пересборка плана с
фактами; сходятся → финализация. Это та развилка, ради которой executor держит LangGraph.

LLM НЕ пишет SQL: узлы плана/пересборки выбирают имена проверок из allowlist (tools.ALLOWLIST) с
типизированными аргументами; фиксированный SQL и исполнение — детерминированный код (tools.py).

Модуль импортируется лениво из agent.py (тянет langgraph = extra `brains`): в реестр схем и сверку
каталога он не попадает, потому CI-юниты его не грузят.
"""

from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from agents.executor import tools

MAX_REPLANS = 2  # предел пересборок: развилка есть, вечного цикла план↔факты нет

# Имя проверки, ограниченное allowlist — LLM не может выбрать ничего вне tools.ALLOWLIST.
CheckName = Enum("CheckName", {name: name for name in tools.ALLOWLIST}, type=str)

CheckRunner = Callable[[str, dict[str, Any]], Awaitable[list[dict[str, Any]]]]


class CheckSelection(BaseModel):
    """Одна проверка из allowlist. Аргументы — плоскими полями: заполни те, что нужны проверке."""

    model_config = ConfigDict(extra="forbid")

    name: CheckName = Field(description="Имя проверки из allowlist")
    user: str | None = Field(
        default=None, description="Имя пользователя — для user_roles и user_privileges"
    )
    database: str | None = Field(
        default=None, description="Имя базы — для user_privileges и db_size"
    )


class PlanOutput(BaseModel):
    """Структурированный выход планировщика: шаги работ + выбранные проверки (не SQL)."""

    model_config = ConfigDict(extra="forbid")

    plan: list[str] = Field(description="Шаги плана работ по регламенту")
    checks: list[CheckSelection] = Field(description="Проверки allowlist к выполнению")


class VerdictOutput(BaseModel):
    """Вердикт по заявке: сопоставление плана с фактами выполненных проверок."""

    model_config = ConfigDict(extra="forbid")

    verdict: str = Field(description="Вердикт: вывод по заявке с учётом фактов проверок")
    consistent: bool = Field(description="Факты согласуются с планом (true) или расходятся (false)")


class ExecState(TypedDict):
    request: dict[str, Any]
    chunks: list[dict[str, Any]]
    plan: list[str]
    selected: list[dict[str, Any]]
    checks: list[dict[str, Any]]
    verdict: str
    consistent: bool
    replans: int


def _regulations(chunks: list[dict[str, Any]]) -> str:
    if not chunks:
        return "(регламентов не найдено — действуй по общим практикам DBA и отметь это в плане)"
    return "\n\n".join(f"[{c['source']} · {c['heading']}]\n{c['text']}" for c in chunks)


_PLAN_SYS = (
    "Ты — исполнитель заявок DBA. По заявке и найденным регламентам составь план работ и выбери "
    "проверки, подтверждающие исходное состояние target-db. Проверки — ТОЛЬКО из allowlist "
    "(ниже), SQL писать нельзя. Аргументы бери из сущностей заявки.\n"
    "ОБЯЗАТЕЛЬНО заполни поле checks минимум одной проверкой — пустой список запрещён: без фактов "
    "заявку не закрыть. Для check_access — user_roles и user_privileges; для update_db_version "
    "— pg_version; всегда добавляй проверки под интент.\n\nAllowlist проверок:\n"
    + tools.catalog_description()
)

_VERDICT_SYS = (
    "Ты — исполнитель заявок DBA. Сравни план с фактами проверок target-db и вынеси вердикт. "
    "Если факты расходятся с планом (версия уже целевая, пользователь не существует, прав больше "
    "ожидаемого) — consistent=false, иначе true."
)

_REPLAN_SYS = (
    "План разошёлся с фактами target-db. Пересобери план с учётом фактов и, если нужно, выбери "
    "другие проверки из allowlist (SQL писать нельзя).\n\nAllowlist проверок:\n"
    + tools.catalog_description()
)


def _request_brief(state: ExecState) -> str:
    r = state["request"]
    return f"Заявка: {r['text']}\nИнтент: {r['intent']}\nСущности: {r['entities']}"


def build_graph(llm: Any, run_check: CheckRunner):
    """Скомпилировать граф на chat-LLM и раннере проверок (asyncpg под READ ONLY, agent.py)."""
    planner = llm.with_structured_output(PlanOutput)
    judge = llm.with_structured_output(VerdictOutput)

    async def plan_node(state: ExecState) -> dict[str, Any]:
        out: PlanOutput = await planner.ainvoke(
            [
                ("system", _PLAN_SYS),
                ("human", f"{_request_brief(state)}\n\nРегламенты:\n{_regulations(state['chunks'])}"),  # noqa: E501
            ]
        )
        return {"plan": out.plan, "selected": [_selection(c) for c in out.checks]}

    async def execute_node(state: ExecState) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        for sel in state["selected"]:
            checks.append(await _execute_one(run_check, sel))
        return {"checks": checks}

    async def verdict_node(state: ExecState) -> dict[str, Any]:
        out: VerdictOutput = await judge.ainvoke(
            [
                ("system", _VERDICT_SYS),
                (
                    "human",
                    f"{_request_brief(state)}\n\nПлан:\n{state['plan']}\n\nФакты:\n{state['checks']}",  # noqa: E501
                ),
            ]
        )
        return {"verdict": out.verdict, "consistent": out.consistent}

    async def replan_node(state: ExecState) -> dict[str, Any]:
        out: PlanOutput = await planner.ainvoke(
            [
                ("system", _REPLAN_SYS),
                (
                    "human",
                    f"{_request_brief(state)}\n\nПрежний план:\n{state['plan']}\n\n"
                    f"Факты проверок:\n{state['checks']}",
                ),
            ]
        )
        return {
            "plan": out.plan,
            "selected": [_selection(c) for c in out.checks],
            "replans": state["replans"] + 1,
        }

    def route_after_verdict(state: ExecState) -> str:
        if not state["consistent"] and state["replans"] < MAX_REPLANS:
            return "replan"
        return END

    graph = StateGraph(ExecState)
    graph.add_node("plan", plan_node)
    graph.add_node("execute", execute_node)
    graph.add_node("verdict", verdict_node)
    graph.add_node("replan", replan_node)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "execute")
    graph.add_edge("execute", "verdict")
    graph.add_conditional_edges("verdict", route_after_verdict, {"replan": "replan", END: END})
    graph.add_edge("replan", "execute")
    return graph.compile()


def _selection(check: CheckSelection) -> dict[str, Any]:
    name = check.name.value if isinstance(check.name, Enum) else check.name
    # пустую строку модель шлёт для нерелевантных полей — отсекаем как отсутствие (не только None)
    args = {k: v for k, v in (("user", check.user), ("database", check.database)) if v}
    return {"name": name, "args": args}


async def _execute_one(run_check: CheckRunner, sel: dict[str, Any]) -> dict[str, Any]:
    """Выполнить одну проверку. Ошибка — в result, не крах заявки: и неверные аргументы, и
    операционный отказ БД (нет такой базы, недоступна) это ФАКТ для вердикта, а не баг агента.
    Факт питает развилку пересборки; сырого SQL нет — ронять весь handle нечему."""
    try:
        result: Any = await run_check(sel["name"], sel["args"])
    except Exception as exc:  # noqa: BLE001 — результат проверки против живой БД, включая её отказ
        result = {"error": f"{type(exc).__name__}: {exc}"}
    return {"name": sel["name"], "args": sel["args"], "result": result}
