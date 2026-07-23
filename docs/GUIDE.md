# Гайд: собери свою систему на agentarium

Этот файл — для инженера, который хочет собрать свою мультиагентную систему на платформе. Про внутренности платформы здесь ничего нет — только как ей пользоваться. Про устройство платформы см. `ARCHITECTURE.md`.

## 1. Собери систему из готовых агентов (только YAML)

В каталоге есть типовые агенты: `parser` (текст → структура), `rag` (вопрос → фрагменты знаний), `executor` (контекст → план и проверки). Опиши конфигурацию системы:

```yaml
# configs/my-system.yaml
system: my-system

entry: request.new                 # тип сообщения, который шлюз публикует на POST /requests

agents:
  intake:
    type: parser
    model: { provider: gigachat, name: GigaChat-2 }
  brain:
    type: rag
    collection: my-docs
    model: { provider: gigachat, name: GigaChat-2 }

collections:
  my-docs:
    source: knowledge/my-docs
    embeddings: { provider: ollama, model: bge-m3, base_url: http://ollama:11434 }

routes:
  request.new:      [intake]
  request.parsed:   [brain]
  knowledge.found:  [gateway]      # gateway — зарезервированное имя шлюза, в agents его не объявляют
  request.rejected: [gateway]      # intake умеет отказать — отказу тоже нужен маршрут
  task.failed:      [gateway]

finals:                            # как шлюзу завершать заявку
  knowledge.found:  complete
  request.rejected: fail
  task.failed:      fail
```

Запусти систему:

```bash
make up CONFIG=my-system
```

Одна команда делает всё по порядку: валидирует конфигурацию → поднимает инфраструктуру → индексирует знания → запускает агентов.

Правила конфигурации, все поля и валидации — в `../spec/40-topology.md`.

## 2. Наполни знания

Положи markdown-файлы в `knowledge/my-docs/` и проиндексируй коллекцию:

```bash
make seed COLLECTION=my-docs
```

Файлы режутся на чанки по заголовкам и загружаются в Qdrant. Модель эмбеддингов фиксируется в паспорте коллекции — искать другой моделью система не даст (объяснение — в `../spec/45-storage.md`).

## 3. Напиши своего агента

Нужен агент, которого нет в каталоге. Минимальный агент — это класс с манифестом и одним методом:

```python
# agents/translator/agent.py
from agentarium import Agent, Reply

class TranslatorAgent(Agent):
    consumes = ["request.parsed"]
    produces = ["request.translated"]

    async def handle(self, envelope) -> Reply | None:
        text = envelope.payload["text"]
        result = await self.my_brains(text)          # мозги любые: LangGraph, голый API, без LLM
        return Reply(type="request.translated", payload={"text": result})
```

Ты пишешь только `consumes`, `produces` и `handle`. Всё остальное даёт SDK: подключение к шине, сборку и валидацию сообщения, ретраи, `task.failed` при ошибке, трейсинг, эндпоинт `/health` и доступ к конфигурации экземпляра через `self.config`. Если нужны общие хранилища, в платформе есть клиенты Postgres и Qdrant (с проверкой паспорта коллекции) — так работают типовые `rag` и `executor`. Ядро при этом не открывается.

Дальше четыре шага:

1. **Dockerfile** — по образцу любого агента каталога (`agents/parser/Dockerfile`).
2. **Строка в каталоге типов** `agents/catalog.yaml` — регистрация типа, его манифест для валидации конфигураций:

   ```yaml
   translator:
     build: agents/translator
     consumes: [request.parsed]
     produces: [request.translated]
     config_schema: {}
   ```

3. **Экземпляр и маршруты** — в конфигурацию системы (`agents:` и `routes:`, как в §1).
4. **Запуск:**

   ```bash
   make up CONFIG=my-system
   ```

   Генерация docker-compose уже внутри `make up` — отдельной команды не нужно.

## 4. Смотри, как оно работает

| Окно | Адрес | Что видно |
|---|---|---|
| Swagger | http://localhost:8000/docs | подать заявку из браузера |
| Jaeger | http://localhost:16686 | путь заявки через агентов, водопад трейсов |
| RabbitMQ | http://localhost:15672 | очереди и сообщения в полёте |
| Qdrant | http://localhost:6333/dashboard | коллекции знаний |

## 5. Частые вопросы

**Хочу два RAG с разными знаниями.** Поставь два экземпляра одного типа с разными коллекциями — тип это класс, экземпляр это объект:

```yaml
agents:
  docs-brain:      { type: rag, collection: handbook,  model: { provider: gigachat, name: GigaChat-2 } }
  incidents-brain: { type: rag, collection: incidents, model: { provider: gigachat, name: GigaChat-2 } }
```

Код один, узла два, знания разные.

**Хочу свою модель или свой сервер с GPU.** Укажи её в конфигурации агента:

```yaml
model: { provider: openai_compatible, base_url: http://my-gpu:11434/v1, name: qwen2.5 }
```

Платформа передаёт конфигурацию модели агенту как данные и не заметит разницы. Два случая:

- провайдер уже реализован в типе агента (у типовых агентов — GigaChat для чата, Ollama для эмбеддингов) — хватает правки YAML;
- новый провайдер — нужен небольшой адаптер в коде типа (метод, который превращает конфиг модели в клиента), YAML тот же.

**Агент падает.** Это штатно: сообщение ждёт в очереди, Docker перезапустит контейнер, ретраи заложены в SDK. Итог заявки — в `GET /requests/{id}` (статус `failed`); в Jaeger виден путь до `task.failed`.

**Хочу ветвление маршрутов.** Перечисли несколько получателей у одного типа (fan-out):

```yaml
routes:
  request.parsed: [brain, stats-collector]
```

Сообщение получит каждый. Один нюанс: достижимый `complete`-финал в конфигурации должен остаться ровно один — иначе две ветки могли бы завершить одну заявку дважды с разными результатами, и итог зависел бы от того, кто добежал первым. Валидация ловит это на старте.
