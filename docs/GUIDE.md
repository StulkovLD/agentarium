# Гайд: собери свою систему на agentarium

Читатель этого файла — инженер, который хочет собрать **свою** мультиагентную систему на платформе. Про внутренности платформы здесь ничего нет — только как ей пользоваться.

## 1. Собери систему из готовых кубиков (только YAML)

Каталог типовых агентов: `parser` (текст → структура), `rag` (вопрос → фрагменты знаний), `executor` (контекст → план + проверки). Опиши чертёж:

```yaml
# configs/my-system.yaml
system: my-system

entry: request.new                 # что публикует шлюз на POST /requests

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
    embeddings: { provider: gigachat, model: Embeddings }

routes:
  request.new:      [intake]
  request.parsed:   [brain]
  knowledge.found:  [gateway]
  request.rejected: [gateway]      # intake умеет отказать — отказу тоже нужен маршрут
  task.failed:      [gateway]

finals:                            # как шлюзу завершать заявку
  knowledge.found:  complete
  request.rejected: fail
  task.failed:      fail
```

Запусти:

```bash
make up CONFIG=my-system    # одной командой: валидация чертежа → инфраструктура → seed → агенты
```

Правила чертежа, все поля и валидации: `../spec/40-topology.md`.

## 2. Наполни знания

Положи markdown-файлы в `knowledge/` и проиндексируй:

```bash
make seed COLLECTION=my-docs
```

Чанки режутся по заголовкам. Модель эмбеддингов фиксируется в паспорте коллекции — искать другой моделью система не даст (объяснение: `../spec/45-storage.md`).

## 3. Напиши своего агента (15 строк кода)

Нужен кубик, которого нет в каталоге:

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

Что даёт платформа бесплатно: подключение к шине, конверт, ретраи, `task.failed`, трейсинг, `/health`, доступ к складам (`self.storage`). Что делаешь ты: `consumes`/`produces` и `handle`. Ядро при этом не открывается вообще.

Дальше: Dockerfile по образцу любого агента каталога, экземпляр в чертёж, маршрут — и `make gen && make up`.

## 4. Смотри, как оно живёт

| Окно | Адрес | Что видно |
|---|---|---|
| Swagger | http://localhost:8000/docs | подать заявку из браузера |
| Jaeger | http://localhost:16686 | путь заявки через агентов, водопад |
| RabbitMQ | http://localhost:15672 | очереди, конверты в полёте |
| Qdrant | http://localhost:6333/dashboard | коллекции знаний |

## 5. Частые вопросы

- **Хочу два RAG с разными знаниями.** Два экземпляра одного типа с разными `collection` — тип это класс, экземпляр это объект:

  ```yaml
  agents:
    docs-brain:      { type: rag, collection: handbook,  model: { provider: gigachat, name: GigaChat-2 } }
    incidents-brain: { type: rag, collection: incidents, model: { provider: gigachat, name: GigaChat-2 } }
  ```

  Код один, узла два, знания разные.
- **Хочу свою модель / свой сервер с GPU.** `model: { provider: openai_compatible, base_url: http://my-gpu:11434/v1, name: qwen2.5 }` — платформа не заметит разницы: конфиг модели она передаёт агенту как данные. Сам адаптер провайдера — часть мозгов агента: у типовых агентов PoC реализован GigaChat, другой провайдер — плюс маленький адаптер в коде типа.
- **Агент падает.** Это штатно: конверт ждёт в очереди, docker перезапустит, ретраи в SDK. Смотри `task.failed` в трейсе и `agentarium.dlq` в RabbitMQ UI.
- **Хочу ветвление маршрутов.** Несколько получателей на тип (`fan-out`) — просто перечисли: `request.parsed: [brain, stats-collector]`. Один нюанс: достижимый `complete`-финал в конфигурации должен остаться ровно один — валидация чертежа за этим следит.
