-- Миграция 001 · таблица requests — склад состояния заявок. Владелец схемы: spec/45.
-- Один SQL-файл, применяется на старте (инструмент миграций не заводим: одна таблица его не оправдывает).
-- IF NOT EXISTS — потому что «применяется на старте»: повторный запуск процесса не должен падать.
CREATE TABLE IF NOT EXISTS requests (
    trace_id    uuid PRIMARY KEY,
    text        text        NOT NULL,          -- заявка как её подал человек
    status      text        NOT NULL,          -- accepted | done | failed
    result      jsonb,                         -- итоговый план / причина отказа
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
