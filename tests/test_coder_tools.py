"""The coder tools that use the model (ask_repo, summarize, draft_code), the client, the self-check,
vector search, reranking and the idle pre-computing. A fake Coder stands in for Kaggle."""
import json

import httpx
import pytest

from coder import client as client_mod
from coder import precompute, search, selfcheck, service, tasks
from coder.client import CoderClient, CoderError, Offline, parse_json
from coder.index import Index

PY = '''def start_blocker(meters, budget):
    """Which meter stops the start?"""
    for m in meters:
        if m.used >= 5:
            return m
    return None


def over_stop(meters):
    return None
'''


@pytest.fixture(autouse=True)
def no_real_coder(monkeypatch):
    monkeypatch.delenv("CODER_URL", raising=False)
    monkeypatch.delenv("CODER_KEY", raising=False)
    monkeypatch.delenv("CODER_RUN_TESTS", raising=False)


class FakeClient:
    """Answers like the Coder. replies: JSON strings (or exceptions) for chat(), in order."""

    def __init__(self, replies=(), online=True, embedder=None, scores=None):
        self.replies, self.chats, self.up = list(replies), [], online
        self.embedder, self.scores, self.embedded = embedder, scores, []

    def online(self):
        return self.up

    def link_status(self):
        return "online" if self.up else "offline (press Start Coder)"

    def chat(self, messages, schema=None, max_tokens=800):
        self.chats.append(messages)
        if not self.up:
            raise Offline("offline")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)

    def embed(self, texts):
        if not self.up:
            raise Offline("offline")
        self.embedded += texts
        return [self.embedder._vec(t).tolist() for t in texts]

    def rerank(self, query, docs):
        if not self.up:
            raise Offline("offline")
        return self.scores(query, docs) if self.scores else None


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "budget.py").write_text(PY)
    (root / "src" / "notes.md").write_text("# Notes\nsome text\n" * 5)
    (root / ".env").write_text("TOKEN=abc\n")
    return root


def make(repo, tmp_path, client=None):
    return service.Coder(tmp_path / "home", [repo], client=client)


ANSWER = {"answer": "start_blocker returns the first meter that is used up.",
          "refs": ["src/budget.py:1-6", "src/invented.py:3"], "confidence": "high"}


# --- the client ------------------------------------------------------------------------------
def test_parse_json_handles_fences_and_chatter():
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('Here you go:\n```json\n{"a": 2}\n```') == {"a": 2}
    assert parse_json('Sure! {"a": 3} hope it helps') == {"a": 3}
    with pytest.raises(CoderError):
        parse_json("no json here")


def client_with(handler, clock=lambda: 0.0):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CoderClient("https://coder.ngrok.test/", "k" * 40, http=http, clock=clock)


def test_client_sends_the_key_and_reads_the_reply():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["ngrok"] = request.headers["ngrok-skip-browser-warning"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "hello"}})
    c = client_with(handler)
    assert c.chat([{"role": "user", "content": "q"}], schema={"type": "object"}, max_tokens=50) == "hello"
    assert seen["auth"] == "Bearer " + "k" * 40 and seen["ngrok"] == "1"
    assert seen["body"]["format"] == {"type": "object"} and seen["body"]["options"]["num_predict"] == 50
    assert seen["body"]["stream"] is False


def test_unreachable_means_offline_and_is_remembered_for_a_while():
    now = [100.0]
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("no route")
    c = client_with(handler, clock=lambda: now[0])
    assert c.online() is False
    assert c.online() is False and len(calls) == 1          # not asked again within 30 s
    now[0] += client_mod.OFFLINE_MEMORY_SEC + 1
    assert c.online() is False and len(calls) == 2


def test_ngrok_error_page_means_offline():
    c = client_with(lambda r: httpx.Response(404, headers={"ngrok-error-code": "ERR_NGROK_3200"}))
    with pytest.raises(Offline):
        c.chat([])


def test_wrong_key_and_lockout_are_errors_not_offline():
    for status, text in ((401, "refused the key"), (429, "locked")):
        c = client_with(lambda r, s=status: httpx.Response(s, json={}))
        with pytest.raises(CoderError, match=text):
            c.chat([])
        assert c.online() is False


def test_embed_checks_the_reply_size():
    c = client_with(lambda r: httpx.Response(200, json={"embeddings": [[0.1]]}))
    with pytest.raises(CoderError):
        c.embed(["a", "b"])
    assert c.embed(["a"]) == [[0.1]]


def test_rerank_503_means_no_reranker_and_is_not_asked_again():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, json={})
    c = client_with(handler)
    assert c.rerank("q", ["a"]) is None and c.rerank("q", ["a"]) is None
    assert len(calls) == 1                       # asked once, then remembered
    assert c._offline_until == 0.0               # a 503 on /rerank is "no reranker", not "offline"


def test_rerank_scores_are_validated():
    c = client_with(lambda r: httpx.Response(200, json={"scores": [0.2, 0.9]}))
    assert c.rerank("q", ["a", "b"]) == [0.2, 0.9]
    assert c.rerank("q", ["only one"]) is None


def test_from_env(monkeypatch):
    assert CoderClient.from_env() is None
    monkeypatch.setenv("CODER_URL", "https://x.ngrok.test/")
    assert CoderClient.from_env() is None
    monkeypatch.setenv("CODER_KEY", "k" * 40)
    assert CoderClient.from_env().url == "https://x.ngrok.test"


# --- shrinking long logs ---------------------------------------------------------------------
def test_shrink_keeps_trouble_head_and_tail_and_says_what_was_left_out():
    lines = [f"INFO step {i} fine" for i in range(5000)]
    lines[2500] = "ERROR something exploded in module x"
    text = "\n".join(lines)
    out = tasks.shrink(text, 8000)
    assert len(out) <= 9000
    assert "INFO step 0 fine" in out and "INFO step 4999 fine" in out
    assert "[line 2501] ERROR something exploded" in out and "lines left out" in out
    assert tasks.shrink("short", 100) == "short"


# --- ask_repo ------------------------------------------------------------------------------
def test_ask_repo_answers_caches_and_drops_invented_refs(repo, tmp_path):
    fake = FakeClient([ANSWER])
    coder = make(repo, tmp_path, fake)
    first = coder.ask_repo("which meter stops the start?")
    assert "first meter that is used up" in first
    assert "src/budget.py:1-6" in first and "invented" not in first
    assert "Confidence: high" in first and "answer #1" in first
    assert "src/budget.py" in "".join(m["content"] for m in fake.chats[0])       # the code was sent
    assert "TOKEN=abc" not in "".join(m["content"] for m in fake.chats[0])       # never the secrets file
    again = coder.ask_repo("Which meter stops the start?  ")
    assert again.endswith("(cached, answer #1)") and len(fake.chats) == 1        # no second model call


def test_ask_repo_cache_is_dropped_when_a_used_file_changes(repo, tmp_path):
    fake = FakeClient([ANSWER, {**ANSWER, "answer": "now different"}])
    coder = make(repo, tmp_path, fake)
    coder.ask_repo("which meter stops the start?")
    (repo / "src" / "budget.py").write_text(PY + "\n# edited\n")
    assert "now different" in coder.ask_repo("which meter stops the start?")


def test_low_confidence_answers_are_not_cached(repo, tmp_path):
    fake = FakeClient([{**ANSWER, "confidence": "low"}, ANSWER])
    coder = make(repo, tmp_path, fake)
    assert "low confidence" in coder.ask_repo("which meter stops the start?")
    assert "cached" not in coder.ask_repo("which meter stops the start?")
    assert len(fake.chats) == 2


def test_ask_repo_offline_returns_the_closest_code(repo, tmp_path):
    coder = make(repo, tmp_path, FakeClient(online=False))
    out = coder.ask_repo("which meter stops the start?")
    assert out.startswith("The Coder is offline") and "src/budget.py" in out


def test_ask_repo_without_setup_and_on_coder_error(repo, tmp_path):
    assert "CODER_URL and CODER_KEY are not set" in make(repo, tmp_path / "a", None).ask_repo("start blocker")
    bad = make(repo, tmp_path / "b", FakeClient(["not json at all"]))
    out = bad.ask_repo("start blocker")
    assert "could not answer" in out and "src/budget.py" in out


# --- summarize -------------------------------------------------------------------------------
def test_summarize_a_file_with_the_model_and_cache_it(repo, tmp_path):
    log = repo / "run.txt"
    log.write_text("\n".join(f"line {i}" for i in range(50)) + "\nERROR boom\n")
    reply = {"summary": "One error: boom.", "key_points": ["ERROR boom at the end"]}
    fake = FakeClient([reply])
    coder = make(repo, tmp_path, fake)
    out = coder.summarize(str(log), "what failed?")
    assert out.startswith("One error: boom.") and "- ERROR boom at the end" in out
    assert "what failed?" in fake.chats[0][1]["content"]
    assert coder.summarize(str(log), "what failed?").endswith("(cached, answer #1)")
    log.write_text("different\n")
    fake.replies.append(reply)
    assert "cached" not in coder.summarize(str(log), "what failed?")


def test_summarize_text_and_refuses_secrets_and_outside_files(repo, tmp_path):
    fake = FakeClient([{"summary": "s", "key_points": []}])
    coder = make(repo, tmp_path, fake)
    assert coder.summarize("line one\nline two\nERROR x").startswith("s")
    assert "not found, or not inside" in coder.summarize(str(repo / ".env")).lower()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    assert "not found, or not inside" in coder.summarize(str(outside)).lower()
    assert "not found, or not inside" in coder.summarize("nope/missing.log").lower()


def test_summarize_offline_still_reduces_the_log(repo, tmp_path):
    lines = [f"INFO step {i}" for i in range(3000)]
    lines[1500] = "Traceback (most recent call last): boom"
    coder = make(repo, tmp_path, FakeClient(online=False))
    out = coder.summarize("\n".join(lines), "what failed?")
    assert out.startswith("The Coder is offline") and "Traceback (most recent call last): boom" in out
    assert len(out) < 8000


# --- draft_code --------------------------------------------------------------------------------
GOOD = {"files": [{"path": "src/extra.py", "content": "def extra():\n    return 1\n"}], "notes": "added extra"}
BROKEN = {"files": [{"path": "src/extra.py", "content": "def extra(:\n    return 1\n"}], "notes": ""}


def test_draft_is_shown_checked_and_never_written(repo, tmp_path):
    coder = make(repo, tmp_path, FakeClient([GOOD]))
    out = coder.draft_code("add a function extra", ["src/budget.py"])
    assert out.startswith("[Draft by the coder model. NOT written to disk.")
    assert "````src/extra.py\ndef extra():" in out and "Checks: passed" in out
    assert "not run (CODER_RUN_TESTS=1" in out and "answer #1" in out
    assert not (repo / "src" / "extra.py").exists()


def test_broken_draft_is_sent_back_with_the_error_and_fixed(repo, tmp_path):
    fake = FakeClient([BROKEN, GOOD])
    out = make(repo, tmp_path, fake).draft_code("add a function extra")
    assert "Checks: passed" in out and len(fake.chats) == 2
    retry = fake.chats[1][1]["content"]
    assert "syntax error" in retry and "def extra(:" in retry and "Fix them" in retry


def test_draft_that_never_passes_says_failed(repo, tmp_path):
    out = make(repo, tmp_path, FakeClient([BROKEN, BROKEN, BROKEN])).draft_code("add a function extra")
    assert "Checks: FAILED after 3 tries" in out and "answer #" not in out


def test_draft_paths_outside_the_project_or_secrets_are_rejected(repo, tmp_path):
    evil = {"files": [{"path": "../outside.py", "content": "x = 1\n"},
                      {"path": ".env", "content": "A=1\n"},
                      {"path": "/etc/passwd.py", "content": "x = 1\n"},
                      {"path": "C:/Windows/x.py", "content": "x = 1\n"}], "notes": ""}
    out = make(repo, tmp_path, FakeClient([evil, evil, evil])).draft_code("do something")
    assert "FAILED" in out and "path is not allowed" in out
    assert "````" not in out.split("Notes:")[0].replace("[Draft", "")        # nothing accepted to show


def test_draft_context_must_be_allowed_and_cache_can_be_skipped(repo, tmp_path):
    fake = FakeClient([GOOD, GOOD])
    coder = make(repo, tmp_path, fake)
    assert "not found or not allowed" in coder.draft_code("x", [".env"]).lower()
    coder.draft_code("add extra")
    assert "(cached" in coder.draft_code("add extra") and len(fake.chats) == 1
    assert "(cached" not in coder.draft_code("add extra", fresh=True) and len(fake.chats) == 2


def test_draft_offline_and_unset(repo, tmp_path):
    assert "offline" in make(repo, tmp_path / "a", FakeClient(online=False)).draft_code("x").lower()
    assert "not set" in make(repo, tmp_path / "b", None).draft_code("x")


# --- self-check ----------------------------------------------------------------------------------
def test_target_path_rules(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    ok = selfcheck.target_path("src/a.py", [root])
    assert ok and ok[1] == (root / "src" / "a.py").resolve()
    assert selfcheck.target_path("./src/a.py", [root])
    for bad in ("../a.py", "/abs/a.py", "C:/a.py", "", ".env", "keys.enc", "a.exe", "src/../../a.py", "secrets.py"):
        assert selfcheck.target_path(bad, [root]) is None, bad
    other = tmp_path / "other"
    other.mkdir()
    assert selfcheck.target_path("proj/a.py", [root, other])[0] == root
    assert selfcheck.target_path("a.py", [root, other]) is None


def test_python_and_js_syntax_checks():
    assert selfcheck.check_python("x = 1\n", "a.py") == []
    assert "syntax error on line 1" in selfcheck.check_python("def f(:\n", "a.py")[0]


def test_tests_are_off_unless_asked_for(repo):
    result = selfcheck.check_draft([repo], GOOD["files"])
    assert result["problems"] == [] and result["tests"].startswith("not run")
    assert result["clean"] == {"src/extra.py": "def extra():\n    return 1\n"}


def test_related_tests_run_in_a_copy_when_enabled(tmp_path):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "conftest.py").write_text("")
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")           # the original is wrong
    (root / "tests" / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    fixed = [{"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}]
    broken = [{"path": "calc.py", "content": "def add(a, b):\n    return a * b\n"}]
    good = selfcheck.check_draft([root], fixed, run_tests_too=True)
    assert good["problems"] == [] and good["tests"].startswith("tests passed")
    bad = selfcheck.check_draft([root], broken, run_tests_too=True)
    assert bad["problems"] and "FAILED" in bad["problems"][0]
    assert (root / "calc.py").read_text().endswith("a - b\n")                        # the real file is untouched
    nothing = selfcheck.check_draft([root], [{"path": "other.py", "content": "x = 1\n"}], run_tests_too=True)
    assert "no related tests" in nothing["tests"]


# --- vector search and reranking -----------------------------------------------------------------
@pytest.fixture
def indexed(repo, tmp_path, embedder):
    (repo / "src" / "audio.py").write_text(
        "def detect_silence(levels):\n    return all(l < 3 for l in levels)\n\n\n"
        "def play_sound(name):\n    return name\n")
    idx = Index(tmp_path / "i.db", [repo])
    idx.refresh()
    return idx


def fill_vectors(idx, embedder):
    while True:
        rows = idx.chunks_without_vectors(8)
        if not rows:
            return
        idx.add_vectors([r["id"] for r in rows], [embedder._vec(idx.vector_text(r)).tolist() for r in rows])


def test_vectors_find_by_meaning_words_and_survive_reindex(indexed, repo, embedder):
    assert indexed.vector_coverage() == 0 and indexed.vector_matrix() is None
    fill_vectors(indexed, embedder)
    assert indexed.vector_coverage() == 1.0
    hits = search.search(indexed, "levels silence", 3, qvec=embedder._vec("levels silence"))
    assert hits[0]["name"] == "detect_silence"
    (repo / "src" / "audio.py").write_text("def detect_silence(levels):\n    return False\n")
    indexed.refresh()
    assert 0 < indexed.vector_coverage() < 1                                       # only the changed file lost its vectors
    assert indexed.chunks_without_vectors(50) and all("audio.py" in indexed.display(r["path"])
                                                      for r in indexed.chunks_without_vectors(50))


def test_vectors_with_wrong_size_are_ignored(indexed, embedder):
    fill_vectors(indexed, embedder)
    assert search.by_vectors(indexed, [0.1, 0.2]) == []
    assert search.by_vectors(indexed, [0.0] * embedder.DIM) == []


def test_rerank_reorders_and_a_crashing_reranker_is_ignored(indexed):
    plain = search.search(indexed, "start meters budget", 3)
    last = plain[-1]
    flipped = search.search(indexed, "start meters budget", 3, rerank=lambda q, docs: [
        1.0 if f":{last['first']}-{last['last']}" in d.split("\n")[0] else 0.0 for d in docs])
    assert flipped[0]["name"] == last["name"] and "rerank" in flipped[0]

    def crash(q, docs):
        raise RuntimeError("gpu")
    assert [h["name"] for h in search.search(indexed, "start meters budget", 3, rerank=crash)] == \
        [h["name"] for h in plain]
    assert [h["name"] for h in search.search(indexed, "start meters budget", 3, rerank=lambda q, d: [1.0])] == \
        [h["name"] for h in plain]                                                  # wrong length: ignored


def test_deep_search_uses_embeddings_and_reranker_when_the_coder_is_up(repo, tmp_path, embedder):
    fake = FakeClient(embedder=embedder, scores=lambda q, docs: [float("start_blocker" in d) for d in docs])
    coder = make(repo, tmp_path, fake)
    coder.index.refresh()
    fill_vectors(coder.index, embedder)
    out = coder.search_code("which meter stops the start", deep=True)
    assert "start_blocker" in out.splitlines()[0] and fake.embedded == ["which meter stops the start"]
    fake.up = False
    assert "start_blocker" in coder.search_code("start blocker", deep=True)           # falls back quietly


# --- idle pre-computing ------------------------------------------------------------------------------
def pre(coder, quiet=True):
    clock = [1000.0]
    coder.last_activity = 1000.0 - (60 if quiet else 1)
    return precompute.Precomputer(coder, idle_after=30, clock=lambda: clock[0])


def test_precompute_makes_vectors_first_then_one_summary_per_step(repo, tmp_path, embedder):
    fake = FakeClient([{"summary": "Budget limits."}, {"summary": "Some notes."}], embedder=embedder)
    coder = make(repo, tmp_path, fake)
    p = pre(coder)
    steps = []
    for _ in range(10):
        did = p.step()
        if did is None:
            break
        steps.append(did)
    assert steps[0] == "vectors" and steps.count("summary") == 2 and steps[-1] == "summary"
    assert coder.index.vector_coverage() == 1.0
    assert "Budget limits." in coder.repo_map("src/budget")
    assert coder.status().count("2 file summaries") == 1


def test_precompute_waits_for_quiet_and_does_nothing_offline(repo, tmp_path, embedder):
    coder = make(repo, tmp_path, FakeClient([{"summary": "x"}], embedder=embedder))
    assert pre(coder, quiet=False).step() is None
    coder.client.up = False
    assert pre(coder).step() is None
    assert make(repo, tmp_path / "x", None).client is None
    assert precompute.Precomputer(make(repo, tmp_path / "y", None)).step() is None


def test_a_summary_is_dropped_if_the_file_changed_meanwhile(repo, tmp_path):
    coder = make(repo, tmp_path, FakeClient())
    coder.index.refresh()
    key = next(r["path"] for r in coder.index.files() if r["path"].endswith("budget.py"))
    old = coder.index.file_hash(key)
    (repo / "src" / "budget.py").write_text(PY + "\n# changed\n")
    coder.index.refresh()
    assert coder.index.set_summary(key, "describes old code", old) is False
    assert coder.index.set_summary(key, "describes new code", coder.index.file_hash(key)) is True


def test_status_reports_the_coder_link(repo, tmp_path):
    assert "Coder: not set up" in make(repo, tmp_path / "a", None).status()
    assert "Coder: online" in make(repo, tmp_path / "b", FakeClient()).status()
    assert "Coder: offline" in make(repo, tmp_path / "c", FakeClient(online=False)).status()


# --- key fingerprint and refused keys ------------------------------------------------------------
def test_fingerprint_matches_the_one_the_kaggle_side_prints():
    import coder_main
    key = "k" * 43 + "="
    assert CoderClient("https://x.test", key).fingerprint() == coder_main.key_fingerprint(key)
    assert len(coder_main.key_fingerprint(key)) == 8 and key not in coder_main.key_fingerprint(key)
    assert coder_main.key_fingerprint(key) != coder_main.key_fingerprint(key + "x")


def test_a_refused_key_is_not_retried_for_a_minute_and_says_how_to_compare():
    now = [0.0]
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(429, json={"error": "too many wrong keys, try later"})
    c = client_with(handler, clock=lambda: now[0])
    with pytest.raises(CoderError, match="fingerprint " + c.fingerprint()):
        c.chat([])
    for _ in range(5):
        assert c.online() is False
    assert len(calls) == 1                                   # no hammering the proxy with the wrong key
    now[0] += client_mod.REFUSED_MEMORY_SEC + 1
    assert c.online() is False and len(calls) == 2


def test_status_tells_a_refused_key_from_an_offline_coder(repo, tmp_path):
    refused = client_with(lambda r: httpx.Response(401, json={}))
    text = make(repo, tmp_path / "a", refused).status()
    assert "REFUSED the key" in text and refused.fingerprint() in text
    down = client_with(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert "offline (press Start Coder; key fingerprint here" in make(repo, tmp_path / "b", down).status()
    up = client_with(lambda r: httpx.Response(200, json={}))
    assert "online (key fingerprint" in make(repo, tmp_path / "c", up).status()
