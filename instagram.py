"""Outbound Instagram messaging engine (Meta Graph API, Messenger platform).

Replies are sent through the Page's /me/messages endpoint using the Permanent
Page Access Token. The recipient is addressed by their Instagram-scoped ID
(IGSID), which arrives on the inbound webhook as messaging_event['sender']['id'].
"""
import requests

from config import META_ACCESS_TOKEN, GRAPH_API_VERSION, logger

_MESSAGES_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"

# Instagram Send API caps a single text message at ~1000 chars (WhatsApp allowed
# 4096). Keep headroom so multi-byte chars / emoji can't tip us over.
_MAX_LEN = 980


def _chunk(body: str, limit: int = _MAX_LEN) -> list[str]:
    """Split `body` into <=limit-char pieces, preferring newline boundaries.

    A single line longer than `limit` is hard-split. Empty result never returned
    for a non-empty body.
    """
    if len(body) <= limit:
        return [body]
    chunks: list[str] = []
    current = ""
    for line in body.split("\n"):
        # Hard-split any single line that can't fit on its own.
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = line if not current else current + "\n" + line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _send_one(recipient_id: str, text: str) -> bool:
    params = {"access_token": META_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text},
    }
    try:
        resp = requests.post(
            _MESSAGES_URL, params=params, json=payload, timeout=10.0
        )
        resp.raise_for_status()
        return True
    except requests.HTTPError as exc:
        logger.error(
            "Instagram send failed (%s): %s",
            exc.response.status_code,
            exc.response.text,
        )
        return False
    except Exception:
        logger.exception("Instagram send failed for %s", recipient_id)
        return False


def send_text(recipient_id: str, body: str) -> bool:
    """Send a plain text reply to an Instagram user. Returns True on success.

    Long bodies (e.g. a big status summary) are split into multiple messages so
    each stays under the Instagram per-message character cap. recipient_id is the
    sender's IGSID from the inbound webhook.
    """
    ok = True
    for chunk in _chunk(body):
        if not _send_one(recipient_id, chunk):
            ok = False  # keep sending the rest; report partial failure
    return ok
