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
    INSTAGRAM_ACCOUNT_ID,
    logger,
)

app = FastAPI(title="SplitBot", version="1.1.0")


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
    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        logger.info("Webhook verified by Meta.")
        return Response(content=challenge, media_type="text/plain")
    logger.warning("Webhook verification failed (mode=%s).", mode)
    return Response(content="Verification failed", status_code=403)


# --------------------------------------------------------------------------- #
# Signature verification
# --------------------------------------------------------------------------- #
def _valid_signature(raw_body: bytes, header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256 (HMAC-SHA256 of the raw body).

    If META_APP_SECRET is unset we skip verification (dev only) and log a warning.
    """
    if not META_APP_SECRET:
        logger.warning("META_APP_SECRET not set — skipping webhook signature check.")
        return True
    if not header or not header.startswith("sha256="):
        logger.warning("Missing/invalid X-Hub-Signature-256 header.")
        return False
    expected = hmac.new(
        META_APP_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


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


def _process_event(mid, sender_id: str, text) -> None:
    """Whitelist + idempotency guard one message, then route it."""
    # --- Whitelist protection ---
    if not db.is_whitelisted(sender_id):
        logger.info("Ignoring message from non-whitelisted IGSID: %s", sender_id)
        return

    # --- Idempotency: skip webhook retries so expenses aren't double-logged. ---
    if mid and not db.claim_message(mid):
        return

    if not text:
        send_text(sender_id, "I can only read text messages right now. " + ledger.HELP_TEXT)
        return

    _handle_text(sender_id, text)


@app.post("/webhook")
async def receive_webhook(request: Request):
    # Read the RAW body first — signature verification must run on exact bytes.
    raw_body = await request.body()

    if not _valid_signature(raw_body, request.headers.get("X-Hub-Signature-256")):
        return Response(content="Invalid signature", status_code=403)

    try:
        payload = json.loads(raw_body)
    except Exception:
        logger.exception("Bad JSON on webhook POST.")
        return {"status": "ignored"}

    events = _extract_events(payload)
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

    if parsed.intent == "expense":
        reply = ledger.process_expense(sender_id, parsed)
    elif parsed.intent == "settle":
        reply = ledger.process_settle(sender_id, parsed)
    elif parsed.intent == "status":
        reply = ledger.render_status()
    else:
        reply = ledger.HELP_TEXT

    send_text(sender_id, reply)
