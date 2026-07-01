"""SplitBot webhook — FastAPI app for the Meta Instagram Graph API.

GET  /webhook  -> Meta verification handshake.
POST /webhook  -> ingest incoming Instagram DMs, parse, act, reply.
"""
import hashlib
import hmac
import json

from fastapi import FastAPI, Request, Response, Query

import database as db
import ledger
from parser import parse_message
from instagram import send_text
from config import (
    META_VERIFY_TOKEN,
    META_APP_SECRET,
    INSTAGRAM_APP_SECRET,
    INSTAGRAM_ACCOUNT_ID,
    GRAPH_API_VERSION,
    GRAPH_API_HOST,
    logger,
)

app = FastAPI(title="SplitBot", version="1.1.0")


def _mask(secret: str, keep: int = 4) -> str:
    """Show only the last `keep` chars of a secret for safe logging."""
    if not secret:
        return "(empty/unset)"
    if len(secret) <= keep:
        return "*" * len(secret)
    return f"…{secret[-keep:]} (len={len(secret)})"


@app.on_event("startup")
def _log_startup_config():
    logger.info("=" * 60)
    logger.info("SplitBot starting (Instagram API with Instagram Login).")
    logger.info("  SEND HOST           = %s", GRAPH_API_HOST)
    logger.info("  GRAPH_API_VERSION   = %s", GRAPH_API_VERSION)
    logger.info("  META_VERIFY_TOKEN   = %s", _mask(META_VERIFY_TOKEN))
    logger.info("  META_APP_SECRET     = %s", _mask(META_APP_SECRET))
    logger.info("  INSTAGRAM_APP_SECRET= %s", _mask(INSTAGRAM_APP_SECRET))
    logger.info(
        "  signature check     = %s",
        "ON" if (META_APP_SECRET or INSTAGRAM_APP_SECRET) else "OFF — dev only!",
    )
    logger.info(
        "  INSTAGRAM_ACCOUNT_ID= %s",
        INSTAGRAM_ACCOUNT_ID or "(unset — relying on is_echo only)",
    )
    logger.info("=" * 60)


@app.get("/health")
def health():
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# GET /webhook  — verification handshake
# --------------------------------------------------------------------------- #
@app.get("/webhook")
def verify_webhook(
    mode: str = Query(default="", alias="hub.mode"),
    token: str = Query(default="", alias="hub.verify_token"),
    challenge: str = Query(default="", alias="hub.challenge"),
):
    logger.info(
        "GET /webhook verify attempt: mode=%r, token(recv)=%r, token(expected)=%r",
        mode, token, META_VERIFY_TOKEN,
    )
    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        logger.info("Webhook verified by Meta. Echoing challenge.")
        return Response(content=challenge, media_type="text/plain")

    # Be explicit about WHICH check failed so the terminal pinpoints it.
    if mode != "subscribe":
        logger.warning("Verify FAILED: hub.mode is %r, expected 'subscribe'.", mode)
    else:
        logger.warning(
            "Verify FAILED: token mismatch. Meta sent %r but META_VERIFY_TOKEN is %r. "
            "Make the dashboard 'Verify Token' EXACTLY equal to your env var.",
            token, META_VERIFY_TOKEN,
        )
    return Response(content="Verification failed", status_code=403)


# --------------------------------------------------------------------------- #
# Signature verification
# --------------------------------------------------------------------------- #
# Candidate signing secrets, in priority order. The Instagram-Login product may
# sign with the Meta App Secret OR the Instagram app secret — we try each.
_SIGNING_SECRETS = [
    ("META_APP_SECRET", META_APP_SECRET),
    ("INSTAGRAM_APP_SECRET", INSTAGRAM_APP_SECRET),
]


def _valid_signature(raw_body: bytes, header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256 (HMAC-SHA256 of the raw body).

    Tries every configured secret and logs which one matched. If NO secret is
    configured, verification is skipped (dev only) and a warning is logged.
    """
    secrets = [(name, s) for name, s in _SIGNING_SECRETS if s]
    if not secrets:
        logger.warning(
            "No app secret set (META_APP_SECRET / INSTAGRAM_APP_SECRET) — "
            "skipping webhook signature check. Dev only."
        )
        return True
    if not header or not header.startswith("sha256="):
        logger.warning(
            "Sig FAILED: missing/invalid X-Hub-Signature-256 header (got %r). "
            "Either Meta didn't send it, or a proxy stripped it.", header,
        )
        return False

    received = header.split("=", 1)[1]
    tried = []
    for name, secret in secrets:
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, received):
            logger.info("Signature OK — matched %s.", name)
            return True
        tried.append(f"{name}({_mask(secret)})→sha256={expected[:12]}…")

    logger.warning(
        "Sig FAILED: no configured secret matched received sha256=%s…. Tried: %s. "
        "For Instagram-Login, the correct value is often the Instagram app secret "
        "(Products → Instagram → API setup), NOT App Settings → Basic. Set it as "
        "INSTAGRAM_APP_SECRET and watch for 'matched INSTAGRAM_APP_SECRET'.",
        received[:12], "; ".join(tried),
    )
    return False


# --------------------------------------------------------------------------- #
# POST /webhook  — incoming messages
# --------------------------------------------------------------------------- #
def _extract_events(payload: dict) -> list[tuple]:
    """Return (mid, sender_id, text) for every actionable inbound message.

    Instagram uses the Messenger structure, and BOTH `entry` and `messaging` are
    arrays that Meta may batch (several users, or rapid messages from one, in a
    single POST). We iterate all of them rather than only [0].

        payload['entry'][i]['messaging'][j]
    with text at messaging_event['message']['text'] and the sender IGSID at
    messaging_event['sender']['id'].

    Skips anything we don't act on:
      - non-message events (read receipts, reactions, deliveries, postbacks)
      - echoes of our OWN outbound messages (message.is_echo, or sender == us),
        which would otherwise make the bot reply to itself forever
    `text` is None for a message with no text (e.g. an image/share).
    """
    events: list[tuple] = []
    try:
        for entry in payload.get("entry") or []:
            for event in entry.get("messaging") or []:
                sender_id = event.get("sender", {}).get("id")
                message = event.get("message")
                if not sender_id or not message:
                    continue  # read receipt / reaction / postback / malformed
                if message.get("is_echo"):
                    continue  # our own outbound — never process
                if INSTAGRAM_ACCOUNT_ID and sender_id == INSTAGRAM_ACCOUNT_ID:
                    continue
                events.append((message.get("mid"), sender_id, message.get("text")))
    except (KeyError, IndexError, TypeError, AttributeError):
        logger.warning("Could not extract messages from payload.")
    return events


UNKNOWN_SENDER_MSG = (
    "I don't know who you are 🤔 You're not part of this SplitBot group, so I "
    "can't track expenses for you."
)


def _process_event(mid, sender_id: str, text) -> None:
    """Idempotency + whitelist guard one message, then route it."""
    # --- Idempotency FIRST: applies to unknown senders too, so a webhook retry
    #     never re-sends the "don't know you" reply (or double-logs an expense). ---
    if mid and not db.claim_message(mid):
        logger.info("SKIP duplicate: mid %s already processed.", mid)
        return

    # --- Whitelist: unknown senders get a polite reply, then we stop. ---
    if not db.is_whitelisted(sender_id):
        logger.info(
            "Unknown sender %s (not in users whitelist) — replying 'don't know you'.",
            sender_id,
        )
        send_text(sender_id, UNKNOWN_SENDER_MSG)
        return
    else:
        name = db.get_name(sender_id)
        logger.info("Sender %s is known as %s.", sender_id, name)
        send_text(sender_id, f"Hi {name}! SUP BIYATCHHH")

    if not text:
        logger.info("Non-text message from %s — sending 'text only' reply.", sender_id)
        send_text(sender_id, "I can only read text messages right now. " + ledger.HELP_TEXT)
        return

    logger.info("MSG from %s (mid=%s): %r", sender_id, mid, text)
    _handle_text(sender_id, text)


@app.post("/webhook")
async def receive_webhook(request: Request):
    # Read the RAW body first — signature verification must run on exact bytes.
    raw_body = await request.body()
    logger.info("POST /webhook received: %d bytes.", len(raw_body))

    if not _valid_signature(raw_body, request.headers.get("X-Hub-Signature-256")):
        logger.warning("Returning 403 — signature check failed (see above).")
        return Response(content="Invalid signature", status_code=403)

    try:
        payload = json.loads(raw_body)
    except Exception:
        logger.exception("Bad JSON on webhook POST.")
        return {"status": "ignored"}

    events = _extract_events(payload)
    logger.info("Extracted %d actionable event(s) from payload.", len(events))
    if not events:
        return {"status": "ignored"}  # nothing we handle in this batch

    for mid, sender_id, text in events:
        _process_event(mid, sender_id, text)
    return {"status": "ok"}


def _handle_text(sender_id: str, text: str) -> None:
    """Route one text message: parse intent, then act and reply."""
    lowered = text.strip().lower()

    # Fast-path commands (no AI needed).
    if lowered in {"status", "balance", "balances", "summary"}:
        send_text(sender_id, ledger.render_status())
        return
    if lowered in {"help", "hi", "hello", "start"}:
        send_text(sender_id, ledger.HELP_TEXT)
        return

    known_names = [u["name"] for u in db.get_all_users()]
    parsed = parse_message(text, known_names=known_names)

    if parsed is None:
        send_text(sender_id, "Sorry, I couldn't process that just now. Please try again.")
        return

    logger.info("Parsed intent=%s for %s", parsed.intent, sender_id)
    if parsed.intent == "expense":
        reply = ledger.process_expense(sender_id, parsed)
    elif parsed.intent == "settle":
        reply = ledger.process_settle(sender_id, parsed)
    elif parsed.intent == "status":
        reply = ledger.render_status()
    else:
        reply = ledger.HELP_TEXT

    send_text(sender_id, reply)
