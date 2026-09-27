"""Phase 5: turn your 👍-rated replies into training examples, and budget the run.

Fine-tuning teaches tone and style. Facts stay in the encrypted memory. Turns that
look like they contain secrets (long numbers, emails) are left out, so the model
cannot learn to repeat them.
"""
import re
from datetime import datetime

import config
from budget import Meter, minutes_until_stop
from llm import system_prompt

_LOOKS_PRIVATE = re.compile(
    r"\d{4,}"                       # PINs, OTPs, account/phone numbers, card digits
    r"|\d[\d\s-]{6,}\d"             # numbers written with spaces or dashes
    r"|[\w.+-]+@[\w-]+\.[\w.]+"     # email addresses
    r"|password|पासवर्ड|pin\b|otp\b",
    re.IGNORECASE,
)


def looks_private(text: str) -> bool:
    return bool(_LOOKS_PRIVATE.search(text))


def build_examples(good_turns: list[tuple[str, str, str, str]]) -> list[list[dict]]:
    """(ts, lang, user_text, reply) rows -> chat examples in the same format the
    assistant uses at run time (system prompt without personal facts)."""
    examples = []
    for ts, lang, user_text, reply in good_turns:
        if not user_text.strip() or not reply.strip():
            continue
        if looks_private(user_text) or looks_private(reply):
            continue
        when = datetime.fromisoformat(ts)
        examples.append([
            {"role": "system", "content": system_prompt(lang or "en", [], [], when)},
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": reply},
        ])
    return examples


def training_minutes_allowed(meters: list[Meter], reserve_min: int = 15) -> int:
    """GPU minutes training may use before the 99 % auto-stop, keeping `reserve_min`
    for loading, exporting and encrypting the model."""
    lefts = [left for m in meters if (left := minutes_until_stop(m, config.STOP_AT)) is not None]
    return max(0, min(lefts) - reserve_min) if lefts else 0
