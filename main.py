"""SplitBot webhook — FastAPI app for the Meta WhatsApp Cloud API.

GET  /webhook  -> Meta verification handshake.
POST /webhook  -> ingest incoming messages, parse, act, reply.
"""
import hashlib
import hmac
import json

from fastapi import FastAPI, Request, Response, Query

import database as db
import ledger
from parser import parse_message
from whatsapp import send_text
from config import META_VERIFY_TOKEN, META_APP_SECRET, logger

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
def _safe_extract(payload: dict):
    """Pull (message_id, from_phone, text_body) from the nested Meta payload.

    Returns (None, None, None) for anything that isn't an inbound user message
    (e.g. delivery/read status callbacks). text_body is None for non-text types.
    """
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        messages = value.get("messages")
        if not messages:
            return None, None, None  # status callbacks etc. have no 'messages'
        msg = messages[0]
        msg_id = msg.get("id")
        if msg.get("type") != "text":
            return msg_id, msg.get("from"), None  # non-text message
        return msg_id, msg["from"], msg["text"]["body"]
    except (KeyError, IndexError, TypeError):
        logger.warning("Could not extract message from payload.")
        return None, None, None


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

    msg_id, from_phone, text = _safe_extract(payload)

    if not from_phone:
        return {"status": "ignored"}  # not a user message we handle

    # --- Whitelist protection ---
    if not db.is_whitelisted(from_phone):
        logger.info("Ignoring message from non-whitelisted number: %s", from_phone)
        return {"status": "ignored"}

    # --- Idempotency: skip WhatsApp retries so expenses aren't double-logged. ---
    if msg_id and not db.claim_message(msg_id):
        return {"status": "duplicate"}

    if not text:
        send_text(from_phone, "I can only read text messages right now. " + ledger.HELP_TEXT)
        return {"status": "ok"}

    _handle_text(from_phone, text)
    return {"status": "ok"}


def _handle_text(from_phone: str, text: str) -> None:
    """Route one text message: parse intent, then act and reply."""
    lowered = text.strip().lower()

    # Fast-path commands (no AI needed).
    if lowered in {"status", "balance", "balances", "summary"}:
        send_text(from_phone, ledger.render_status())
        return
    if lowered in {"help", "hi", "hello", "start"}:
        send_text(from_phone, ledger.HELP_TEXT)
        return

    known_names = [u["name"] for u in db.get_all_users()]
    parsed = parse_message(text, known_names=known_names)

    if parsed is None:
        send_text(from_phone, "Sorry, I couldn't process that just now. Please try again.")
        return

    if parsed.intent == "expense":
        reply = ledger.process_expense(from_phone, parsed)
    elif parsed.intent == "settle":
        reply = ledger.process_settle(from_phone, parsed)
    elif parsed.intent == "status":
        reply = ledger.render_status()
    else:
        reply = ledger.HELP_TEXT

    send_text(from_phone, reply)
