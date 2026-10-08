"""The coder helper's web front (src/coder_proxy.py) in front of a fake Ollama."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import coder_proxy
import config
import tunnel

KEY = "k" * 43


def streamed(status: int, body: bytes, content_type: str) -> httpx.Response:
    """A response that is still unread, like one coming off the network."""
    return httpx.Response(status, stream=httpx.ByteStream(body), headers={"content-type": content_type})


class FakeOllama:
    """Records what reached 'Ollama' and answers like it."""

    def __init__(self):
        self.seen: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.seen.append((request.url.path, body))
        if request.url.path == "/api/chat":
            lines = [json.dumps({"message": {"content": w}, "done": False}) for w in ("he", "llo")]
            lines.append(json.dumps({"done": True, "eval_count": 2}))
            return streamed(200, ("\n".join(lines) + "\n").encode(), "application/x-ndjson")
        if request.url.path == "/v1/chat/completions":
            return streamed(200, json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode(),
                            "application/json")
        return streamed(200, json.dumps({"embeddings": [[0.1, 0.2]]}).encode(), "application/json")


@pytest.fixture
def ollama():
    return FakeOllama()


@pytest.fixture
def state():
    return coder_proxy.ProxyState()


@pytest.fixture
def client(ollama, state):
    app = coder_proxy.create_app(KEY, "http://ollama.test", state,
                                 rerank=lambda q, docs: [float(len(d)) for d in docs],
                                 transport=httpx.MockTransport(ollama))
    return TestClient(app)


def auth(key=KEY):
    return {"Authorization": f"Bearer {key}"}


def test_no_key_and_wrong_key_get_401_and_nothing_reaches_ollama(client, ollama):
    assert client.post("/api/chat", json={"messages": []}).status_code == 401
    assert client.post("/api/chat", json={"messages": []}, headers=auth("x" * 43)).status_code == 401
    assert client.get("/health", headers={"Authorization": "Basic abc"}).status_code == 401
    assert ollama.seen == []


def test_health_with_key(client):
    r = client.get("/health", headers=auth())
    assert r.status_code == 200
    assert r.json()["model"] == config.CODER_ALIAS and r.json()["rerank"] is True


def test_chat_always_uses_our_model_context_and_reply_cap(client, ollama):
    r = client.post("/api/chat", headers=auth(), json={
        "model": "llama3:70b", "messages": [{"role": "user", "content": "q"}],
        "options": {"num_ctx": 4096, "num_predict": 99999, "temperature": 0.1}})
    assert r.status_code == 200
    assert r.text.count("\n") == 3                      # the stream passed through
    path, sent = ollama.seen[0]
    assert sent["model"] == config.CODER_ALIAS
    assert sent["options"]["num_ctx"] == config.CODER_CONTEXT_TOKENS
    assert sent["options"]["num_predict"] == config.CODER_MAX_REPLY_TOKENS
    assert sent["options"]["temperature"] == 0.1       # harmless settings are kept
    assert sent["keep_alive"] == -1


def test_openai_chat_is_forwarded_with_our_model_and_cap(client, ollama):
    r = client.post("/v1/chat/completions", headers=auth(), json={
        "model": "gpt-4", "messages": [], "max_tokens": 50000})
    assert r.json()["choices"][0]["message"]["content"] == "hi"
    _, sent = ollama.seen[0]
    assert sent["model"] == config.CODER_ALIAS and sent["max_tokens"] == config.CODER_MAX_REPLY_TOKENS


def test_embeddings_use_our_embedding_model_and_are_size_limited(client, ollama):
    ok = client.post("/api/embed", headers=auth(), json={"model": "anything", "input": ["a", "b"]})
    assert ok.status_code == 200
    assert ollama.seen[0][1]["model"] == config.CODER_EMBED_MODEL
    too_many = client.post("/api/embed", headers=auth(),
                           json={"input": ["x"] * (coder_proxy.MAX_EMBED_INPUTS + 1)})
    too_long = client.post("/api/embed", headers=auth(),
                           json={"input": ["x" * (coder_proxy.MAX_EMBED_CHARS + 1)]})
    assert too_many.status_code == 400 and too_long.status_code == 400
    assert client.post("/api/embed", headers=auth(), content=b"not json").status_code == 400
    assert len(ollama.seen) == 1


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/pull"), ("POST", "/api/create"), ("DELETE", "/api/delete"),
    ("POST", "/api/generate"), ("GET", "/api/ps"), ("POST", "/api/copy"), ("GET", "/anything")])
def test_other_paths_are_not_forwarded(client, ollama, method, path):
    r = client.request(method, path, headers=auth(), json={"name": "x"})
    assert r.status_code == 404
    assert ollama.seen == []


def test_model_lists_name_only_our_model(client, ollama):
    assert client.get("/v1/models", headers=auth()).json()["data"][0]["id"] == config.CODER_ALIAS
    assert client.get("/api/tags", headers=auth()).json()["models"][0]["name"] == config.CODER_ALIAS
    assert ollama.seen == []


def test_rerank_scores_and_validation(client):
    r = client.post("/rerank", headers=auth(), json={"query": "q", "documents": ["a", "bbb"]})
    assert r.json() == {"scores": [1.0, 3.0]}
    for bad in ({"query": "q"}, {"query": "q", "documents": []}, {"query": 1, "documents": ["a"]},
                {"query": "q", "documents": ["a"] * (coder_proxy.MAX_RERANK_DOCS + 1)},
                {"query": "q", "documents": [1]}):
        assert client.post("/rerank", headers=auth(), json=bad).status_code == 400


def test_rerank_without_reranker_and_when_it_crashes(ollama, state):
    def crash(q, docs):
        raise RuntimeError("cuda out of memory")
    for fn, status in ((None, 503), (crash, 500)):
        app = coder_proxy.create_app(KEY, "http://o.test", state, rerank=fn,
                                     transport=httpx.MockTransport(ollama))
        r = TestClient(app).post("/rerank", headers=auth(), json={"query": "q", "documents": ["a"]})
        assert r.status_code == status


def test_big_request_is_refused(client):
    r = client.post("/api/chat", headers={**auth(), "Content-Length": str(config.CODER_MAX_BODY_BYTES + 1)},
                    content=b"{}")
    assert r.status_code == 413


def test_ollama_down_gives_502(state):
    def down(request):
        raise httpx.ConnectError("refused")
    app = coder_proxy.create_app(KEY, "http://o.test", state, transport=httpx.MockTransport(down))
    r = TestClient(app).post("/api/chat", headers=auth(), json={"messages": []})
    assert r.status_code == 502 and state.busy == 0


def test_wrong_keys_lock_out_wrong_keys_only(client, state):
    for _ in range(config.CODER_FAILS_BEFORE_LOCKOUT):
        assert client.get("/health", headers=auth("x" * 43)).status_code == 401
    assert client.get("/health", headers=auth("x" * 43)).status_code == 429
    assert client.get("/health").status_code == 429
    assert client.get("/health", headers=auth()).status_code == 200   # the real key still works


def test_lockout_ends_after_the_window():
    clock = [1000.0]
    s = coder_proxy.ProxyState(now=lambda: clock[0])
    for _ in range(config.CODER_FAILS_BEFORE_LOCKOUT):
        s.wrong_key()
    assert s.locked_out()
    clock[0] += config.CODER_LOCKOUT_MIN * 60 + 1
    assert not s.locked_out()


def test_state_idle_busy_and_traffic():
    clock = [0.0]
    s = coder_proxy.ProxyState(now=lambda: clock[0])
    clock[0] = 300
    assert s.idle_seconds() == 300
    s.count_request()
    s.begin()
    clock[0] = 900
    assert s.idle_seconds() == 0          # a request being answered is not idle
    s.end()
    clock[0] = 960
    assert s.idle_seconds() == 60
    s.count(500)
    assert s.take_traffic() == (1, 500) and s.take_traffic() == (0, 0)


def test_logs_have_no_content(client, capsys):
    client.post("/api/chat", headers=auth(), json={"messages": [{"role": "user", "content": "SECRET-QUESTION"}]})
    out = capsys.readouterr().out
    assert "[coder] POST /api/chat 200" in out and "SECRET" not in out


def test_bearer_parsing_and_comparison():
    assert coder_proxy.bearer("Bearer abc") == "abc" and coder_proxy.bearer("bearer  abc ") == "abc"
    assert coder_proxy.bearer(None) == "" and coder_proxy.bearer("Basic abc") == ""
    assert coder_proxy.key_matches("abc", "abc") and not coder_proxy.key_matches("", "")
    assert not coder_proxy.key_matches("abd", "abc")


def test_ngrok_policy_for_the_coder_link_needs_an_authorization_header():
    rule = tunnel.coder_traffic_policy()["on_http_request"][0]
    assert rule["expressions"] == ["!('authorization' in req.headers)"]
    assert rule["actions"][0]["type"] == "deny" and rule["actions"][0]["config"]["status_code"] == 401
    assert "oauth" not in json.dumps(tunnel.coder_traffic_policy())


def test_every_request_counts_for_ngrok_but_wrong_keys_do_not_keep_the_run_alive(client, state):
    state.last_request -= 500
    client.get("/health", headers=auth("x" * 43))          # wrong key
    client.get("/nothing", headers=auth())                   # right key, unknown path
    client.get("/health", headers=auth())
    assert state.requests == 3
    state.last_request = state.now() - 700
    client.get("/health", headers=auth("x" * 43))
    assert state.idle_seconds() >= 700                        # still idle: a wrong key is not activity
    client.get("/health", headers=auth())
    assert state.idle_seconds() < 5
