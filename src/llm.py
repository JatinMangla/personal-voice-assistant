"""Talks to the LLM through Ollama and streams the reply sentence by sentence."""
import json
import random
import re
from datetime import datetime
from typing import Iterator

import requests

import config
import tools

LANGUAGE_RULE = {
    "en": "Reply in English.",
    "hi": "Reply in Hindi written in Devanagari script. Common English words are fine.",
}


def system_prompt(lang: str, facts: list[str], related: list[str], now: datetime) -> str:
    parts = [
        f"You are {config.ASSISTANT_NAME}, a friendly personal voice assistant for one user.",
        "Your replies are spoken aloud, so keep them to 1-3 short sentences unless the "
        "user asks for more. No markdown, lists, emojis, or code.",
        LANGUAGE_RULE.get(lang, LANGUAGE_RULE["en"]),
        f"Current date and time in India: {now.strftime('%A, %d %B %Y, %I:%M %p')}.",
        "You cannot control devices or look things up online unless a tool is available. "
        "Never pretend you did something.",
    ]
    if facts:
        parts.append("Facts the user asked you to remember:\n" + "\n".join(f"- {f}" for f in facts))
    if related:
        parts.append("Possibly related past conversations (use only if relevant):\n"
                     + "\n".join(f"- {r}" for r in related))
    return "\n\n".join(parts)


def build_messages(user_text: str, lang: str, facts: list[str], related: list[str],
                   history: list[tuple[str, str]], now: datetime) -> list[dict]:
    messages = [{"role": "system", "content": system_prompt(lang, facts, related, now)}]
    for user, reply in history:
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant", "content": reply})
    messages.append({"role": "user", "content": user_text})
    return messages


_NO_THINK_OPTION: set[str] = set()  # models that reject the "think" setting


def stream_reply(messages: list[dict], model: str | None = None) -> Iterator[str]:
    """Yield reply text pieces as the model writes them. Runs registered tools."""
    model = model or config.LLM_MODEL
    messages = list(messages)
    for _ in range(3):  # at most 3 rounds of tool calls
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "keep_alive": "60m",
            "options": {"num_ctx": config.LLM_CONTEXT_TOKENS,
                        "num_predict": config.LLM_MAX_REPLY_TOKENS},
        }
        if model not in _NO_THINK_OPTION:
            payload["think"] = False  # no hidden reasoning: much faster for voice
        if tools.REGISTRY:
            payload["tools"] = tools.ollama_specs()

        tool_calls = []
        with requests.post(f"{config.OLLAMA_URL}/api/chat", json=payload,
                           stream=True, timeout=(5, 120)) as resp:
            if resp.status_code == 400 and "think" in resp.text and model not in _NO_THINK_OPTION:
                # Our fine-tuned model has thinking switched off in its template instead.
                _NO_THINK_OPTION.add(model)
                yield from stream_reply(messages, model)
                return
            if resp.status_code >= 400:  # include Ollama's own explanation
                raise RuntimeError(f"Ollama error {resp.status_code}: {resp.text.strip()[:500]}")
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    raise RuntimeError(f"Ollama error: {chunk['error']}")
                message = chunk.get("message", {})
                tool_calls += message.get("tool_calls") or []
                if message.get("content"):
                    yield message["content"]

        if not tool_calls:
            return
        messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call.get("function", {})
            result = tools.run(fn.get("name", ""), fn.get("arguments") or {})
            messages.append({"role": "tool", "content": result, "tool_name": fn.get("name", "")})


_SENTENCE_END = re.compile(r"(?<=[.!?।])\s+|\n+")


class SentenceChunker:
    """Collect streamed text and hand out whole sentences for speech."""

    def __init__(self, min_chars: int = 8):
        self.buffer = ""   # text after the last sentence end
        self.pending = ""  # finished sentences too short to speak on their own
        self.min_chars = min_chars

    def feed(self, text: str) -> list[str]:
        self.buffer += text
        parts = _SENTENCE_END.split(self.buffer)
        self.buffer = parts.pop()  # the last part may be unfinished
        out = []
        for part in parts:
            self.pending = f"{self.pending} {part}".strip()
            if len(self.pending) >= self.min_chars:
                out.append(self.pending)
                self.pending = ""
        return out

    def flush(self) -> list[str]:
        rest = f"{self.pending} {self.buffer}".strip()
        self.buffer = self.pending = ""
        return [rest] if rest else []


class ModelPicker:
    """Which LLM answers: the base model, or (after fine-tuning) a blind A/B test.

    mode "auto": answer with base and tuned at random (you don't know which) until
    both have AB_MIN_RATINGS 👍/👎 ratings, then keep whichever you rated better.
    """

    def __init__(self, base: str, tuned: str | None, stats: dict[str, tuple[int, int]],
                 mode: str = "auto", min_ratings: int = 30):
        self.base, self.tuned = base, tuned
        self.decision = self._decide(stats, mode, min_ratings)

    def _decide(self, stats, mode, min_ratings) -> str:
        if not self.tuned or mode == "base":
            return "base"
        if mode == "tuned":
            return "tuned"
        base_up, base_down = stats.get(self.base, (0, 0))
        tuned_up, tuned_down = stats.get(self.tuned, (0, 0))
        if base_up + base_down < min_ratings or tuned_up + tuned_down < min_ratings:
            return "ab"
        base_rate = base_up / (base_up + base_down)
        tuned_rate = tuned_up / (tuned_up + tuned_down)
        return "tuned" if tuned_rate >= base_rate else "base"

    def in_use(self) -> list[str]:
        return {"ab": [self.base, self.tuned], "tuned": [self.tuned]}.get(self.decision, [self.base])

    def pick(self) -> str:
        if self.decision == "ab":
            return random.choice([self.base, self.tuned])
        return self.tuned if self.decision == "tuned" else self.base


def is_ready(model: str) -> bool:
    try:
        tags = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=5).json()
        return any(m.get("name") == model for m in tags.get("models", []))
    except requests.RequestException:
        return False
