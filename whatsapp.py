"""Outbound WhatsApp messaging engine (Meta Cloud API)."""
import httpx

from config import (
    META_ACCESS_TOKEN,
    PHONE_NUMBER_ID,
    GRAPH_API_VERSION,
    logger,
)

_MESSAGES_URL = (
    f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
)


def send_text(to_phone: str, body: str) -> bool:
    """Send a plain text reply to a WhatsApp user. Returns True on success."""
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(_MESSAGES_URL, headers=headers, json=payload)
            resp.raise_for_status()
        return True
    except httpx.HTTPStatusError as exc:
        logger.error(
            "WhatsApp send failed (%s): %s", exc.response.status_code, exc.response.text
        )
        return False
    except Exception:
        logger.exception("WhatsApp send failed for %s", to_phone)
        return False
