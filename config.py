"""Central configuration. Single source for all environment variables.

Every setting is read once at import time and validated so the app fails fast
on boot if something is missing, rather than mid-request.
"""
import os
import logging

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("splitbot")


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


# --- Instagram API with Instagram Login (graph.instagram.com) ---
# META_ACCESS_TOKEN is the long-lived Instagram User access token (from Business
# Login for Instagram). NOT a Facebook Page token — this is the pure-Instagram
# product, so calls go to graph.instagram.com, not graph.facebook.com.
META_ACCESS_TOKEN = _require("META_ACCESS_TOKEN")
META_VERIFY_TOKEN = _require("META_VERIFY_TOKEN")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v25.0")
# Host for outbound send calls. Instagram-Login product = graph.instagram.com.
# (Set to graph.facebook.com only if you revert to the Page-linked product.)
GRAPH_API_HOST = os.getenv("GRAPH_API_HOST", "graph.instagram.com")
# Own Instagram App-Scoped ID (IGSID). Optional: if set, inbound events whose
# sender is our own account are dropped as a second echo guard (belt-and-braces
# with the message.is_echo flag).
INSTAGRAM_ACCOUNT_ID = os.getenv("INSTAGRAM_ACCOUNT_ID", "")
# App secret used to verify the X-Hub-Signature-256 header on inbound webhooks.
# Optional: if unset, signature verification is skipped (dev only — a warning is
# logged). Set it in production to reject forged POSTs.
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
# For the Instagram-Login product the webhook can be signed with the *Instagram*
# app secret (Products → Instagram → API setup with Instagram Login → Instagram
# app secret) rather than the Meta App Secret above. Set both; the signature
# check tries each and logs which one matched.
INSTAGRAM_APP_SECRET = os.getenv("INSTAGRAM_APP_SECRET", "")

# --- Google AI Studio (Gemini) ---
GEMINI_API_KEY = _require("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

# --- Supabase ---
SUPABASE_URL = _require("SUPABASE_URL")
SUPABASE_KEY = _require("SUPABASE_KEY")
