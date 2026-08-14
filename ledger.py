"""Split ledger math engine + settle-up + human-readable summaries.

Ties parser output to the database: works out who shares an expense, records the
transaction, updates the balances matrix (raw accumulate, payer included), and
handles paybacks. Ambiguous or unknown names are never guessed — the user is
asked to clarify instead, so a mis-typed name can't silently corrupt balances.
"""
import database as db
from parser import ParsedMessage
from instagram import send_text
from config import logger


def _resolve_names(
    names: list[str], all_users: list[dict]
) -> tuple[list[str], list[str], list[tuple[str, list[str]]]]:
    """Map free-text names to Instagram IDs (IGSIDs).

    Returns (matched_ids, unmatched_names, ambiguous) where `ambiguous` is a
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
            matched.append(exact[0]["instagram_id"])
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
            matched.append(subs[0]["instagram_id"])
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


def process_expense(sender_id: str, parsed: ParsedMessage) -> str:
    """Record an expense and update balances. Returns the reply text for the sender."""
    if parsed.amount <= 0:
        return "I couldn't read an amount from that. Try e.g. '1200 dinner yesterday'."

    all_users = db.get_all_users()

    # --- Who actually paid: the sender, unless a different name was given. ---
    payer_id = sender_id
    if parsed.payer_name:
        matched, unmatched, ambiguous = _resolve_names([parsed.payer_name], all_users)
        clarify = _clarify_message(unmatched, ambiguous)
        if clarify:
            return clarify
        payer_id = matched[0]

    payer_name = db.get_name(payer_id)

    # --- Personal spend: recorded, never split. ---
    if parsed.is_personal:
        row = db.insert_transaction(
            payer_id, parsed.amount, parsed.purpose, parsed.date, is_personal=True
        )
        if not row:
            return "Something went wrong saving that. Please try again."
        whose = "your" if payer_id == sender_id else f"{payer_name}'s"
        return (
            f"Noted {whose} personal spend: ₹{parsed.amount:.2f} on "
            f"{parsed.purpose or 'something'} ({parsed.date}). Not split."
        )

    # --- Shared spend: figure out the participant set. ---
    if parsed.split_everyone or not parsed.participants:
        participant_ids = [u["instagram_id"] for u in all_users]
    else:
        matched, unmatched, ambiguous = _resolve_names(parsed.participants, all_users)
        clarify = _clarify_message(unmatched, ambiguous)
        if clarify:  # don't log a mis-parsed split — ask first.
            return clarify
        participant_ids = matched
        if payer_id not in participant_ids:  # payer always shares
            participant_ids.append(payer_id)

    # Deduplicate while preserving order.
    participant_ids = list(dict.fromkeys(participant_ids))

    if len(participant_ids) < 2:
        return (
            "I couldn't work out who to split this with. "
            "Try naming the people, e.g. '900 cab with Diya and Esha'."
        )

    row = db.insert_transaction(
        payer_id, parsed.amount, parsed.purpose, parsed.date, is_personal=False
    )
    if not row:
        return "Something went wrong saving that. Please try again."

    split = round(parsed.amount / len(participant_ids), 2)
    failures = 0
    for pid in participant_ids:
        if pid == payer_id:
            continue  # payer covers own share
        if not db.add_debt(pid, payer_id, split):
            failures += 1

    others = len(participant_ids) - 1
    names = ", ".join(db.get_name(p) for p in participant_ids if p != payer_id)
    reply = (
        f"Logged ₹{parsed.amount:.2f} for {parsed.purpose or 'expense'} ({parsed.date}).\n"
        f"Split {len(participant_ids)} ways = ₹{split:.2f} each.\n"
        f"{others} owe {payer_name} ₹{split:.2f} each ({names})."
    )
    if failures:
        reply += f"\n(Warning: {failures} balance update(s) failed — check with the group.)"
    return reply


def process_settle(payer_id: str, parsed: ParsedMessage) -> str:
    """Record a payback / settle-up between the sender and one or more other people."""
    all_users = db.get_all_users()
    matched, unmatched, ambiguous = _resolve_names(parsed.settle_targets, all_users)
    clarify = _clarify_message(unmatched, ambiguous)
    if clarify:
        return clarify
    if not matched:
        return "Who did you settle with? Try 'settled with Diya' or 'paid Diya 500'."

    matched = list(dict.fromkeys(matched))
    if payer_id in matched:
        return "You can't settle with yourself."

    payer_name = db.get_name(payer_id)
    amount = parsed.settle_amount

    # --- Multiple people + an unsplit total: don't guess how it divides. ---
    if len(matched) > 1 and amount > 0:
        names = " and ".join(db.get_name(t) for t in matched)
        return (
            f"How much did you pay each of {names}? "
            f"Try settling with them one at a time, e.g. 'paid Diya 500'."
        )

    if len(matched) == 1 and amount > 0:
        return _settle_partial(payer_id, matched[0], amount)

    # --- Full settle: clear the balance both ways with each named person. ---
    settled, already, failed = [], [], []
    for target in matched:
        target_name = db.get_name(target)
        owed_by_payer = db.get_debt(payer_id, target)
        owed_to_payer = db.get_debt(target, payer_id)
        if owed_by_payer <= 0 and owed_to_payer <= 0:
            already.append(target_name)
            continue
        ok1 = db.set_debt(payer_id, target, 0)
        ok2 = db.set_debt(target, payer_id, 0)
        if not (ok1 and ok2):
            failed.append(target_name)
            continue
        settled.append((target, target_name))

    not_notified = []
    for target, target_name in settled:
        if not send_text(target, f"{payer_name} settled up with you. You're all clear! 🎉"):
            not_notified.append(target_name)

    if not settled and not already:
        return "Something went wrong settling that. Please try again."

    lines = []
    if settled:
        lines.append(f"Settled with {', '.join(n for _, n in settled)}. 🎉")
    if already:
        lines.append(f"Already settled with {', '.join(already)}.")
    if failed:
        lines.append(f"(Couldn't settle with {', '.join(failed)} — please try again.)")
    if not_notified:
        lines.append(
            f"({', '.join(not_notified)} couldn't be notified — "
            f"ask them to message me first.)"
        )
    return "\n".join(lines)


def _settle_partial(payer_id: str, target: str, amount: float) -> str:
    """Partial payback: the sender paid `amount` toward what they owe target."""
    payer_name = db.get_name(payer_id)
    target_name = db.get_name(target)
    owed_by_payer = db.get_debt(payer_id, target)
    if owed_by_payer <= 0:
        return (
            f"You don't currently owe {target_name} anything, so there's nothing to "
            f"pay back. (Use 'settled with {target_name}' to clear the whole balance.)"
        )
    applied = min(amount, owed_by_payer)
    remaining = round(owed_by_payer - applied, 2)
    if not db.set_debt(payer_id, target, remaining):
        return "Something went wrong recording that. Please try again."

    reply = f"Recorded: {payer_name} paid {target_name} ₹{applied:.2f}."
    if remaining > 0:
        reply += f" You still owe {target_name} ₹{remaining:.2f}."
    else:
        reply += f" You're now settled with {target_name}. 🎉"
        if not send_text(target, f"{payer_name} settled up with you. You're all clear! 🎉"):
            reply += f"\n({target_name} couldn't be notified — ask them to message me first.)"
    if amount > owed_by_payer:
        reply += (
            f"\n(You only owed ₹{owed_by_payer:.2f}; the extra "
            f"₹{amount - owed_by_payer:.2f} was ignored.)"
        )
    return reply


def render_status(viewer_id: str) -> str:
    """Personalized summary for the requester only.

    Shows what *they* owe, what *they* are owed, their net position, and their
    OWN personal spend total — never other people's balances or personal spends.
    """
    balances = db.get_all_balances()

    you_owe = []       # (other_id, amount) — viewer owes other
    owed_to_you = []   # (other_id, amount) — other owes viewer
    for b in balances:
        amt = float(b["net_balance"])
        if amt <= 0:
            continue
        if b["user_who_owes"] == viewer_id:
            you_owe.append((b["user_who_is_owed"], amt))
        elif b["user_who_is_owed"] == viewer_id:
            owed_to_you.append((b["user_who_owes"], amt))

    total_you_owe = round(sum(a for _, a in you_owe), 2)
    total_owed_to_you = round(sum(a for _, a in owed_to_you), 2)
    net = round(total_owed_to_you - total_you_owe, 2)

    my_personal = db.get_personal_spend_totals().get(viewer_id, 0.0)

    lines: list[str] = ["*Your SplitBot summary*"]

    if you_owe:
        lines.append("\n_You owe:_")
        for oid, amt in sorted(you_owe, key=lambda x: -x[1]):
            lines.append(f"• {db.get_name(oid)}: ₹{amt:.2f}")
        lines.append(f"Total you owe: ₹{total_you_owe:.2f}")

    if owed_to_you:
        lines.append("\n_Owed to you:_")
        for oid, amt in sorted(owed_to_you, key=lambda x: -x[1]):
            lines.append(f"• {db.get_name(oid)}: ₹{amt:.2f}")
        lines.append(f"Total owed to you: ₹{total_owed_to_you:.2f}")

    if you_owe or owed_to_you:
        if net > 0:
            lines.append(f"\n*Net: you're owed ₹{net:.2f}.*")
        elif net < 0:
            lines.append(f"\n*Net: you owe ₹{-net:.2f}.*")
        else:
            lines.append("\n*Net: you're even. 🎉*")
    else:
        lines.append("\nYou have no outstanding balances. All settled! 🎉")

    if my_personal > 0:
        lines.append(f"\n_Your personal spends (not split):_ ₹{my_personal:.2f}")

    return "\n".join(lines)


HELP_TEXT = (
    "*SplitBot* 🤝\n"
    "• Log a shared spend: '1200 dinner yesterday'\n"
    "• Split with some: '900 cab with Diya and Esha'\n"
    "• Personal (not split): 'my coffee 150'\n"
    "• Pay someone back: 'paid Diya 500' or 'settled with Diya'\n"
    "• Settle with several people: 'paid everything to Diya and Esha'\n"
    "• See balances: 'status' or 'summary'"
)
