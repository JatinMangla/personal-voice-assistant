"""Learn lasting facts about you from normal conversation (no "remember that" needed).

After each turn, the LLM reads what YOU said and writes down facts that will still be
true in months: allergies, diet, health, family, likes, work... Health and diet facts
are then always in front of the assistant, so e.g. a recipe that contains something
you're allergic to gets a warning, even months later. Your corrections of the assistant
("you can't work without internet") are kept the same way and always followed.

Guards: only your own words are used (never the assistant's or web text); anything
that looks private (PINs, OTPs, card/phone numbers, passwords, emails) is dropped;
near-duplicates are skipped; a changed fact replaces the old one. "Forget that ..."
removes learned facts like any other.
"""
import json
import re
from datetime import datetime

import requests

import config
from finetune_data import looks_private

CATEGORIES = ["health", "diet", "person", "place", "work", "preference", "routine", "goal",
              "decision", "event", "assistant", "pattern", "other"]

# Worth an extra LLM call only if the user talked about themselves ("I", "my") or
# told the assistant something about itself ("you", "your") - e.g. a correction.
_PERSONAL = re.compile(
    r"\b(i|i'm|im|i've|i'd|i'll|my|me|mine|we|our|us|you|you're|your|yours)\b"
    r"|मैं|मेरा|मेरी|मेरे|मुझे|मुझको|हम|हमारा|हमारी|हमें|तुम|तुम्हें|तुम्हारा|आप|आपको|आपका"
    r"|\b(main|mera|meri|mere|mujhe|hum|hamara|hamari|hume|tum|tumhe|tumhara|aap|aapko|aapka)\b",
    re.IGNORECASE,
)

EXTRACT_PROMPT = """You keep the long-term memory of a personal voice assistant. Decide by
yourself what is worth remembering about the user from their message, and extract it:
- details: name, family and people, home and places, work, study, projects;
- health (allergies, conditions, medicines) and diet (vegetarian, foods they avoid);
- preferences, likes and dislikes, and settings they want from the assistant;
- decisions they made ("decided to switch to a new job");
- actions and events worth remembering, with the date if known ("bought a Honda City in
  September 2026", "started going to the gym");
- goals and plans, habits and routines, and what they focus on in their work.

When the user corrects the assistant or tells it how it works or how it should behave
(for example "you can't talk to me without internet", "don't call me sir", "keep answers
short", "reply in Hindi"), save that with category "assistant", written as a fact or
instruction about the assistant. Example: "The assistant needs internet; it cannot work offline."

Rules:
- Only what the user clearly says. No questions, guesses, small talk, or things that only
  matter for a moment (today's mood, "I'm eating now"). Ignore what the assistant said.
- Turn relative dates ("yesterday", "next month") into real dates using today's date.
- Write each fact in short English, third person. Example: "User is allergic to peanuts."
- Never include passwords, PINs, OTPs, card, bank or phone numbers, or exact addresses.
- If a fact changes a known fact, set "replaces" to that known fact's number; otherwise 0.
- If there is nothing lasting, return an empty list."""

SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "replaces": {"type": "integer"},  # known fact number, 0 = none
                },
                "required": ["fact", "category", "replaces"],
            },
        }
    },
    "required": ["facts"],
}


def worth_checking(user_text: str) -> bool:
    """Skip the extra LLM call unless the user talked about themselves."""
    return len(user_text.split()) >= 3 and bool(_PERSONAL.search(user_text))


REFLECT_PROMPT = """You keep the long-term memory of a personal voice assistant. Below are the
user's recent messages. Write down up to 5 lasting PATTERNS about the user that are clearly
supported by several messages: what they focus on most (in work and life), recurring
interests and topics, habits, how they like to get help (short answers, step by step,
Hindi or English), and decisions they keep coming back to.

Rules:
- Only patterns backed by the messages; no guesses about personality or feelings.
- Skip anything already in the known facts (or set "replaces" to update one; otherwise 0).
- Never include passwords, PINs, OTPs, card, bank or phone numbers, or exact addresses.
- Short English, third person, category "pattern". Example:
  "User often asks for help with their Kaggle voice assistant project."
- If there is no clear pattern, return an empty list."""


def _ask_llm(user_text: str, known: list[str], now: datetime | None = None,
             prompt: str = EXTRACT_PROMPT, label: str = "User's message") -> list[dict]:
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(known, 1)) or "(none)"
    today = f"Today is {now:%d %B %Y}.\n\n" if now else ""
    resp = requests.post(f"{config.OLLAMA_URL}/api/chat", timeout=(5, 90), json={
        "model": config.LLM_MODEL,
        "messages": [{"role": "system", "content": prompt},
                     {"role": "user", "content": f"{today}Known facts:\n{numbered}\n\n"
                                                 f"{label}:\n{user_text}"}],
        "stream": False,
        "think": False,
        "format": SCHEMA,
        "keep_alive": "60m",
        # same num_ctx as every other request: a different one makes Ollama reload the model
        "options": {"temperature": 0, "num_predict": 300, "num_ctx": config.LLM_CONTEXT_TOKENS},
    })
    resp.raise_for_status()
    return json.loads(resp.json()["message"]["content"]).get("facts", [])


def _store(memory, items: list[dict], known: list[tuple[int, str, float]], now: datetime,
           limit: int, force_category: str | None = None) -> list[str]:
    """Save extracted facts: skip private or already-known ones, replace changed ones."""
    stored = []
    for item in items[:limit]:
        fact = str(item.get("fact", "")).strip()[:200]
        category = force_category or (item.get("category") if item.get("category") in CATEGORIES
                                      else "other")
        if not fact or looks_private(fact):
            continue
        replaces = item.get("replaces")
        if isinstance(replaces, int) and 1 <= replaces <= len(known):
            memory.delete_fact(known[replaces - 1][0])  # e.g. "now vegetarian" replaces old diet
        else:
            nearest = memory.similar_facts(fact, 1)
            if nearest and nearest[0][2] >= config.LEARN_DUPLICATE_SIMILARITY:
                continue  # already known
        memory.add_fact(fact, now, category=category, source="auto")
        stored.append(fact)
    return stored


def learn(memory, user_text: str, now: datetime) -> list[str]:
    """Extract and store new facts. Returns the facts learned (to show on the page)."""
    if not worth_checking(user_text) or looks_private(user_text):
        return []
    known = memory.similar_facts(user_text, config.LEARN_KNOWN_FACTS)
    items = _ask_llm(user_text, [text for _, text, _ in known], now)
    return _store(memory, items, known, now, limit=4)


def reflect(memory, now: datetime) -> list[str]:
    """Look back over recent messages and store PATTERNS: what the user focuses on,
    recurring interests, habits, how they like help. Runs every few conversations and
    when a session ends. Returns the patterns stored."""
    rows = memory.unreflected_turns(config.REFLECT_MAX_MESSAGES)
    if len(rows) < config.REFLECT_MIN_MESSAGES:
        return []
    messages = [text for _, text in rows if text and not looks_private(text)]
    joined = "\n".join(f"- {m[:300]}" for m in messages)
    known = memory.similar_facts(joined[:2000], config.LEARN_KNOWN_FACTS * 2)
    items = _ask_llm(joined, [text for _, text, _ in known], now, prompt=REFLECT_PROMPT,
                     label="User's recent messages")
    stored = _store(memory, items, known, now, limit=5, force_category="pattern")
    memory.mark_reflected([turn_id for turn_id, _ in rows])
    return stored
