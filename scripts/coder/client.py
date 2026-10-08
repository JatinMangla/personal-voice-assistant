"""Talks to the Coder helper on Kaggle (src/coder_proxy.py) through the ngrok address.

Needs CODER_URL (https://<your ngrok domain>) and CODER_KEY in the environment. When Kaggle
is not running the answer is `Offline` within a few seconds (remembered for 30 s), so the tools
fall back at once instead of waiting.
"""
import os
import re
import time

import httpx

ALIAS = "jarvis-coder"
OFFLINE_MEMORY_SEC = 30.0
CONNECT_TIMEOUT = 4.0
READ_TIMEOUT = 300.0     # a long answer from a big model takes a while


class Offline(Exception):
    """The Coder is not running (or not reachable)."""


class CoderError(Exception):
    """The Coder answered, but not usefully (wrong key, bad reply)."""


def parse_json(text: str):
    """The model's JSON, even if it wrapped it in a code fence or added a sentence."""
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
    raise CoderError("the coder's answer was not valid JSON")


class CoderClient:
    def __init__(self, url: str, key: str, http: httpx.Client | None = None, clock=time.time):
        self.url = url.rstrip("/")
        self.clock = clock
        self.http = http or httpx.Client(
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT))
        self.headers = {"Authorization": f"Bearer {key}", "ngrok-skip-browser-warning": "1"}
        self._offline_until = 0.0
        self._rerank_missing = False

    @classmethod
    def from_env(cls) -> "CoderClient | None":
        url, key = os.environ.get("CODER_URL", "").strip(), os.environ.get("CODER_KEY", "").strip()
        return cls(url, key) if url and key else None

    def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self.clock() < self._offline_until:
            raise Offline("the Coder is offline")
        try:
            resp = self.http.request(method, self.url + path, headers=self.headers, **kwargs)
        except httpx.TransportError as e:   # connect errors, timeouts, resets
            self._offline_until = self.clock() + OFFLINE_MEMORY_SEC
            raise Offline(f"the Coder is not reachable ({type(e).__name__})") from e
        if resp.headers.get("ngrok-error-code") or (resp.status_code in (502, 503, 504)
                                                    and path != "/rerank"):  # 503 there = no reranker
            self._offline_until = self.clock() + OFFLINE_MEMORY_SEC
            raise Offline("the Coder is offline (nothing is connected to the address)")
        if resp.status_code in (401, 403):
            raise CoderError("the Coder refused the key: CODER_KEY here must equal the CODER_KEY secret")
        if resp.status_code == 429:
            raise CoderError("the Coder is locked for a while after wrong keys")
        return resp

    def online(self) -> bool:
        try:
            return self._call("GET", "/health").status_code == 200
        except (Offline, CoderError):
            return False

    def chat(self, messages: list[dict], schema: dict | None = None, max_tokens: int = 800) -> str:
        body = {"model": ALIAS, "messages": messages, "stream": False,
                "options": {"num_predict": max_tokens, "temperature": 0.2}}
        if schema:
            body["format"] = schema
        resp = self._call("POST", "/api/chat", json=body)
        if resp.status_code != 200:
            raise CoderError(f"the coder answered with an error ({resp.status_code})")
        try:
            return resp.json()["message"]["content"]
        except (ValueError, KeyError, TypeError) as e:
            raise CoderError("the coder's reply was not understood") from e

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._call("POST", "/api/embed", json={"input": texts})
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
