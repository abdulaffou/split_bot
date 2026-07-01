"""AI parsing layer. Turns free-text WhatsApp messages into structured intents.

Uses a Gemini Flash model (see config.GEMINI_MODEL) with a Pydantic-defined
structured-output schema so the model is forced to return valid JSON matching
`ParsedMessage`.
"""
from datetime import date as date_cls
from typing import Optional

import google.generativeai as genai
from pydantic import BaseModel, Field

from config import GEMINI_API_KEY, GEMINI_MODEL, logger

genai.configure(api_key=GEMINI_API_KEY)

# Valid values for ParsedMessage.intent.
INTENTS = {"expense", "settle", "status", "other"}


class ParsedMessage(BaseModel):
    """Structured result of parsing one message.

    Every field is REQUIRED (no defaults). The Gemini SDK's schema converter
    rejects Pydantic `default`s ("Unknown field for Schema: default"), and marking
    all fields required also forces the model to always return them.
    """

    intent: str = Field(
        description="One of: 'expense' (logs a spend), 'settle' (records a payback/settle-up), 'status' (asks for balances/summary), 'other' (greeting/help/anything else)."
    )
    # --- expense fields ---
    amount: float = Field(
        description="Amount spent for an expense. 0 if not an expense."
    )
    purpose: str = Field(
        description="Short description of the expense. Empty string if not an expense."
    )
    date: str = Field(
        description="Expense date as YYYY-MM-DD. Empty string if not an expense."
    )
    is_personal: bool = Field(
        description="True if the payer bought this only for themselves (e.g. 'my coffee'). Personal spends are NOT split."
    )
    split_everyone: bool = Field(
        description="True if the expense is shared by the whole group. False if only specific named people share it."
    )
    participants: list[str] = Field(
        description="Names sharing an expense when split_everyone is False. Empty otherwise. Do NOT include the payer; the app adds them."
    )
    # --- settle fields ---
    settle_target: str = Field(
        description="For a 'settle' intent, the name of the other person being settled with. Empty otherwise."
    )
    settle_amount: float = Field(
        description="For a 'settle' intent, the amount paid back. Use 0 to settle the whole balance with that person."
    )


def _build_prompt(text: str, today: str, known_names: list[str]) -> str:
    names = ", ".join(known_names) if known_names else "(none)"
    return f"""You are the parsing engine for SplitBot, a shared-expense tracker.

Today's date is {today}. Use it to resolve relative dates like "yesterday",
"last Friday", or "2 days ago". If a message gives no date, use today ({today}).
Always output the date as YYYY-MM-DD.

The group members are: {names}.

Set `intent` to exactly one of:
- "expense": the message records money spent (e.g. "1200 dinner yesterday").
- "settle": the sender paid someone back or wants to clear a balance
  (e.g. "settled with Diya", "paid Aarav 500", "cleared up with Esha").
- "status": the message asks for balances/summary (e.g. "status", "who owes me").
- "other": greeting, help, or anything else.

For intent = "expense", fill amount, purpose, date and:
- If bought only for the payer ("my", "just for me", "personal"), set
  is_personal = true and split_everyone = false.
- If shared by the whole group, set split_everyone = true, participants = [].
- If only specific people share it (e.g. "dinner with Aarav and Diya"), set
  split_everyone = false, is_personal = false, list those names in participants.
  Do NOT include the payer; the app adds them.

For intent = "settle", set settle_target to the other person's name and
settle_amount to the amount paid back (use 0 to settle the entire balance).

Leave all fields not relevant to the chosen intent at empty/zero values.

Message to parse:
\"\"\"{text}\"\"\""""


def parse_message(
    text: str, known_names: Optional[list[str]] = None
) -> Optional[ParsedMessage]:
    """Parse a message into a ParsedMessage. Returns None on total AI failure."""
    today = date_cls.today().isoformat()
    known_names = known_names or []
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content(
            _build_prompt(text, today, known_names),
            generation_config={
                "response_mime_type": "application/json",
                # Pass the Pydantic model directly; the SDK converts it to a
                # Gemini schema. (A raw JSON-schema dict is NOT accepted here.)
                "response_schema": ParsedMessage,
                "temperature": 0.0,
            },
        )
        parsed = ParsedMessage.model_validate_json(response.text)

        # Guardrails.
        if parsed.intent not in INTENTS:
            parsed.intent = "other"
        if parsed.intent == "expense" and not parsed.date:
            parsed.date = today
        if parsed.is_personal:
            parsed.split_everyone = False
            parsed.participants = []
        return parsed
    except Exception:
        logger.exception("parse_message failed for text=%r", text)
        return None
