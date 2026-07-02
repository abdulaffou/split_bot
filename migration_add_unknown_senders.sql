-- Migration: add the unknown_senders table.
-- Run this once in the Supabase SQL editor on an existing install. (A fresh
-- install via schema.sql already includes this table.)
--
-- Purpose: the bot no longer replies to non-whitelisted DMers. Instead it logs
-- their Instagram App-Scoped ID (IGSID) here so you can copy it into the users
-- table (with a display name) to add them to the group later.
--
-- To promote someone from here into the group:
--   INSERT INTO users (instagram_id, name) VALUES ('<igsid>', 'Their Name');
--   DELETE FROM unknown_senders WHERE instagram_id = '<igsid>';

BEGIN;

CREATE TABLE IF NOT EXISTS unknown_senders (
    instagram_id TEXT PRIMARY KEY,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
