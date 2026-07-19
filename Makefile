# Команды платформы. Владелец перечня — CLAUDE.md (таблица «Команды»).
CONFIG ?= dba-base
COLLECTION ?= all

# .env — канал секретов и адресов. Загружаем в окружение хоста для команд, что бегут НЕ в контейнере
# (gen читает env:TARGET_DB_DSN; seed — GIGACHAT_*). Инфра-адреса для хост-команд принудительно на
# localhost: в .env стоят compose-хосты (rabbitmq/qdrant), они верны для контейнеров, не для хоста.
-include .env
export

COMPOSE := docker compose -f docker-compose.yml -f docker-compose.agents.yml
# ollama в INFRA: его healthcheck зелен лишь когда bge-m3 подгружена (serve+pull встроены в старт
# сервиса, docker-compose.yml), поэтому `up --wait $(INFRA)` прогревает модель ДО seed (spec/45).
INFRA := rabbitmq postgres qdrant jaeger ollama target-db
LOCAL_RABBITMQ := amqp://agentarium:agentarium@localhost:5672/
LOCAL_QDRANT := http://localhost:6333
LOCAL_OLLAMA := http://localhost:11534

.PHONY: test test-integration test-e2e lint gen apply up demo seed

test:
	uv run pytest -m "not integration" -q

test-integration:
	uv run pytest -m integration -q

# e2e-live: три заявки spec/55 поверх ПОДНЯТОЙ системы на живом GigaChat (локально, не в CI).
test-e2e:
	AGENTARIUM_E2E=1 uv run pytest -m e2e -q

lint:
	uv run ruff check core tools tests agents demo

# gen — валидация рубежа 1 + генерация docker-compose.agents.yml. Только файлы: с брокером не говорит.
gen:
	uv run python -m agentarium gen configs/$(CONFIG).yaml

# apply — привести живой брокер к чертежу (topology apply, spec/40). Хост → localhost-порт брокера.
apply:
	RABBITMQ_URL=$(LOCAL_RABBITMQ) uv run python -m agentarium apply configs/$(CONFIG).yaml

# seed — проиндексировать базу знаний в Qdrant (spec/45). Эмбеддер — из чертежа (PoC: bge-m3/Ollama).
# Хост-адреса: Qdrant и Ollama на localhost-портах (в чертеже стоят compose-хосты, для контейнеров).
# COLLECTION=all по умолчанию. В make up встроен ПОСЛЕ инфраструктуры (модель уже прогрета), до агентов.
seed:
	QDRANT_URL=$(LOCAL_QDRANT) OLLAMA_BASE_URL=$(LOCAL_OLLAMA) uv run --extra brains python -m tools.ingest configs/$(CONFIG).yaml $(COLLECTION)

# up — вся система одной командой, строгий порядок из CLAUDE.md:
#   gen (файлы) → инфраструктура up+wait → topology apply → seed → агенты и шлюз up.
# Знания раньше агентов (иначе паспорт-сверка уронит rag); шлюз после apply (иначе его очереди ещё нет).
up: gen
	$(COMPOSE) up -d --wait $(INFRA)
	RABBITMQ_URL=$(LOCAL_RABBITMQ) uv run python -m agentarium apply configs/$(CONFIG).yaml
	QDRANT_URL=$(LOCAL_QDRANT) OLLAMA_BASE_URL=$(LOCAL_OLLAMA) uv run --extra brains python -m tools.ingest configs/$(CONFIG).yaml $(COLLECTION)
	$(COMPOSE) up -d --build --wait

# demo — полный цикл: up конфигурации + прогон трёх демо-заявок spec/55 (заявка → путь → ответ).
# Требует живой ключ GigaChat. Смена CONFIG пересобирает систему, включая шлюз.
demo: up
	uv run python demo/run_demo.py
