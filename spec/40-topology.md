# 40 · Чертёж системы

```
Статус: draft
Владелец: контракт конфигурации — каталог типов, реестр экземпляров, маршруты, вход и финалы
Зависит от: 00, 20, 30
```

## Определение

**Опр.** Чертёж (topology) — YAML-файл, единственный источник состава системы: какие экземпляры агентов существуют, как связаны маршрутами, где вход и что считается финалом. Ядро превращает чертёж в работающую систему.

**Опр.** Каталог типов (catalog) — `agents/catalog.yaml`: декларация каждого типа агента для ядра. Ядро не может импортировать код агентов (они в других образах) — каталог и есть их машиночитаемое лицо.

## Каталог типов

```yaml
# agents/catalog.yaml — владелец знания «какие типы существуют и каковы их контракты»
parser:
  build: agents/parser
  consumes: [request.new]
  produces: [request.parsed, request.rejected]
  config_schema: { model: llm }            # какие поля конфига обязан дать чертёж
rag:
  build: agents/rag
  consumes: [request.parsed]
  produces: [knowledge.found]
  config_schema: { model: llm, collection: str }
executor:
  build: agents/executor
  consumes: [knowledge.found]
  produces: [plan.ready]
  config_schema: { model: llm, target_db: dsn }
auditor:
  build: agents/auditor
  consumes: [plan.ready]
  produces: [audit.done]
  config_schema: { model: llm, collection: str }
```

Разделение владений: **универсальные** проверки (маршруты, манифесты, схема чертежа) — ядро; **типовые** схемы конфига (`collection`, `target_db`) — каталог. Ядро про «collection» не знает — оно лишь применяет схему типа из каталога. Так требование «добавление агента любого типа без изменения ядра» выполняется и для валидации: новый тип приносит свою строку каталога.

## Чертёж

```yaml
# configs/dba-base.yaml
system: dba-requests

entry: request.new              # тип, который шлюз публикует на POST /requests

agents:                         # реестр экземпляров: имя → тип из каталога + конфиг по схеме типа
  parser:
    type: parser
    model: { provider: gigachat, name: GigaChat-2 }
  knowledge:
    type: rag
    collection: regulations
    model: { provider: gigachat, name: GigaChat-2 }
  executor:
    type: executor
    target_db: env:TARGET_DB_DSN
    model: { provider: gigachat, name: GigaChat-2-Max }

collections:                    # общие коллекции знаний: владелец паспорта эмбеддингов (45)
  regulations:
    source: knowledge/regulations
    embeddings: { provider: gigachat, model: Embeddings }

routes:                         # маршруты: тип конверта → очереди экземпляров
  request.new:      [parser]
  request.parsed:   [knowledge]
  knowledge.found:  [executor]
  plan.ready:       [gateway]
  request.rejected: [gateway]
  task.failed:      [gateway]

finals:                         # финалы: как шлюзу завершать заявку — предметка не в ядре, а в чертеже
  plan.ready:       complete
  request.rejected: fail
  task.failed:      fail
```

Правила:
- **Тип ≠ экземпляр.** Один тип может стоять в системе много раз с разными конфигами: два `rag` с разными коллекциями — два узла из одного кода (класс/объект).
- `env:ИМЯ` подставляется из окружения — секреты и адреса в чертёж не вписываются.
- Fan-out: несколько получателей у типа — конверт получает каждый.
- `gateway` — зарезервированное имя входа/выхода: в `agents` не объявляется, в `routes` использоваться может; валидация знает об этом исключении.
- `finals` — карта «тип → complete | fail». Финальность конфигурируема: в расширенной конфигурации `plan.ready` перестаёт быть финалом и уходит аудитору, финалом становится `audit.done` (см. 55). Шлюз универсален: он исполняет `finals`, не зная предметных имён.

## Шлюз: HTTP-контракт (универсальный, предметки не содержит)

| Запрос | Ответ |
|---|---|
| `POST /requests {"text": "..."}` | `202 {"trace_id": "..."}` — конверт `entry`-типа опубликован, заявка `accepted` |
| `GET /requests/{trace_id}` | `200 {"status": "accepted \| done \| failed", "result": {...} \| null}` |
| `GET /requests/{trace_id}` (нет такой) | `404` |

Шлюз идемпотентен по финалам: повторная доставка финального конверта перезаписывает `result` той же заявки тем же содержимым (at-least-once переживается без дублей смысла).

## Как чертёж становится системой

1. **Маршруты = биндинги брокера.** Отдельного процесса-роутера нет: `make gen` объявляет topic-exchange `agentarium`, очередь `agentarium.<экземпляр>` каждому агенту и привязки «тип → очередь» по `routes`. Доставляет сам RabbitMQ. Topic-exchange (а не direct) — ради wildcard-привязок служебных стоков: `*.failed → agentarium.dlq`.
2. **Реконсиляция.** Биндинги приводятся к чертежу как diff: недостающие объявить, лишние снять. Смена конфигурации не оставляет очередей-сирот, в которые молча копятся конверты.
3. **Compose агентов генерируется.** `make gen` эмитит `docker-compose.agents.yml` из `agents` + каталога: build, env (имя экземпляра, mount чертежа), healthcheck на `/health`, `restart: unless-stopped`. Шаблон фиксированный и маленький; инфраструктура — статичный `docker-compose.yml`. Страховка в roadmap: если генератор начнёт съедать время — S3-гейт разрешает откат на статичный compose (см. 70).

## Валидация — fail-fast, двумя рубежами

**Рубеж 1 — `make gen` (полный чертёж):** схема YAML цела; каждый `type` есть в каталоге; конфиг экземпляра проходит `config_schema` типа; маршрут не ссылается на несуществующий экземпляр (кроме `gateway`); тип маршрутизирован только тем, чей `consumes` его принимает; каждый `produces` имеет маршрут — конверту есть куда лететь; `entry` и все ключи `finals` замкнуты на маршруты; у каждой упомянутой коллекции есть блок в `collections`.

**Рубеж 2 — старт агента (свой фрагмент):** имя экземпляра существует в чертеже; конфиг проходит схему; коллекция сверяется с паспортом (45). 

Полусобранная система не поднимается. Либо чертёж цел, либо громкий отказ со списком всех дыр сразу.
