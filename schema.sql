-- SplitBot database schema (PostgreSQL / Supabase)
-- Run this in the Supabase SQL editor before starting the app.

-- 1. Users (the whitelist). Phone number is the primary key, no leading '+'.
CREATE TABLE IF NOT EXISTS users (
    phone_number TEXT PRIMARY KEY,
    name         TEXT NOT NULL
);

-- 2. Transactions: every logged expense, split or personal.
--    is_personal = TRUE means the payer bought it only for themselves
--    (recorded for the spend summary, but never split into balances).
CREATE TABLE IF NOT EXISTS transactions (
    id          BIGSERIAL PRIMARY KEY,
    payer_phone TEXT NOT NULL REFERENCES users(phone_number),
    amount      NUMERIC(10, 2) NOT NULL,
    purpose     TEXT,
    is_personal BOOLEAN NOT NULL DEFAULT FALSE,
    date        DATE NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_transactions_payer ON transactions(payer_phone);

-- 3. Balances matrix: net_balance is what user_who_owes owes user_who_is_owed.
--    Raw-accumulate model: each split increments the relevant pair row.
CREATE TABLE IF NOT EXISTS balances (
    id                BIGSERIAL PRIMARY KEY,
    user_who_owes     TEXT NOT NULL REFERENCES users(phone_number),
    user_who_is_owed  TEXT NOT NULL REFERENCES users(phone_number),
    net_balance       NUMERIC(10, 2) NOT NULL DEFAULT 0,
    CONSTRAINT uq_balance_pair UNIQUE (user_who_owes, user_who_is_owed),
    CONSTRAINT chk_no_self_debt CHECK (user_who_owes <> user_who_is_owed)
);

-- 4. Idempotency guard: WhatsApp re-delivers webhooks it doesn't see acked in
--    time (and can duplicate on the happy path). We record each handled message
--    id so a retry never double-logs an expense.
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id   TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed the 6 friends (edit names / numbers as needed).
INSERT INTO users (phone_number, name) VALUES
    ('919876543210', 'Aarav'),
    ('919876543211', 'Bhavya'),
    ('919876543212', 'Chirag'),
    ('919876543213', 'Diya'),
    ('919876543214', 'Esha'),
    ('919876543215', 'Farhan')
ON CONFLICT (phone_number) DO NOTHING;
