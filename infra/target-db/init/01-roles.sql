-- target-db «пациент» PoC: ролевая модель (spec/55, регламент role-model.md).
-- Порядок init-скриптов: 01-roles → 02-schema → 03-grants (алфавитный, зависимости соблюдены).
-- Групповые роли носят привилегии, логины входят в них членством. Пароли — демо-фикстуры
-- (пациент одноразовый, не секрет); readonly_executor/readonly совпадает с TARGET_DB_DSN (env.example).

-- Групповые роли (NOLOGIN) — контейнеры привилегий по уровню доступа.
CREATE ROLE billing_ro    NOLOGIN;   -- только чтение
CREATE ROLE billing_rw    NOLOGIN;   -- чтение + запись данных
CREATE ROLE billing_owner NOLOGIN;   -- владелец объектов, DDL

-- rw включает ro — иерархия ролей без дублирования грантов.
GRANT billing_ro TO billing_rw;

-- Логины людей и приложения. Права получают только через членство в группах.
-- ivanov — субъект демо-заявки «проверь доступы ivanov»: разработчик с доступом на запись.
CREATE ROLE ivanov      LOGIN PASSWORD 'ivanov';
CREATE ROLE petrov      LOGIN PASSWORD 'petrov'   VALID UNTIL '2026-12-31';  -- срочный доступ аналитика
CREATE ROLE sidorova    LOGIN PASSWORD 'sidorova';
CREATE ROLE app_billing LOGIN PASSWORD 'app_billing' CONNECTION LIMIT 20;    -- сервисная учётка

GRANT billing_rw TO ivanov;       -- разработчик: чтение + запись
GRANT billing_ro TO petrov;       -- аналитик: только чтение
GRANT billing_ro TO sidorova;     -- поддержка: только чтение
GRANT billing_rw TO app_billing;  -- приложение: чтение + запись

-- readonly_executor — изолированная роль автоматических allowlist-проверок (spec/55).
-- pg_monitor: видит pg_stat_activity шире собственной сессии (active_connections()).
-- read-only на уровне роли + statement_timeout: сырого write/DDL от исполнителя не существует.
CREATE ROLE readonly_executor LOGIN PASSWORD 'readonly';
GRANT pg_monitor TO readonly_executor;
ALTER ROLE readonly_executor SET statement_timeout = '5s';
ALTER ROLE readonly_executor SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE readonly_executor SET default_transaction_read_only = on;
