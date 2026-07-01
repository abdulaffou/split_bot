# SplitBot 🤝

A WhatsApp expense-tracker bot for a fixed group of friends. Log shared or
personal expenses in plain English; SplitBot parses them with Gemini, keeps a
running "who owes whom" ledger in Supabase, and replies over the official Meta
WhatsApp Cloud API — all through a 1-on-1 chat with the bot.

## How it works

```
WhatsApp user ──▶ Meta Cloud API ──▶ POST /webhook (FastAPI)
                                          │
                        whitelist check (users table)
                                          │
                        Gemini 1.5 Flash parse (parser.py)
                                          │
             ┌────────────────────────────┴───────────────────────┐
      expense (ledger.py)                                    status / command
   split math + balances update                        render "who owes whom"
             └────────────────────────────┬───────────────────────┘
                                          │
                          send_text reply (whatsapp.py)
```

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
| `parser.py`      | Gemini structured-output parsing → `ParsedExpense`         |
| `ledger.py`      | Split math, balance updates, summary rendering             |
| `whatsapp.py`    | Outbound Meta Cloud API sender                             |
| `main.py`        | FastAPI webhook (GET verify + POST ingest)                 |
| `schema.sql`     | PostgreSQL schema + seed users                             |

## Setup

1. **Database** — in the Supabase SQL editor, run `schema.sql`. Edit the seeded
   users to your 6 real friends (phone numbers with country code, no `+`).

2. **Environment** — copy and fill in secrets:
   ```bash
   cp .env.example .env
   ```

3. **Install & run**
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```

4. **Expose & register the webhook** — point Meta at a public HTTPS URL (e.g. via
   ngrok during dev: `ngrok http 8000`). In the Meta app dashboard set:
   - Callback URL: `https://<your-domain>/webhook`
   - Verify token: the same value as `META_VERIFY_TOKEN`
   - Subscribe to the **messages** field.

## Security

- **Signature verification**: every POST is checked against the
  `X-Hub-Signature-256` HMAC-SHA256 header using `META_APP_SECRET` (from Meta app
  dashboard → Settings → Basic). Forged POSTs are rejected with 403. If
  `META_APP_SECRET` is left blank the check is skipped (dev only) and a warning is
  logged — **set it in production**.
- **Whitelist**: only numbers present in the `users` table are served.
- **Idempotency**: each WhatsApp message `id` is recorded in `processed_messages`;
  a re-delivered webhook is skipped so expenses are never double-logged.

## Notes

- Only whitelisted numbers (rows in `users`) are served; everything else is
  silently ignored.
- Every external call (Gemini, Meta, Supabase) is wrapped in try/except and
  degrades gracefully with a user-facing message.
- `google-generativeai` (used per the spec) prints a `FutureWarning` that it is
  deprecated in favour of `google-genai`. It still works; migrating parser.py to
  `google-genai` is a drop-in follow-up if you want to silence it.
- The Gemini structured-output schema requires all `ParsedExpense` fields, with
  **no Pydantic defaults** — the SDK's schema converter rejects `default` keys.

