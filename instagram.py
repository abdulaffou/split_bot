"""Outbound Instagram messaging engine.

Uses the "Instagram API with Instagram Login" product: calls go to
graph.instagram.com and are authenticated with the Instagram User access token
(Bearer). The recipient is addressed by their Instagram-scoped ID (IGSID), which
arrives on the inbound webhook as messaging_event['sender']['id'].
"""
import requests

from config import META_ACCESS_TOKEN, GRAPH_API_VERSION, GRAPH_API_HOST, logger

_MESSAGES_URL = f"https://{GRAPH_API_HOST}/{GRAPH_API_VERSION}/me/messages"

# Instagram caps a single text message at 1000 BYTES (UTF-8), not characters.
# ₹ is 3 bytes, emoji up to 4, so we measure bytes and keep headroom.
_MAX_BYTES = 980


def _bytelen(s: str) -> int:
    return len(s.encode("utf-8"))


def _take_bytes(s: str, limit: int) -> str:
    """Largest prefix of `s` that fits in `limit` UTF-8 bytes without splitting
    a multi-byte character."""
    b = s.encode("utf-8")[:limit]
    while b:
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            b = b[:-1]  # drop a trailing continuation byte and retry
    return ""


def _chunk(body: str, limit: int = _MAX_BYTES) -> list[str]:
    """Split `body` into <=limit-BYTE pieces, preferring newline boundaries.

    A single line longer than `limit` bytes is hard-split on a char boundary.
    """
    if _bytelen(body) <= limit:
        return [body]
    chunks: list[str] = []
    current = ""
    for line in body.split("\n"):
        while _bytelen(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            piece = _take_bytes(line, limit)
            chunks.append(piece)
            line = line[len(piece):]
        candidate = line if not current else current + "\n" + line
        if _bytelen(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _send_one(recipient_id: str, text: str) -> bool:
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text},
    }
    try:
        resp = requests.post(
            _MESSAGES_URL, headers=headers, json=payload, timeout=10.0
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
    each stays under Instagram's per-message byte cap. recipient_id is the
    sender's IGSID from the inbound webhook.
    """
    chunks = _chunk(body)
    ok = True
    for i, chunk in enumerate(chunks, 1):
        if _send_one(recipient_id, chunk):
            logger.info(
                "Sent reply to %s (chunk %d/%d, %d bytes).",
                recipient_id, i, len(chunks), _bytelen(chunk),
            )
        else:
            ok = False  # keep sending the rest; report partial failure
    return ok
