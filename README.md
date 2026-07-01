# SplitBot 🤝

An Instagram DM expense-tracker bot for a fixed group of friends. Log shared or
personal expenses in plain English; SplitBot parses them with Gemini, keeps a
running "who owes whom" ledger in Supabase, and replies over the Meta Instagram
Graph API — all through 1-on-1 Instagram Direct chats with the bot.

The bot uses the **Instagram API with Instagram Login** (the pure-Instagram
product — no Facebook Page). It authenticates with a **long-lived Instagram User
access token** and sends replies via **`graph.instagram.com`**. Inbound webhooks
use the Messenger-style `entry[].messaging[]` structure.

## How it works

```
Instagram user ──▶ Meta webhook ──▶ POST /webhook (FastAPI)
                                          │
                        echo guard (skip our own messages)
                                          │
                        whitelist check (users table)
                                          │
                        Gemini parse (parser.py)
                                          │
             ┌────────────────────────────┴───────────────────────┐
      expense (ledger.py)                                    status / command
   split math + balances update                        render "who owes whom"
             └────────────────────────────┬───────────────────────┘
                                          │
                          send_text reply (instagram.py)
```

Users are identified by their **Instagram App-Scoped User ID (IGSID)** — a long
integer string delivered on the webhook as `sender.id`. IGSIDs are **not** phone
numbers and cannot be known ahead of time: you harvest each one from the webhook
logs the first time that user DMs the bot, then add it to the `users` table.

## Splitting rules

- **Shared with everyone** (default): `1200 dinner yesterday` → split across all
  group members (payer included). Each other member owes `amount / N`.
- **Shared with some**: `900 cab with Diya and Esha` → split only among the named
  people plus the payer.
- **Personal**: `my coffee 150` → recorded but **not split**. Shows up only in the
  personal-spend section of the summary.
- **Settle up**: `settled with Diya` clears the whole balance between you two;
  `paid Diya 500` reduces what you owe Diya by ₹500 (overpayment is capped and
  flagged). This is how balances go *down*.
- **Status**: `status`, `balance`, or `summary` → breakdown of who owes whom plus
  each person's personal spend total.

Balances use a **raw-accumulate** model: each split adds to the relevant
`(owes, owed)` pair; no automatic bidirectional netting. Settle-up subtracts.

**Ambiguous names are never guessed.** If a name in a split/settle matches more
than one member (or none), the bot asks you to clarify and resends nothing to the
ledger — a typo can't silently corrupt balances. Exact (case-insensitive) name
matches always win over partial ones.

## File layout

| File             | Responsibility                                             |
|------------------|------------------------------------------------------------|
| `config.py`      | Loads & validates env vars (fails fast on boot)            |
| `database.py`    | All Supabase reads/writes                                  |
| `parser.py`      | Gemini structured-output parsing → `ParsedMessage`         |
| `ledger.py`      | Split math, balance updates, summary rendering             |
| `instagram.py`   | Outbound Instagram Graph API sender (`/me/messages`)       |
| `main.py`        | FastAPI webhook (GET verify + POST ingest)                 |
| `schema.sql`     | PostgreSQL schema + migration + seed users                 |

## Setup

1. **Meta app / Instagram** — you need:
   - an Instagram **Professional (Creator or Business)** account,
   - a Meta app with the **Instagram** product ("Instagram API with Instagram
     Login") added, and `instagram_business_manage_messages` permission,
   - **"Allow Access to Messages"** enabled in the IG app's message settings,
   - a **long-lived Instagram User access token** via Business Login for
     Instagram → `META_ACCESS_TOKEN`. (No Facebook Page required.)

2. **Database** — in the Supabase SQL editor, run `schema.sql`. Seed the `users`
   table with each friend's **IGSID** and display name (see IGSID note below).

3. **Environment** — copy and fill in secrets:
   ```bash
   cp .env.example .env
   ```
   Set `META_ACCESS_TOKEN` (Page token), `META_VERIFY_TOKEN`, `META_APP_SECRET`,
   `GRAPH_API_VERSION` (default `v25.0`), `GEMINI_API_KEY`, and the Supabase vars.
   Optionally set `INSTAGRAM_ACCOUNT_ID` (your own IGSID) as a second echo guard.

4. **Install & run**
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```

5. **Expose & register the webhook** — point Meta at a public HTTPS URL (e.g. via
   ngrok during dev: `ngrok http 8000`). In the Meta app dashboard, under the
   **Instagram** product → Webhooks:
   - Callback URL: `https://<your-domain>/webhook`
   - Verify token: the same value as `META_VERIFY_TOKEN`
   - Subscribe to the **messages** field.

### Harvesting IGSIDs

An IGSID only exists once a user has messaged the bot. To seed a new friend:
1. Have them send any DM to the bot's Instagram account.
2. Read `sender.id` from the incoming webhook (server logs).
3. `INSERT INTO users (instagram_id, name) VALUES ('<that id>', 'Their Name');`

Until an IGSID is whitelisted, the bot silently ignores that person's messages.

## Security

- **Signature verification**: every POST is checked against the
  `X-Hub-Signature-256` HMAC-SHA256 header using `META_APP_SECRET` (from Meta app
  dashboard → Settings → Basic). Forged POSTs are rejected with 403. If
  `META_APP_SECRET` is left blank the check is skipped (dev only) and a warning is
  logged — **set it in production**.
- **Echo guard**: Meta echoes the bot's own outbound messages back as inbound
  webhooks. Any event with `message.is_echo` (or, if `INSTAGRAM_ACCOUNT_ID` is
  set, from our own IGSID) is dropped so the bot never replies to itself.
- **Whitelist**: only IGSIDs present in the `users` table are served.
- **Idempotency**: each message `mid` is recorded in `processed_messages`; a
  re-delivered webhook is skipped so expenses are never double-logged.

## Notes

- Only whitelisted IGSIDs (rows in `users`) are served; everything else is
  silently ignored.
- Every external call (Gemini, Meta, Supabase) is wrapped in try/except and
  degrades gracefully with a user-facing message.
- Non-text messages (images, shares, reactions) get a "text only" reply.
- `google-generativeai` (used per the spec) prints a `FutureWarning` that it is
  deprecated in favour of `google-genai`. It still works; migrating parser.py to
  `google-genai` is a drop-in follow-up if you want to silence it.
- The Gemini structured-output schema requires all `ParsedMessage` fields, with
  **no Pydantic defaults** — the SDK's schema converter rejects `default` keys.
