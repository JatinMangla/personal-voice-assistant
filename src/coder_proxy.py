"""The coder helper's web front: a small proxy in front of Ollama (runs on Kaggle).

- Every request needs `Authorization: Bearer <CODER_KEY>` (compared in constant time).
  Wrong keys are counted; too many in a short time get 429 (right keys still pass, like
  devices.py). ngrok already turns away requests without an Authorization header.
- Only a few paths are forwarded (chat, embeddings, a model list). Nothing can pull,
  delete or create models through it.
- The client can't choose the model or the context size: the chat model is always the
  coder alias with the one fixed num_ctx (Ollama would reload the model otherwise).
- Logs show path, status, time and size only. Never the question, code or answer.
"""
import asyncio
import hmac
import json
import threading
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import config

CHAT_PATHS = {"/v1/chat/completions", "/api/chat"}
EMBED_PATHS = {"/v1/embeddings", "/api/embed"}
MAX_RERANK_DOCS = 50
MAX_RERANK_CHARS = 6000
MAX_EMBED_INPUTS = 64
MAX_EMBED_CHARS = 8000


class ProxyState:
    """Numbers the run needs: activity (idle stop), traffic (ngrok meters), wrong keys."""

    def __init__(self, now=time.time):
        self.now = now
        self.lock = threading.Lock()
        self.started = now()
        self.last_request = now()
        self.requests = 0
        self.bytes_out = 0
        self.busy = 0                       # requests being answered right now
        self.failures: list[float] = []     # times of wrong keys

    def touch(self) -> None:
        with self.lock:
            self.last_request = self.now()

    def idle_seconds(self) -> float:
        with self.lock:
            return 0.0 if self.busy else self.now() - self.last_request

    def count(self, nbytes: int) -> None:
        with self.lock:
            self.bytes_out += nbytes

    def count_request(self) -> None:
        """Every request that reaches us counts against ngrok's free monthly limit."""
        with self.lock:
            self.requests += 1

    def begin(self, background: bool = False) -> None:
        """background=True: housekeeping (e.g. the laptop pre-computing file summaries). It keeps
        the Coder 'busy' while it runs but does not push back the idle stop, otherwise a laptop
        that merely stays open would keep the GPU running for hours."""
        with self.lock:
            self.busy += 1
            if not background:
                self.last_request = self.now()

    def end(self, background: bool = False) -> None:
        with self.lock:
            self.busy = max(0, self.busy - 1)
            if not background:
                self.last_request = self.now()

    def take_traffic(self) -> tuple[int, int]:
        """(requests, bytes) since the last call, for the ngrok meters."""
        with self.lock:
            out = (self.requests, self.bytes_out)
            self.requests = self.bytes_out = 0
            return out

    def wrong_key(self) -> None:
        with self.lock:
            self.failures.append(self.now())

    def locked_out(self) -> bool:
        with self.lock:
            cutoff = self.now() - config.CODER_LOCKOUT_MIN * 60
            self.failures = [t for t in self.failures if t >= cutoff]
            return len(self.failures) >= config.CODER_FAILS_BEFORE_LOCKOUT


BACKGROUND_HEADER = "x-coder-background"


def is_background(request) -> bool:
    return request.headers.get(BACKGROUND_HEADER) == "1"


def bearer(header: str | None) -> str:
    if header and header[:7].lower() == "bearer ":
        return header[7:].strip()
    return ""


def key_matches(given: str, key: str) -> bool:
    return bool(given) and hmac.compare_digest(given.encode(), key.encode())


def clamp_reply(value, default: int) -> int:
    try:
        return max(1, min(int(value), config.CODER_MAX_REPLY_TOKENS))
    except (TypeError, ValueError):
        return default


def prepare_chat(path: str, body: dict) -> dict:
    """The client's request with the model and limits forced to ours."""
    body = dict(body)
    body["model"] = config.CODER_ALIAS
    if path == "/api/chat":
        options = dict(body.get("options") or {})
        options["num_ctx"] = config.CODER_CONTEXT_TOKENS
        options["num_predict"] = clamp_reply(options.get("num_predict"), config.CODER_MAX_REPLY_TOKENS)
        body["options"] = options
        body["keep_alive"] = -1
    else:
        wanted = body.get("max_tokens", body.get("max_completion_tokens"))
        body["max_tokens"] = clamp_reply(wanted, config.CODER_MAX_REPLY_TOKENS)
        body.pop("max_completion_tokens", None)
    return body


def prepare_embed(body: dict) -> dict | None:
    """None if the input is too big; else the request with our embedding model."""
    body = dict(body)
    inputs = body.get("input")
    inputs = [inputs] if isinstance(inputs, str) else inputs
    if not isinstance(inputs, list) or not inputs or len(inputs) > MAX_EMBED_INPUTS:
        return None
    if any(not isinstance(t, str) or len(t) > MAX_EMBED_CHARS for t in inputs):
        return None
    body["model"] = config.CODER_EMBED_MODEL
    body["input"] = inputs
    body["keep_alive"] = -1
    return body


def create_app(key: str, ollama_url: str, state: ProxyState, rerank=None, transport=None,
               ready=lambda: True) -> FastAPI:
    """rerank: callable(query, [documents]) -> [scores] or None (then /rerank answers 503).
    transport: lets tests replace the connection to Ollama."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    upstream = httpx.AsyncClient(base_url=ollama_url, transport=transport,
                                 timeout=httpx.Timeout(900.0, connect=5.0))

    def log(method: str, path: str, status: int, started: float, nbytes: int) -> None:
        print(f"[coder] {method} {path} {status} in {time.time() - started:.1f}s ({nbytes} B)")

    @app.middleware("http")
    async def gate(request: Request, call_next):
        state.count_request()
        given = bearer(request.headers.get("authorization"))
        if not key_matches(given, key):
            if state.locked_out():
                return JSONResponse({"error": "too many wrong keys, try later"}, 429)
            state.wrong_key()
            return JSONResponse({"error": "unauthorized"}, 401)
        if int(request.headers.get("content-length") or 0) > config.CODER_MAX_BODY_BYTES:
            return JSONResponse({"error": "request too big"}, 413)
        # Real work keeps the Coder alive. A health check is only a look at it, and background
        # housekeeping is not a person using it: neither may postpone the idle stop.
        if request.url.path != "/health" and not is_background(request):
            state.touch()
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"model": config.CODER_ALIAS, "ready": bool(ready()), "rerank": rerank is not None,
                "idle_seconds": int(state.idle_seconds()), "context": config.CODER_CONTEXT_TOKENS}

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": config.CODER_ALIAS, "object": "model",
                                            "owned_by": "jarvis"}]}

    @app.get("/api/tags")
    async def tags():
        return {"models": [{"name": config.CODER_ALIAS, "model": config.CODER_ALIAS}]}

    async def read_json(request: Request) -> dict | None:
        try:
            body = await request.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    async def forward(request: Request, path: str, body: dict) -> Response:
        started, method = time.time(), request.method
        background = is_background(request)
        state.begin(background)  # stays busy until the whole reply has been streamed
        try:
            req = upstream.build_request("POST", path, json=body)
            resp = await upstream.send(req, stream=True)
        except httpx.HTTPError:
            state.end(background)
            log(method, path, 502, started, 0)
            return JSONResponse({"error": "the coder model is not reachable"}, 502)
        content_type = resp.headers.get("content-type", "application/json")

        async def pieces():
            sent = 0
            try:
                async for chunk in resp.aiter_raw():
                    sent += len(chunk)
                    yield chunk
            finally:
                await resp.aclose()
                state.count(sent)
                state.end(background)
                log(method, path, resp.status_code, started, sent)

        return StreamingResponse(pieces(), status_code=resp.status_code, media_type=content_type)

    @app.post("/v1/chat/completions")
    @app.post("/api/chat")
    async def chat(request: Request):
        body = await read_json(request)
        if body is None:
            return JSONResponse({"error": "bad request"}, 400)
        return await forward(request, request.url.path, prepare_chat(request.url.path, body))

    @app.post("/v1/embeddings")
    @app.post("/api/embed")
    async def embed(request: Request):
        body = await read_json(request)
        prepared = prepare_embed(body) if body is not None else None
        if prepared is None:
            return JSONResponse({"error": "bad request"}, 400)
        return await forward(request, request.url.path, prepared)

    @app.post("/rerank")
    async def rerank_route(request: Request):
        started = time.time()
        body = await read_json(request)
        query = body.get("query") if body else None
        docs = body.get("documents") if body else None
        if (not isinstance(query, str) or not isinstance(docs, list) or not docs
                or len(docs) > MAX_RERANK_DOCS or any(not isinstance(d, str) for d in docs)):
            return JSONResponse({"error": "bad request"}, 400)
        if rerank is None:
            return JSONResponse({"error": "no reranker on this run"}, 503)
        docs = [d[:MAX_RERANK_CHARS] for d in docs]
        background = is_background(request)
        state.begin(background)
        try:
            scores = await asyncio.get_running_loop().run_in_executor(None, rerank, query, docs)
        except Exception as e:  # the answer is only a better order; never fail the call hard
            log("POST", "/rerank", 500, started, 0)
            return JSONResponse({"error": f"rerank failed: {type(e).__name__}"}, 500)
        finally:
            state.end(background)
        out = json.dumps({"scores": [float(s) for s in scores]})
        state.count(len(out))
        log("POST", "/rerank", 200, started, len(out))
        return Response(out, media_type="application/json")

    @app.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def everything_else(rest: str):
        return JSONResponse({"error": "not found"}, 404)

    return app
