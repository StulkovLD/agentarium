-- target-db «пациент» PoC: схема billing и наполнение (spec/55).
-- Данные синтетические, генерируются generate_series — объём для правдоподобия db_size()
-- и активности; PII нет. Объекты владеет billing_owner, не человек (регламент role-model.md).

-- Гигиена схемы public: снять неявные права PUBLIC.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE billing FROM PUBLIC;

-- Создаём объекты от имени роли-владельца, чтобы они сразу принадлежали billing_owner.
GRANT CREATE ON SCHEMA public TO billing_owner;
SET ROLE billing_owner;

CREATE TABLE customers (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name  text NOT NULL,
    email      text NOT NULL UNIQUE,
    inn        char(12),
    status     text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tariffs (
    id          int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code        text NOT NULL UNIQUE,
    title       text NOT NULL,
    monthly_fee numeric(10,2) NOT NULL
);

CREATE TABLE subscriptions (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES customers(id),
    tariff_id   int    NOT NULL REFERENCES tariffs(id),
    started_at  date   NOT NULL,
    status      text   NOT NULL DEFAULT 'active'
);

CREATE TABLE invoices (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES customers(id),
    period      date   NOT NULL,
    amount      numeric(10,2) NOT NULL,
    status      text   NOT NULL DEFAULT 'issued',
    issued_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE payments (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    invoice_id bigint NOT NULL REFERENCES invoices(id),
    amount     numeric(10,2) NOT NULL,
    method     text   NOT NULL,
    paid_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
    id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor  text NOT NULL,
    action text NOT NULL,
    object text NOT NULL,
    at     timestamptz NOT NULL DEFAULT now()
);

-- Наполнение.
INSERT INTO tariffs (code, title, monthly_fee) VALUES
    ('BASE',  'Базовый',           299.00),
    ('PRO',   'Профессиональный',  899.00),
    ('ENT',   'Корпоративный',    2990.00),
    ('TRIAL', 'Пробный',             0.00);

INSERT INTO customers (full_name, email, inn, status)
SELECT 'Клиент ' || g,
       'client' || g || '@example.test',
       lpad((100000000000 + g)::text, 12, '0'),
       CASE WHEN g % 17 = 0 THEN 'blocked' ELSE 'active' END
FROM generate_series(1, 500) AS g;

INSERT INTO subscriptions (customer_id, tariff_id, started_at, status)
SELECT c.id,
       1 + (c.id % 4),
       DATE '2024-01-01' + (c.id % 365)::int,
       CASE WHEN c.id % 23 = 0 THEN 'cancelled' ELSE 'active' END
FROM customers c;

INSERT INTO invoices (customer_id, period, amount, status)
SELECT c.id,
       (DATE '2025-01-01' + (m || ' month')::interval)::date,
       (100 + (c.id % 50) * 10)::numeric(10,2),
       CASE WHEN m % 6 = 5 THEN 'overdue'
            WHEN m % 3 = 0 THEN 'paid'
            ELSE 'issued' END
FROM customers c, generate_series(0, 11) AS m;

INSERT INTO payments (invoice_id, amount, method, paid_at)
SELECT i.id,
       i.amount,
       (ARRAY['card','sbp','invoice'])[1 + (i.id % 3)],
       i.period + INTERVAL '5 day'
FROM invoices i
WHERE i.status = 'paid';

INSERT INTO audit_log (actor, action, object, at)
SELECT (ARRAY['ivanov','petrov','sidorova','app_billing'])[1 + (g % 4)],
       (ARRAY['SELECT','UPDATE','INSERT','GRANT'])[1 + (g % 4)],
       'invoices#' || g,
       now() - (g || ' hour')::interval
FROM generate_series(1, 200) AS g;

CREATE INDEX ON invoices (customer_id);
CREATE INDEX ON invoices (status);
CREATE INDEX ON payments (invoice_id);
CREATE INDEX ON subscriptions (customer_id);

RESET ROLE;
