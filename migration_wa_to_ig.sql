-- Migration: WhatsApp (phone_number) schema -> Instagram (instagram_id) schema.
-- Run this ONLY if the old phone-based tables already exist in your Supabase
-- project. For a brand-new install, run schema.sql instead.
--
-- Phone numbers cannot be converted into IGSIDs (they are unrelated identifiers),
-- so the existing rows must be wiped and re-seeded with real IGSIDs harvested
-- from your webhook logs. Run this whole file in the Supabase SQL editor.

BEGIN;

-- 1. Rename the identifier columns. In Postgres, the foreign-key constraints on
--    transactions/balances follow a column RENAME automatically and stay valid.
ALTER TABLE users        RENAME COLUMN phone_number TO instagram_id;
ALTER TABLE transactions RENAME COLUMN payer_phone  TO payer_id;

-- 2. Wipe the old phone-based data (children first, or the FKs block the parent).
DELETE FROM balances;
DELETE FROM transactions;
DELETE FROM users;

-- 3. Re-seed with real IGSIDs. Replace the placeholders with the values from
--    your webhook logs (each user's sender.id, ~16-17 digit strings).
INSERT INTO users (instagram_id, name) VALUES
    ('REPLACE_WITH_IGSID_1', 'Aarav'),
    ('REPLACE_WITH_IGSID_2', 'Bhavya'),
    ('REPLACE_WITH_IGSID_3', 'Chirag'),
    ('REPLACE_WITH_IGSID_4', 'Diya'),
    ('REPLACE_WITH_IGSID_5', 'Esha'),
    ('REPLACE_WITH_IGSID_6', 'Farhan')
ON CONFLICT (instagram_id) DO NOTHING;

-- Note: processed_messages needs no change — message_id is already generic TEXT.

COMMIT;
