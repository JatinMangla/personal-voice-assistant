"""Learn lasting facts about you from normal conversation (no "remember that" needed).

After each turn, the LLM reads what YOU said and writes down facts that will still be
true in months: allergies, diet, health, family, likes, work... Health and diet facts
are then always in front of the assistant, so e.g. a recipe that contains something
you're allergic to gets a warning, even months later.

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

CATEGORIES = ["health", "diet", "person", "place", "work", "preference", "routine", "other"]

_FIRST_PERSON = re.compile(
    r"\b(i|i'm|im|i've|i'd|i'll|my|me|mine|we|our|us)\b"
    r"|मैं|मेरा|मेरी|मेरे|मुझे|मुझको|हम|हमारा|हमारी|हमें"
    r"|\b(main|mera|meri|mere|mujhe|hum|hamara|hamari|hume)\b",
    re.IGNORECASE,
)

EXTRACT_PROMPT = """You keep a list of lasting facts about the user of a personal assistant.
From the user's message, extract facts that will most likely still be true in months:
health (allergies, conditions, medicines), diet (vegetarian, foods they avoid), family and
people, home and places, work and study, likes and dislikes, routines and goals.

Rules:
- Only what the user clearly says about themselves or their family. No questions, guesses,
  or temporary things (today's mood, what they are doing right now).
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
    return len(user_text.split()) >= 3 and bool(_FIRST_PERSON.search(user_text))


def _ask_llm(user_text: str, known: list[str]) -> list[dict]:
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(known, 1)) or "(none)"
    resp = requests.post(f"{config.OLLAMA_URL}/api/chat", timeout=(5, 60), json={
        "model": config.LLM_MODEL,
        "messages": [{"role": "system", "content": EXTRACT_PROMPT},
                     {"role": "user", "content": f"Known facts:\n{numbered}\n\n"
                                                 f"User's message: {user_text}"}],
        "stream": False,
        "think": False,
        "format": SCHEMA,
        "keep_alive": "60m",
        "options": {"temperature": 0, "num_predict": 250},
    })
    resp.raise_for_status()
    return json.loads(resp.json()["message"]["content"]).get("facts", [])


def learn(memory, user_text: str, now: datetime) -> list[str]:
    """Extract and store new facts. Returns the facts learned (to show on the page)."""
    if not worth_checking(user_text) or looks_private(user_text):
        return []
    known = memory.similar_facts(user_text, config.LEARN_KNOWN_FACTS)
    learned = []
    for item in _ask_llm(user_text, [text for _, text, _ in known])[:3]:
        fact = str(item.get("fact", "")).strip()[:200]
        category = item.get("category") if item.get("category") in CATEGORIES else "other"
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
        learned.append(fact)
    return learned
