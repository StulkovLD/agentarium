"""Чертёж системы: схема, загрузка, env-подстановка, валидация рубежа 1. Контракт: spec/40.

Владелец контракта конфигурации: каталог типов + реестр экземпляров + маршруты + вход/финалы.
Ядро НЕ импортирует код агентов (они в других образах) — валидация опирается только на каталог,
машиночитаемое лицо типов. Fail-fast: не одна дыра, а полный список всех дыр разом, по-человечески.
"""

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Зарезервированные имена и типы — carve-out валидации (spec/40).
GATEWAY = "gateway"  # вход/выход: в agents не объявляется, в routes допустим
TASK_FAILED = "task.failed"  # системный тип: эмитит SDK, не агент; маршрутизирован + в finals
PRIMITIVES = frozenset({"llm", "str", "dsn"})  # примитивы config_schema, других нет (spec/40)

_ENV_RE = re.compile(r"^env:(.+)$")


class TopologyError(Exception):
    """Чертёж не может стать системой. Несёт полный список дыр — fail-fast со всеми сразу."""

    def __init__(self, holes: list[str]):
        self.holes = holes
        body = "\n".join(f"  - {h}" for h in holes)
        super().__init__(f"чертёж не может стать системой — дыр: {len(holes)}\n{body}")


# --- модели каталога и чертежа ----------------------------------------------------------


class CatalogType(BaseModel):
    """Строка каталога: машиночитаемое лицо типа для ядра (spec/40)."""

    model_config = ConfigDict(extra="forbid")

    build: str = Field(min_length=1)
    consumes: list[str]
    produces: list[str]
    config_schema: dict[str, str] = Field(default_factory=dict)


class Instance(BaseModel):
    """Экземпляр в реестре: type из каталога + произвольный конфиг (проверяется схемой типа)."""

    model_config = ConfigDict(extra="allow")

    type: str

    @property
    def config(self) -> dict[str, Any]:
        return dict(self.__pydantic_extra__ or {})


class Embeddings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str


class Collection(BaseModel):
    """Общая коллекция знаний: владелец паспорта эмбеддингов (spec/45)."""

    model_config = ConfigDict(extra="forbid")

    source: str
    embeddings: Embeddings


class Topology(BaseModel):
    """Чертёж: единственный источник состава системы (spec/40)."""

    model_config = ConfigDict(extra="forbid")

    system: str
    entry: str
    agents: dict[str, Instance]
    collections: dict[str, Collection] = Field(default_factory=dict)
    routes: dict[str, list[str]]
    finals: dict[str, Literal["complete", "fail"]]


# --- загрузка каталога ------------------------------------------------------------------


def load_catalog(path: str | Path) -> dict[str, CatalogType]:
    """Прочитать agents/catalog.yaml. Fail-fast: битая строка каталога — со списком всех дыр."""
    raw = _read_yaml(path)
    if not isinstance(raw, dict):
        raise TopologyError([f"каталог {path} пуст или не YAML-объект"])
    catalog: dict[str, CatalogType] = {}
    holes: list[str] = []
    for type_name, body in raw.items():
        try:
            entry = CatalogType.model_validate(body)
        except ValidationError as exc:
            holes.extend(f"каталог, тип '{type_name}': {m}" for m in _humanize(exc))
            continue
        for field, prim in entry.config_schema.items():
            if prim not in PRIMITIVES:
                holes.append(
                    f"каталог, тип '{type_name}': поле '{field}' объявлено примитивом '{prim}', "
                    f"а их всего {sorted(PRIMITIVES)}"
                )
        catalog[type_name] = entry
    if holes:
        raise TopologyError(holes)
    return catalog


# --- загрузка чертежа -------------------------------------------------------------------


def load_topology(
    path: str | Path,
    catalog: dict[str, CatalogType],
    *,
    environ: dict[str, str] | None = None,
) -> Topology:
    """Прочитать чертёж, подставить env:, прогнать все валидации рубежа 1 (spec/40).

    Возврат — только если чертёж цел. Иначе TopologyError со списком ВСЕХ дыр разом.
    """
    import os

    env = os.environ if environ is None else environ
    raw = _read_yaml(path)
    if not isinstance(raw, dict):
        raise TopologyError([f"чертёж {path} пуст или не YAML-объект"])

    data, env_holes = _substitute_env(raw, env)

    try:
        topo = Topology.model_validate(data)
    except ValidationError as exc:
        raise TopologyError([*_humanize(exc), *env_holes]) from exc

    holes = _validate_rubezh_1(topo, catalog) + env_holes
    if holes:
        raise TopologyError(holes)
    return topo


# --- рубеж 1: все валидации из spec/40 --------------------------------------------------


def _validate_rubezh_1(topo: Topology, catalog: dict[str, CatalogType]) -> list[str]:
    holes: list[str] = []

    # gateway — зарезервированное имя: в agents не объявляется (carve-out spec/40)
    if GATEWAY in topo.agents:
        holes.append(
            f"'{GATEWAY}' — зарезервированное имя входа/выхода, его нельзя объявлять в agents"
        )

    # каждый type есть в каталоге; конфиг экземпляра проходит config_schema типа
    for name, inst in topo.agents.items():
        cat = catalog.get(inst.type)
        if cat is None:
            holes.append(
                f"экземпляр '{name}': тип '{inst.type}' не найден в каталоге agents/catalog.yaml"
            )
            continue
        holes += _validate_config(name, inst, cat, topo.collections)

    # маршрут не ссылается на несуществующий экземпляр (кроме gateway);
    # тип маршрутизирован только тем, чей consumes его принимает
    for msg_type, names in topo.routes.items():
        for name in names:
            if name == GATEWAY:
                continue
            inst = topo.agents.get(name)
            if inst is None:
                holes.append(
                    f"маршрут '{msg_type}' ведёт в '{name}', но такого экземпляра нет в agents "
                    f"(и это не gateway)"
                )
                continue
            cat = catalog.get(inst.type)
            if cat is not None and msg_type not in cat.consumes:
                holes.append(
                    f"маршрут '{msg_type}' ведёт в '{name}' (тип {inst.type}), "
                    f"но тот не принимает '{msg_type}' в consumes {cat.consumes}"
                )

    # каждый produces имеет маршрут — конверту есть куда лететь
    for name, inst in topo.agents.items():
        cat = catalog.get(inst.type)
        if cat is None:
            continue
        for produced in cat.produces:
            if produced not in topo.routes:
                holes.append(
                    f"экземпляр '{name}' производит '{produced}', но маршрута для '{produced}' "
                    f"нет — конверту некуда лететь"
                )

    # entry замкнут на маршрут (его payload-схема {text:str} — забота contract-модуля entry-типа,
    # ядро схем агентов не импортирует; проверяется рубежом 2 при старте агента и шлюзом)
    if topo.entry not in topo.routes:
        holes.append(f"entry '{topo.entry}' не замкнут на маршрут: нет routes['{topo.entry}']")

    # финалы двусторонне замкнуты на gateway
    gateway_types = {t for t, names in topo.routes.items() if GATEWAY in names}
    for final_type in topo.finals:
        if final_type not in gateway_types:
            holes.append(
                f"финал '{final_type}' не маршрутизирован в gateway "
                f"(routes['{final_type}'] = {topo.routes.get(final_type)})"
            )
    for gtype in gateway_types:
        if gtype not in topo.finals:
            holes.append(
                f"в gateway ведёт '{gtype}', но '{gtype}' нет в finals — "
                f"двусторонняя замкнутость нарушена"
            )

    # task.failed маршрутизирован и присутствует в finals
    if TASK_FAILED not in topo.routes:
        holes.append(f"'{TASK_FAILED}' не маршрутизирован (обычно {TASK_FAILED}: [gateway])")
    if TASK_FAILED not in topo.finals:
        holes.append(f"'{TASK_FAILED}' отсутствует в finals")

    # достижимый complete-финал ровно один — гонка двух финалов не проходит
    reachable = _reachable_types(topo, catalog)
    completes = [t for t, verdict in topo.finals.items() if verdict == "complete"]
    reachable_completes = [t for t in completes if t in reachable]
    if len(reachable_completes) != 1:
        holes.append(
            f"достижимый complete-финал должен быть ровно один, найдено "
            f"{len(reachable_completes)}: {reachable_completes} (все complete: {completes})"
        )

    return holes


def _validate_config(
    name: str,
    inst: Instance,
    cat: CatalogType,
    collections: dict[str, Collection],
) -> list[str]:
    holes: list[str] = []
    config = inst.config
    schema = cat.config_schema

    for field, prim in schema.items():
        if field not in config:
            holes.append(
                f"экземпляр '{name}' (тип {inst.type}): не задано обязательное поле "
                f"конфига '{field}' ({prim})"
            )
            continue
        err = _check_primitive(prim, config[field])
        if err is not None:
            holes.append(f"экземпляр '{name}', поле '{field}' ({prim}): {err}")

    for field in config:
        if field not in schema:
            holes.append(
                f"экземпляр '{name}': поле '{field}' не описано в config_schema типа "
                f"'{inst.type}' — вероятна опечатка"
            )

    # у каждой упомянутой коллекции есть блок в collections (ссылка — по имени поля 'collection')
    ref = config.get("collection")
    if isinstance(ref, str) and ref and ref not in collections:
        holes.append(
            f"экземпляр '{name}' ссылается на коллекцию '{ref}', которой нет в collections"
        )
    return holes


def _check_primitive(prim: str, value: Any) -> str | None:
    """Примитивы config_schema (spec/40). None — годно, строка — человеческая причина отказа."""
    if prim == "str":
        if not (isinstance(value, str) and value.strip()):
            return "должно быть непустой строкой"
        return None
    if prim == "dsn":
        if isinstance(value, str) and value.startswith("env:"):
            return None  # неразрешённый env: уже отмечен отдельной дырой
        if not (isinstance(value, str) and value.strip()):
            return "должно быть строкой-DSN (допустимо env:ИМЯ)"
        return None
    if prim == "llm":
        return _check_llm(value)
    return f"неизвестный примитив '{prim}'"  # недостижимо: каталог проверен на PRIMITIVES


def _check_llm(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "должно быть llm-блоком {provider: str, name: str, base_url?: str}"
    problems: list[str] = []
    for key in ("provider", "name"):
        if not (isinstance(value.get(key), str) and value[key].strip()):
            problems.append(f"нет непустого '{key}'")
    if "base_url" in value and not isinstance(value["base_url"], str):
        problems.append("'base_url' не строка")
    extra = set(value) - {"provider", "name", "base_url"}
    if extra:
        problems.append(f"лишние поля {sorted(extra)}")
    return ("llm-блок кривой: " + ", ".join(problems)) if problems else None


def _reachable_types(topo: Topology, catalog: dict[str, CatalogType]) -> set[str]:
    """Типы, достижимые от entry по цепочке consumes→produces. Опора для «complete ровно один»."""
    reachable: set[str] = set()
    frontier = [topo.entry]
    while frontier:
        msg_type = frontier.pop()
        if msg_type in reachable:
            continue
        reachable.add(msg_type)
        for name in topo.routes.get(msg_type, []):
            if name == GATEWAY:
                continue
            inst = topo.agents.get(name)
            if inst is None:
                continue
            cat = catalog.get(inst.type)
            if cat is None or msg_type not in cat.consumes:
                continue
            frontier.extend(p for p in cat.produces if p not in reachable)
    return reachable


# --- вспомогательное --------------------------------------------------------------------


def _read_yaml(path: str | Path) -> Any:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise TopologyError([f"файл {path} не читается: {exc}"]) from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TopologyError([f"файл {path} не читается как YAML: {exc}"]) from exc


def _substitute_env(data: Any, env: dict[str, str]) -> tuple[Any, list[str]]:
    """Рекурсивно заменить строки 'env:ИМЯ' значениями окружения. Нет переменной — дыра."""
    holes: list[str] = []

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            match = _ENV_RE.match(node)
            if match:
                var = match.group(1)
                if var in env:
                    return env[var]
                holes.append(f"'{node}' ссылается на переменную окружения {var}, но её нет")
                return node
        return node

    return walk(data), holes


def _humanize(exc: ValidationError) -> list[str]:
    """Ошибки Pydantic → человеческие строки: путь + причина."""
    out: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<корень>"
        out.append(f"поле '{loc}': {err['msg']}")
    return out
