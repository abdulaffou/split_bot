"""Data-access layer. All Supabase reads/writes live here.

Every function wraps its Supabase call in try/except and returns a safe default
(None / [] / False) on failure so the request handlers never crash on a DB error.
"""
from typing import Optional

from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_KEY, logger

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# --------------------------------------------------------------------------- #
# Users / whitelist
# --------------------------------------------------------------------------- #
def get_user(instagram_id: str) -> Optional[dict]:
    """Return the user row for an Instagram ID (IGSID), or None if not whitelisted."""
    try:
        res = (
            supabase.table("users")
            .select("*")
            .eq("instagram_id", instagram_id)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception:
        logger.exception("get_user failed for %s", instagram_id)
        return None


def is_whitelisted(instagram_id: str) -> bool:
    return get_user(instagram_id) is not None


def get_all_users() -> list[dict]:
    """All group members. Used as the default split set and for name lookups."""
    try:
        res = supabase.table("users").select("*").execute()
        return res.data or []
    except Exception:
        logger.exception("get_all_users failed")
        return []


def get_name(instagram_id: str, fallback: Optional[str] = None) -> str:
    user = get_user(instagram_id)
    if user:
        return user["name"]
    return fallback or instagram_id


def record_unknown_sender(instagram_id: str) -> None:
    """Persist the IGSID of a non-whitelisted DMer so we can whitelist them later.

    Insert-ignore on the PK: the first sighting is kept, retries/duplicates are
    no-ops. Never raises — a failure here must not break the webhook.
    """
    try:
        supabase.table("unknown_senders").insert(
            {"instagram_id": instagram_id}
        ).execute()
        logger.info("Recorded new unknown sender %s.", instagram_id)
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "23505" in str(exc):
            return  # already recorded — fine
        logger.exception("record_unknown_sender failed for %s", instagram_id)


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #
def insert_transaction(
    payer_id: str,
    amount: float,
    purpose: str,
    date: str,
    is_personal: bool = False,
) -> Optional[dict]:
    """Record an expense. Returns the inserted row, or None on failure."""
    try:
        res = (
            supabase.table("transactions")
            .insert(
                {
                    "payer_id": payer_id,
                    "amount": amount,
                    "purpose": purpose,
                    "date": date,
                    "is_personal": is_personal,
                }
            )
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception:
        logger.exception("insert_transaction failed for %s", payer_id)
        return None


def get_personal_spend_totals() -> dict[str, float]:
    """Map of payer_id -> total amount they spent on themselves."""
    totals: dict[str, float] = {}
    try:
        res = (
            supabase.table("transactions")
            .select("payer_id, amount")
            .eq("is_personal", True)
            .execute()
        )
        for row in res.data or []:
            totals[row["payer_id"]] = totals.get(row["payer_id"], 0.0) + float(
                row["amount"]
            )
    except Exception:
        logger.exception("get_personal_spend_totals failed")
    return totals


# --------------------------------------------------------------------------- #
# Balances matrix (raw accumulate)
# --------------------------------------------------------------------------- #
def add_debt(user_who_owes: str, user_who_is_owed: str, amount: float) -> bool:
    """Increment net_balance for a (owes, owed) pair, upserting the row.

    Raw-accumulate model: no bidirectional netting. If A already owes B, this
    adds to the existing figure; a fresh pair starts at `amount`.
    """
    if user_who_owes == user_who_is_owed or amount <= 0:
        return True  # nothing to record
    try:
        existing = (
            supabase.table("balances")
            .select("id, net_balance")
            .eq("user_who_owes", user_who_owes)
            .eq("user_who_is_owed", user_who_is_owed)
            .execute()
        )
        if existing.data:
            row = existing.data[0]
            new_balance = round(float(row["net_balance"]) + amount, 2)
            supabase.table("balances").update({"net_balance": new_balance}).eq(
                "id", row["id"]
            ).execute()
        else:
            supabase.table("balances").insert(
                {
                    "user_who_owes": user_who_owes,
                    "user_who_is_owed": user_who_is_owed,
                    "net_balance": round(amount, 2),
                }
            ).execute()
        return True
    except Exception:
        logger.exception(
            "add_debt failed: %s owes %s (%s)", user_who_owes, user_who_is_owed, amount
        )
        return False


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
def claim_message(message_id: str) -> bool:
    """Atomically claim a message id for processing.

    Returns True if this is the first time we've seen it (caller should process),
    False if it was already handled (a WhatsApp retry — caller should skip).
    On DB error we fail OPEN (return True) so a real message is never dropped;
    the small double-log risk on an outage is preferable to silently losing spends.
    """
    try:
        supabase.table("processed_messages").insert(
            {"message_id": message_id}
        ).execute()
        return True
    except Exception as exc:
        # A primary-key violation means we've already processed this id.
        if "duplicate" in str(exc).lower() or "23505" in str(exc):
            logger.info("Skipping duplicate message id %s", message_id)
            return False
        logger.exception("claim_message errored for %s; failing open", message_id)
        return True


def get_debt(user_who_owes: str, user_who_is_owed: str) -> float:
    """How much user_who_owes currently owes user_who_is_owed (0 if none)."""
    try:
        res = (
            supabase.table("balances")
            .select("net_balance")
            .eq("user_who_owes", user_who_owes)
            .eq("user_who_is_owed", user_who_is_owed)
            .execute()
        )
        return float(res.data[0]["net_balance"]) if res.data else 0.0
    except Exception:
        logger.exception("get_debt failed: %s -> %s", user_who_owes, user_who_is_owed)
        return 0.0


def set_debt(user_who_owes: str, user_who_is_owed: str, value: float) -> bool:
    """Set the (owes, owed) balance to an absolute value; delete the row at <= 0."""
    value = round(value, 2)
    try:
        if value <= 0:
            supabase.table("balances").delete().eq(
                "user_who_owes", user_who_owes
            ).eq("user_who_is_owed", user_who_is_owed).execute()
            return True
        existing = (
            supabase.table("balances")
            .select("id")
            .eq("user_who_owes", user_who_owes)
            .eq("user_who_is_owed", user_who_is_owed)
            .execute()
        )
        if existing.data:
            supabase.table("balances").update({"net_balance": value}).eq(
                "id", existing.data[0]["id"]
            ).execute()
        else:
            supabase.table("balances").insert(
                {
                    "user_who_owes": user_who_owes,
                    "user_who_is_owed": user_who_is_owed,
                    "net_balance": value,
                }
            ).execute()
        return True
    except Exception:
        logger.exception("set_debt failed: %s -> %s", user_who_owes, user_who_is_owed)
        return False


def get_all_balances() -> list[dict]:
    """All non-zero balance rows."""
    try:
        res = (
            supabase.table("balances")
            .select("*")
            .gt("net_balance", 0)
            .execute()
        )
        return res.data or []
    except Exception:
        logger.exception("get_all_balances failed")
        return []
