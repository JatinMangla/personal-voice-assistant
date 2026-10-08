"""Talks to the Coder helper on Kaggle (src/coder_proxy.py) through the ngrok address.

Needs CODER_URL (https://<your ngrok domain>) and CODER_KEY in the environment. When Kaggle
is not running the answer is `Offline` within a few seconds (remembered for 30 s), so the tools
fall back at once instead of waiting.
"""
import hashlib
import os
import re
import time

import httpx

ALIAS = "jarvis-coder"
OFFLINE_MEMORY_SEC = 30.0
REFUSED_MEMORY_SEC = 60.0   # after a refused key, don't ask again for a while (no hammering)
CONNECT_TIMEOUT = 4.0
READ_TIMEOUT = 300.0     # a long answer from a big model takes a while


class Offline(Exception):
    """The Coder is not running (or not reachable)."""


class CoderError(Exception):
    """The Coder answered, but not usefully (wrong key, bad reply)."""


CLOSERS = ('"', '"}', '"]', '"]}', '"}]', '"}]}', ']', ']}', '}', '}]', '}]}')


def repair_truncated_json(text: str):
    """JSON that stops in the middle (the model hit its length limit while writing a long list):
    close it at the last place that still makes sense, so the part that was written is not lost.
    Returns the object, or None."""
    import json
    s = text.strip()
    if not s.startswith("{"):
        return None
    for _ in range(200):
        for closer in CLOSERS:
            try:
                value = json.loads(s + closer)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
        cut = max(s.rfind(","), s.rfind("{"))
        if cut <= 0:
            return None
        s = s[:cut].rstrip()
    return None


def parse_json(text: str):
    """The model's JSON, even if it wrapped it in a code fence, added a sentence, or was cut off."""
    import json
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    for candidate in ([fenced.group(1)] if fenced else []) + [text[text.find("{"):text.rfind("}") + 1]]:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    repaired = repair_truncated_json(fenced.group(1) if fenced else text[text.find("{"):])
    if repaired is not None:
        return repaired
    raise CoderError("the coder's answer was not valid JSON")


class CoderClient:
    def __init__(self, url: str, key: str, http: httpx.Client | None = None, clock=time.time):
        self.url = url.rstrip("/")
        self.clock = clock
        self.http = http or httpx.Client(
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT))
        self.headers = {"Authorization": f"Bearer {key}", "ngrok-skip-browser-warning": "1"}
        self._offline_until = 0.0
        self._refused_until = 0.0
        self._refused_text = ""
        self._rerank_missing = False
        self.key = key
        self.last_done_reason: str | None = None   # "length" = the last answer was cut off

    def fingerprint(self) -> str:
        """Same as the `[coder] key fingerprint` line in the Kaggle log (SHA-256, 8 characters)."""
        return hashlib.sha256(self.key.encode()).hexdigest()[:8]

    @classmethod
    def from_env(cls) -> "CoderClient | None":
        url, key = os.environ.get("CODER_URL", "").strip(), os.environ.get("CODER_KEY", "").strip()
        return cls(url, key) if url and key else None

    def _call(self, method: str, path: str, background: bool = False, **kwargs) -> httpx.Response:
        """background=True marks housekeeping calls: the Coder then does not count them as use, so
        they can never keep the (GPU-hour burning) run alive past its idle stop."""
        headers = {**self.headers, "X-Coder-Background": "1"} if background else self.headers
        if self.clock() < self._offline_until:
            raise Offline("the Coder is offline")
        if self.clock() < self._refused_until:
            raise CoderError(self._refused_text)
        try:
            resp = self.http.request(method, self.url + path, headers=headers, **kwargs)
        except httpx.TransportError as e:   # connect errors, timeouts, resets
            self._offline_until = self.clock() + OFFLINE_MEMORY_SEC
            raise Offline(f"the Coder is not reachable ({type(e).__name__})") from e
        if resp.headers.get("ngrok-error-code") or (resp.status_code in (502, 503, 504)
                                                    and path != "/rerank"):  # 503 there = no reranker
            self._offline_until = self.clock() + OFFLINE_MEMORY_SEC
            raise Offline("the Coder is offline (nothing is connected to the address)")
        if resp.status_code in (401, 403, 429):
            self._refused_text = (
                f"the Coder refused the key (here: fingerprint {self.fingerprint()}; compare it with the "
                "`[coder] key fingerprint` line in the Kaggle log). CODER_KEY here must equal the "
                "CODER_KEY GitHub secret" + (" (it is also locked for a few minutes after wrong keys)"
                                               if resp.status_code == 429 else ""))
            self._refused_until = self.clock() + REFUSED_MEMORY_SEC
            raise CoderError(self._refused_text)
        return resp

    def online(self, background: bool = False) -> bool:
        try:
            return self._call("GET", "/health", background=background).status_code == 200
        except (Offline, CoderError):
            return False

    def link_status(self) -> str:
        """One line for `coder_status`: online, refused (with the key fingerprint) or offline."""
        if self.online():
            return f"online (key fingerprint {self.fingerprint()})"
        if self.clock() < self._refused_until:
            return f"reachable but it REFUSED the key. {self._refused_text}"
        return f"offline (press Start Coder; key fingerprint here {self.fingerprint()})"

    def chat(self, messages: list[dict], schema: dict | None = None, max_tokens: int = 800,
             background: bool = False) -> str:
        body = {"model": ALIAS, "messages": messages, "stream": False,
                "options": {"num_predict": max_tokens, "temperature": 0.2}}
        if schema:
            body["format"] = schema
        resp = self._call("POST", "/api/chat", background=background, json=body)
        if resp.status_code != 200:
            raise CoderError(f"the coder answered with an error ({resp.status_code})")
        try:
            data = resp.json()
            self.last_done_reason = data.get("done_reason")
            return data["message"]["content"]
        except (ValueError, KeyError, TypeError) as e:
            raise CoderError("the coder's reply was not understood") from e

    def embed(self, texts: list[str], background: bool = False) -> list[list[float]]:
        resp = self._call("POST", "/api/embed", background=background, json={"input": texts})
        if resp.status_code != 200:
            raise CoderError(f"embedding failed ({resp.status_code})")
        try:
            vectors = resp.json()["embeddings"]
        except (ValueError, KeyError, TypeError) as e:
            raise CoderError("embedding reply not understood") from e
        if len(vectors) != len(texts):
            raise CoderError("embedding reply has the wrong size")
        return vectors

    def rerank(self, query: str, documents: list[str]) -> list[float] | None:
        """Scores (higher = better) or None if this run has no reranker or it failed."""
        if self._rerank_missing:
            return None
        resp = self._call("POST", "/rerank", json={"query": query, "documents": documents})
        if resp.status_code == 503:
            self._rerank_missing = True       # not loaded on this run: don't ask again
            return None
        if resp.status_code != 200:
            return None
        scores = resp.json().get("scores")
        return scores if isinstance(scores, list) and len(scores) == len(documents) else None
