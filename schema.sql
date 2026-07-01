-- SplitBot database schema (PostgreSQL / Supabase) — Instagram edition.
-- Fresh install: run this whole file in the Supabase SQL editor before starting
-- the app. (Upgrading an existing WhatsApp/phone install? Use migration_wa_to_ig.sql.)
--
-- Users are identified by their Instagram App-Scoped User ID (IGSID): a long
-- integer string delivered on the inbound webhook as sender.id. It is NOT a
-- phone number. There is no way to derive an IGSID ahead of time — you harvest
-- each one from the webhook logs the first time that user DMs the bot, then
-- seed it below with their display name.

-- 1. Users (the whitelist). Instagram ID (IGSID) is the primary key.
CREATE TABLE IF NOT EXISTS users (
    instagram_id TEXT PRIMARY KEY,
    name         TEXT NOT NULL
);

-- 2. Transactions: every logged expense, split or personal.
--    is_personal = TRUE means the payer bought it only for themselves
--    (recorded for the spend summary, but never split into balances).
CREATE TABLE IF NOT EXISTS transactions (
    id          BIGSERIAL PRIMARY KEY,
    payer_id    TEXT NOT NULL REFERENCES users(instagram_id),
    amount      NUMERIC(10, 2) NOT NULL,
    purpose     TEXT,
    is_personal BOOLEAN NOT NULL DEFAULT FALSE,
    date        DATE NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_transactions_payer ON transactions(payer_id);

-- 3. Balances matrix: net_balance is what user_who_owes owes user_who_is_owed.
--    Raw-accumulate model: each split increments the relevant pair row.
--    Both columns hold IGSIDs.
CREATE TABLE IF NOT EXISTS balances (
    id                BIGSERIAL PRIMARY KEY,
    user_who_owes     TEXT NOT NULL REFERENCES users(instagram_id),
    user_who_is_owed  TEXT NOT NULL REFERENCES users(instagram_id),
    net_balance       NUMERIC(10, 2) NOT NULL DEFAULT 0,
    CONSTRAINT uq_balance_pair UNIQUE (user_who_owes, user_who_is_owed),
    CONSTRAINT chk_no_self_debt CHECK (user_who_owes <> user_who_is_owed)
);

-- 4. Idempotency guard: Meta re-delivers webhooks it doesn't see acked in time
--    (and can duplicate on the happy path). We record each handled message id
--    (message.mid) so a retry never double-logs an expense.
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id   TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed the group with real IGSIDs. Replace the placeholder ids below with the
-- values harvested from your webhook logs. (IGSIDs are ~16-17 digit strings.)
INSERT INTO users (instagram_id, name) VALUES
    ('REPLACE_WITH_IGSID_1', 'Affou'),
    ('REPLACE_WITH_IGSID_2', 'Hisham'),
    ('REPLACE_WITH_IGSID_3', 'Chirag'),
    ('REPLACE_WITH_IGSID_4', 'Diya'),
    ('REPLACE_WITH_IGSID_5', 'Esha'),
    ('REPLACE_WITH_IGSID_6', 'Farhan')
ON CONFLICT (instagram_id) DO NOTHING;
