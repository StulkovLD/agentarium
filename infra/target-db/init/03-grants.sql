-- target-db «пациент» PoC: гранты на объекты (spec/55, регламент role-model.md).
-- Выполняется после 02-schema — объекты уже существуют. Роли и группы — из 01-roles.

-- Групповые роли: ro читает, rw дописывает права записи.
GRANT CONNECT ON DATABASE billing TO billing_ro;                       -- rw наследует через ro
GRANT USAGE   ON SCHEMA public     TO billing_ro;
GRANT SELECT  ON ALL TABLES        IN SCHEMA public TO billing_ro;

GRANT INSERT, UPDATE, DELETE ON ALL TABLES     IN SCHEMA public TO billing_rw;
GRANT USAGE, SELECT          ON ALL SEQUENCES   IN SCHEMA public TO billing_rw;

-- Права по умолчанию: новые таблицы владельца сразу доступны группам (регламент role-model.md).
ALTER DEFAULT PRIVILEGES FOR ROLE billing_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO billing_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE billing_owner IN SCHEMA public
    GRANT INSERT, UPDATE, DELETE ON TABLES TO billing_rw;

-- readonly_executor: подключение + только чтение таблиц. Записи нет ни грантами,
-- ни транзакционно (default_transaction_read_only=on на роли, 01-roles).
GRANT CONNECT ON DATABASE billing TO readonly_executor;
GRANT USAGE   ON SCHEMA public     TO readonly_executor;
GRANT SELECT  ON ALL TABLES        IN SCHEMA public TO readonly_executor;
ALTER DEFAULT PRIVILEGES FOR ROLE billing_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO readonly_executor;
