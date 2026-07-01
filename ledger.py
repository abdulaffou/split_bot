"""Split ledger math engine + settle-up + human-readable summaries.

Ties parser output to the database: works out who shares an expense, records the
transaction, updates the balances matrix (raw accumulate, payer included), and
handles paybacks. Ambiguous or unknown names are never guessed — the user is
asked to clarify instead, so a mis-typed name can't silently corrupt balances.
"""
import database as db
from parser import ParsedMessage
from config import logger


def _resolve_names(
    names: list[str], all_users: list[dict]
) -> tuple[list[str], list[str], list[tuple[str, list[str]]]]:
    """Map free-text names to phone numbers.

    Returns (matched_phones, unmatched_names, ambiguous) where `ambiguous` is a
    list of (typed_name, [candidate display names]). Exact (case-insensitive)
    matches win over substring matches.
    """
    matched: list[str] = []
    unmatched: list[str] = []
    ambiguous: list[tuple[str, list[str]]] = []

    for name in names:
        needle = name.strip().lower()
        if not needle:
            continue
        exact = [u for u in all_users if u["name"].lower() == needle]
        if len(exact) == 1:
            matched.append(exact[0]["phone_number"])
            continue
        if len(exact) > 1:
            ambiguous.append((name, [u["name"] for u in exact]))
            continue
        subs = [
            u
            for u in all_users
            if needle in u["name"].lower() or u["name"].lower() in needle
        ]
        if len(subs) == 1:
            matched.append(subs[0]["phone_number"])
        elif len(subs) > 1:
            ambiguous.append((name, [u["name"] for u in subs]))
        else:
            unmatched.append(name)

    return matched, unmatched, ambiguous


def _clarify_message(unmatched: list[str], ambiguous: list[tuple[str, list[str]]]) -> str:
    """Build a 'please clarify' reply; returns '' if nothing to clarify."""
    parts: list[str] = []
    if ambiguous:
        for typed, cands in ambiguous:
            parts.append(f"'{typed}' could be: {', '.join(cands)}")
    if unmatched:
        parts.append(f"I don't recognise: {', '.join(unmatched)}")
    if not parts:
        return ""
    return (
        "I wasn't sure who you meant.\n"
        + "\n".join(f"• {p}" for p in parts)
        + "\nPlease resend using full names."
    )


def process_expense(payer_phone: str, parsed: ParsedMessage) -> str:
    """Record an expense and update balances. Returns the reply text for the payer."""
    if parsed.amount <= 0:
        return "I couldn't read an amount from that. Try e.g. '1200 dinner yesterday'."

    payer_name = db.get_name(payer_phone)

    # --- Personal spend: recorded, never split. ---
    if parsed.is_personal:
        row = db.insert_transaction(
            payer_phone, parsed.amount, parsed.purpose, parsed.date, is_personal=True
        )
        if not row:
            return "Something went wrong saving that. Please try again."
        return (
            f"Noted your personal spend: ₹{parsed.amount:.2f} on "
            f"{parsed.purpose or 'something'} ({parsed.date}). Not split."
        )

    # --- Shared spend: figure out the participant set. ---
    all_users = db.get_all_users()

    if parsed.split_everyone or not parsed.participants:
        participant_phones = [u["phone_number"] for u in all_users]
    else:
        matched, unmatched, ambiguous = _resolve_names(parsed.participants, all_users)
        clarify = _clarify_message(unmatched, ambiguous)
        if clarify:  # don't log a mis-parsed split — ask first.
            return clarify
        participant_phones = matched
        if payer_phone not in participant_phones:  # payer always shares
            participant_phones.append(payer_phone)

    # Deduplicate while preserving order.
    participant_phones = list(dict.fromkeys(participant_phones))

    if len(participant_phones) < 2:
        return (
            "I couldn't work out who to split this with. "
            "Try naming the people, e.g. '900 cab with Diya and Esha'."
        )

    row = db.insert_transaction(
        payer_phone, parsed.amount, parsed.purpose, parsed.date, is_personal=False
    )
    if not row:
        return "Something went wrong saving that. Please try again."

    split = round(parsed.amount / len(participant_phones), 2)
    failures = 0
    for phone in participant_phones:
        if phone == payer_phone:
            continue  # payer covers own share
        if not db.add_debt(phone, payer_phone, split):
            failures += 1

    others = len(participant_phones) - 1
    names = ", ".join(db.get_name(p) for p in participant_phones if p != payer_phone)
    reply = (
        f"Logged ₹{parsed.amount:.2f} for {parsed.purpose or 'expense'} ({parsed.date}).\n"
        f"Split {len(participant_phones)} ways = ₹{split:.2f} each.\n"
        f"{others} owe {payer_name} ₹{split:.2f} each ({names})."
    )
    if failures:
        reply += f"\n(Warning: {failures} balance update(s) failed — check with the group.)"
    return reply


def process_settle(payer_phone: str, parsed: ParsedMessage) -> str:
    """Record a payback / settle-up between the sender and one other person."""
    all_users = db.get_all_users()
    matched, unmatched, ambiguous = _resolve_names([parsed.settle_target], all_users)
    clarify = _clarify_message(unmatched, ambiguous)
    if clarify:
        return clarify
    if not matched:
        return "Who did you settle with? Try 'settled with Diya' or 'paid Diya 500'."

    target = matched[0]
    if target == payer_phone:
        return "You can't settle with yourself."

    payer_name = db.get_name(payer_phone)
    target_name = db.get_name(target)
    amount = parsed.settle_amount

    # --- Full settle: clear the balance both ways between the two. ---
    if amount <= 0:
        owed_by_payer = db.get_debt(payer_phone, target)
        owed_to_payer = db.get_debt(target, payer_phone)
        if owed_by_payer <= 0 and owed_to_payer <= 0:
            return f"You and {target_name} are already settled up. 🎉"
        ok1 = db.set_debt(payer_phone, target, 0)
        ok2 = db.set_debt(target, payer_phone, 0)
        if not (ok1 and ok2):
            return "Something went wrong settling that. Please try again."
        return f"All settled between {payer_name} and {target_name}. 🎉"

    # --- Partial payback: the sender paid `amount` toward what they owe target. ---
    owed_by_payer = db.get_debt(payer_phone, target)
    if owed_by_payer <= 0:
        return (
            f"You don't currently owe {target_name} anything, so there's nothing to "
            f"pay back. (Use 'settled with {target_name}' to clear the whole balance.)"
        )
    applied = min(amount, owed_by_payer)
    remaining = round(owed_by_payer - applied, 2)
    if not db.set_debt(payer_phone, target, remaining):
        return "Something went wrong recording that. Please try again."

    reply = f"Recorded: {payer_name} paid {target_name} ₹{applied:.2f}."
    if remaining > 0:
        reply += f" You still owe {target_name} ₹{remaining:.2f}."
    else:
        reply += f" You're now settled with {target_name}. 🎉"
    if amount > owed_by_payer:
        reply += (
            f"\n(You only owed ₹{owed_by_payer:.2f}; the extra "
            f"₹{amount - owed_by_payer:.2f} was ignored.)"
        )
    return reply


def render_status() -> str:
    """Human-readable breakdown of who owes whom + personal spend totals."""
    balances = db.get_all_balances()
    personal = db.get_personal_spend_totals()

    lines: list[str] = ["*SplitBot summary*"]

    if balances:
        lines.append("\n_Who owes whom:_")
        for b in balances:
            amt = float(b["net_balance"])
            if amt <= 0:
                continue
            ower = db.get_name(b["user_who_owes"])
            owed = db.get_name(b["user_who_is_owed"])
            lines.append(f"• {ower} owes {owed} ₹{amt:.2f}")
    else:
        lines.append("\nNo outstanding balances. All settled! 🎉")

    if personal:
        lines.append("\n_Personal spends (not split):_")
        for phone, total in sorted(personal.items(), key=lambda x: -x[1]):
            lines.append(f"• {db.get_name(phone)}: ₹{total:.2f}")

    return "\n".join(lines)


HELP_TEXT = (
    "*SplitBot* 🤝\n"
    "• Log a shared spend: '1200 dinner yesterday'\n"
    "• Split with some: '900 cab with Diya and Esha'\n"
    "• Personal (not split): 'my coffee 150'\n"
    "• Pay someone back: 'paid Diya 500' or 'settled with Diya'\n"
    "• See balances: 'status' or 'summary'"
)
