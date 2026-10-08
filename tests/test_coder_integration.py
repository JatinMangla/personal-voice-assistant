"""The laptop side (scripts/coder) talking to the Kaggle side (src/coder_proxy.py) directly,
with only Ollama faked. Catches any mismatch between what one sends and the other expects."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import coder_proxy
import config
from coder import service
from coder.client import CoderClient

KEY = "k" * 43
ANSWER = {"answer": "over_stop returns the first used-up meter.", "refs": ["src/budget.py:1-3"],
          "confidence": "high"}

CODE = "def over_stop(meters):\n    return next((m for m in meters if m.used >= 1), None)\n"


class FakeOllama:
    def __init__(self):
        self.seen = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.seen.append((request.url.path, body))
        if request.url.path == "/api/chat":
            reply = json.dumps(ANSWER) if body.get("format") else "plain"
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(
                {"message": {"role": "assistant", "content": reply}, "done": True}).encode()),
                headers={"content-type": "application/json"})
        vectors = [[1.0, 0.0] for _ in body["input"]]
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps({"embeddings": vectors}).encode()),
                              headers={"content-type": "application/json"})


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.delenv("CODER_URL", raising=False)
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "budget.py").write_text(CODE)
    ollama = FakeOllama()
    state = coder_proxy.ProxyState()
    app = coder_proxy.create_app(KEY, "http://ollama.test", state,
                                 rerank=lambda q, docs: [float("over_stop" in d) for d in docs],
                                 transport=httpx.MockTransport(ollama))

    def coder_for(key=KEY):
        client = CoderClient("http://testserver", key, http=TestClient(app))
        return service.Coder(tmp_path / f"home-{key[:3]}", [root], client=client), client
    return coder_for, ollama, state


def test_client_and_proxy_agree_end_to_end(wired):
    coder_for, ollama, state = wired
    coder, client = coder_for()
    assert client.online()
    out = coder.ask_repo("which function stops when a meter is used up?")
    assert "over_stop returns the first used-up meter." in out and "Refs: src/budget.py:1-3" in out
    chat = next(body for path, body in ollama.seen if path == "/api/chat")
    assert chat["model"] == config.CODER_ALIAS                       # forced by the proxy
    assert chat["options"]["num_ctx"] == config.CODER_CONTEXT_TOKENS
    assert chat["format"]["required"] == ["answer", "refs", "confidence"]
    assert state.requests >= 2 and state.bytes_out > 0                 # health + chat were counted


def test_embeddings_and_deep_search_go_through(wired):
    coder_for, ollama, _ = wired
    coder, client = coder_for()
    coder.index.refresh()
    rows = coder.index.chunks_without_vectors(10)
    coder.index.add_vectors([r["id"] for r in rows], client.embed([coder.index.vector_text(r) for r in rows]))
    assert coder.index.vector_coverage() == 1.0
    assert "over_stop" in coder.search_code("meter used up", deep=True)
    embed_calls = [b for p, b in ollama.seen if p == "/api/embed"]
    assert embed_calls and all(b["model"] == config.CODER_EMBED_MODEL for b in embed_calls)


def test_a_wrong_key_is_refused_and_the_tools_fall_back_to_search(wired):
    coder_for, ollama, _ = wired
    coder, client = coder_for("w" * 43)
    assert client.online() is False
    out = coder.ask_repo("which function stops when a meter is used up?")
    assert "refused the key" in out and "src/budget.py" in out
    assert ollama.seen == []                                           # nothing reached Ollama


def test_precompute_writes_vectors_and_a_summary_through_the_proxy(wired):
    from coder import precompute
    coder_for, ollama, _ = wired
    coder, _ = coder_for()
    coder.last_activity -= 120
    p = precompute.Precomputer(coder, idle_after=30)
    assert p.step() == "vectors"
    assert p.step() is None                  # the file is under 8 lines: no summary is wanted
    assert coder.index.vector_coverage() == 1.0
