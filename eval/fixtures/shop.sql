-- shop.sql
-- A realistic e-commerce schema used as the canonical fixture for the
-- PromptQuery retrieval eval. Designed to exercise:
--   - simple lookups
--   - aggregations
--   - one- and multi-hop joins
--   - inbound and outbound FK edges
--   - time-range queries
--   - the FK-graph expansion path in retrieval.py
--
-- Apache-2.0. Hand-crafted, no real data.

CREATE TABLE countries (
    code           CHAR(2)       PRIMARY KEY,
    name           TEXT          NOT NULL,
    currency_code  CHAR(3)       NOT NULL
);
COMMENT ON TABLE  countries IS 'ISO country reference data';

CREATE TABLE plans (
    id             BIGSERIAL     PRIMARY KEY,
    name           TEXT          NOT NULL UNIQUE,
    monthly_price  NUMERIC(10,2) NOT NULL
);
COMMENT ON TABLE  plans IS 'subscription tiers offered to customers';

CREATE TABLE users (
    id            BIGSERIAL      PRIMARY KEY,
    email         TEXT           NOT NULL UNIQUE,
    full_name     TEXT           NOT NULL,
    country_code  CHAR(2)        REFERENCES countries(code),
    plan_id       BIGINT         REFERENCES plans(id),
    signup_date   DATE           NOT NULL,
    is_active     BOOLEAN        NOT NULL DEFAULT TRUE
);
COMMENT ON TABLE  users IS 'end-customer accounts';

CREATE TABLE addresses (
    id            BIGSERIAL      PRIMARY KEY,
    user_id       BIGINT         NOT NULL REFERENCES users(id),
    street        TEXT           NOT NULL,
    city          TEXT           NOT NULL,
    postal_code   TEXT,
    country_code  CHAR(2)        NOT NULL REFERENCES countries(code),
    is_default    BOOLEAN        NOT NULL DEFAULT FALSE
);
COMMENT ON TABLE  addresses IS 'shipping and billing addresses for users';

CREATE TABLE categories (
    id            BIGSERIAL      PRIMARY KEY,
    name          TEXT           NOT NULL,
    parent_id     BIGINT         REFERENCES categories(id)
);
COMMENT ON TABLE  categories IS 'product taxonomy with self-referential parent';

CREATE TABLE products (
    id            BIGSERIAL      PRIMARY KEY,
    sku           TEXT           NOT NULL UNIQUE,
    name          TEXT           NOT NULL,
    category_id   BIGINT         REFERENCES categories(id),
    price         NUMERIC(10,2)  NOT NULL,
    is_available  BOOLEAN        NOT NULL DEFAULT TRUE
);
COMMENT ON TABLE  products IS 'catalog of items for sale';

CREATE TABLE orders (
    id            BIGSERIAL      PRIMARY KEY,
    user_id       BIGINT         NOT NULL REFERENCES users(id),
    shipping_address_id BIGINT   REFERENCES addresses(id),
    status        TEXT           NOT NULL,
    total_amount  NUMERIC(12,2)  NOT NULL,
    created_at    TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE  orders IS 'customer purchase orders';

CREATE TABLE order_items (
    id            BIGSERIAL      PRIMARY KEY,
    order_id      BIGINT         NOT NULL REFERENCES orders(id),
    product_id    BIGINT         NOT NULL REFERENCES products(id),
    quantity      INTEGER        NOT NULL,
    unit_price    NUMERIC(10,2)  NOT NULL
);
COMMENT ON TABLE  order_items IS 'line items inside an order';

CREATE TABLE payments (
    id            BIGSERIAL      PRIMARY KEY,
    order_id      BIGINT         NOT NULL REFERENCES orders(id),
    method        TEXT           NOT NULL,
    amount        NUMERIC(12,2)  NOT NULL,
    processed_at  TIMESTAMPTZ    NOT NULL,
    status        TEXT           NOT NULL
);
COMMENT ON TABLE  payments IS 'payment transactions for orders';

CREATE TABLE reviews (
    id            BIGSERIAL      PRIMARY KEY,
    user_id       BIGINT         NOT NULL REFERENCES users(id),
    product_id    BIGINT         NOT NULL REFERENCES products(id),
    rating        SMALLINT       NOT NULL,
    body          TEXT,
    created_at    TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE  reviews IS 'product reviews written by users';

CREATE TABLE shipments (
    id            BIGSERIAL      PRIMARY KEY,
    order_id      BIGINT         NOT NULL REFERENCES orders(id),
    carrier       TEXT           NOT NULL,
    tracking_no   TEXT,
    shipped_at    TIMESTAMPTZ,
    delivered_at  TIMESTAMPTZ
);
COMMENT ON TABLE  shipments IS 'physical shipment tracking for orders';

CREATE TABLE sessions (
    id            BIGSERIAL      PRIMARY KEY,
    user_id       BIGINT         REFERENCES users(id),
    started_at    TIMESTAMPTZ    NOT NULL,
    ended_at      TIMESTAMPTZ,
    ip_address    INET,
    user_agent    TEXT
);
COMMENT ON TABLE  sessions IS 'user web sessions';

CREATE TABLE refunds (
    id            BIGSERIAL      PRIMARY KEY,
    payment_id    BIGINT         NOT NULL REFERENCES payments(id),
    reason        TEXT,
    amount        NUMERIC(12,2)  NOT NULL,
    refunded_at   TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE  refunds IS 'refunded payments';

CREATE TABLE audit_log (
    id            BIGSERIAL      PRIMARY KEY,
    table_name    TEXT           NOT NULL,
    action        TEXT           NOT NULL,
    row_id        BIGINT,
    actor_user_id BIGINT         REFERENCES users(id),
    occurred_at   TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE  audit_log IS 'append-only audit trail of mutations';
