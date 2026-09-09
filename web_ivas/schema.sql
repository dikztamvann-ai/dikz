-- D1 schema untuk dashboard IVAS. Mirror dari ivas_bot.db (SQLite bot).
-- Semua tabel pakai upsert via bot, jadi PK harus stabil.

CREATE TABLE IF NOT EXISTS allowed_users (
    user_id   INTEGER PRIMARY KEY,
    username  TEXT,
    added_by  INTEGER,
    added_at  TEXT
);

CREATE TABLE IF NOT EXISTS premium_access (
    user_id     INTEGER PRIMARY KEY,
    expired_at  TEXT,
    package     TEXT,
    days        INTEGER DEFAULT 0,
    total_paid  INTEGER DEFAULT 0,
    buy_count   INTEGER DEFAULT 0,
    invoice_id  TEXT,
    granted_by  INTEGER,
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS premium_tokens (
    user_id      INTEGER PRIMARY KEY,
    balance      INTEGER DEFAULT 0,
    total_bought INTEGER DEFAULT 0,
    total_spent  INTEGER DEFAULT 0,
    total_refund INTEGER DEFAULT 0,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS token_ledger (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL,
    delta         INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    ref           TEXT,
    note          TEXT,
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON token_ledger(user_id, id DESC);

CREATE TABLE IF NOT EXISTS premium_invoices (
    invoice_id  TEXT PRIMARY KEY,
    user_id     INTEGER,
    package     TEXT,
    kind        TEXT,
    days        INTEGER DEFAULT 0,
    tokens      INTEGER DEFAULT 0,
    amount      INTEGER DEFAULT 0,
    fee         INTEGER DEFAULT 0,
    total       INTEGER DEFAULT 0,
    status      TEXT,
    paid_at     TEXT,
    expired_at  TEXT,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_inv_status ON premium_invoices(status);

CREATE TABLE IF NOT EXISTS resetotp_trial (
    user_id       INTEGER PRIMARY KEY,
    used          INTEGER DEFAULT 0,
    first_used_at TEXT,
    last_used_at  TEXT
);
