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

    def online(self, background=False):
        self.online_calls = getattr(self, 'online_calls', 0) + 1
        return self.up

    def link_status(self):
        return "online" if self.up else "offline (press Start Coder)"

    def warning_line(self, check=False):
        note = getattr(self, "warning", None)
        return "\nFree limit: " + note if note else ""

    def chat(self, messages, schema=None, max_tokens=800, background=False):
        self.chats.append(messages)
        if not self.up:
            raise Offline("offline")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)

    def embed(self, texts, background=False):
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
    assert "Confidence: low" in coder.ask_repo("which meter stops the start?")
    assert coder.cache.get("ask_repo", "which meter stops the start?") is None
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


# --- ranking: code before prose, exact names first (first run, 2026-10-09) -------------------
def test_code_outranks_the_docs_that_describe_it(repo, tmp_path):
    # Same shape as the real src/budget.py: the docstring uses the words of the question.
    (repo / "src" / "budget.py").write_text(
        'def start_blocker(meters, budget):\n'
        '    """The limit that forbids starting now, if any: already at the auto-stop, or too\n'
        '    little left for a session (start-up alone takes a few minutes)."""\n'
        '    return budget.over_stop(meters) or budget.too_little_left(meters, 5)\n')
    (repo / "docs").mkdir()
    (repo / "docs" / "PLAN.md").write_text(
        "The limit that forbids starting now is checked before the GPU work starts; "
        "it refuses when too little time is left for a session.\n" * 3)
    (repo / "README.md").write_text("Which limit forbids starting now: see the budget guard.\n" * 3)
    coder = make(repo, tmp_path, None)
    top = coder.search_code("which limit forbids starting now").splitlines()[0]
    assert "src/budget.py" in top and "start_blocker" in top
    # The prose is still found, and comes first when the question asks for the docs.
    assert "docs/PLAN.md" in coder.search_code("limit forbids starting now")
    asked = coder.search_code("docs about the limit that forbids starting")
    assert "docs/PLAN.md" in asked.splitlines()[0] or "README.md" in asked.splitlines()[0]


def test_a_name_that_matches_every_word_comes_first(repo, tmp_path):
    (repo / "src" / "other.py").write_text(
        "def helper():\n    # start blocker start blocker start blocker\n    return 1\n" * 4)
    coder = make(repo, tmp_path, None)
    assert "start_blocker" in coder.search_code("start blocker").splitlines()[0]


# --- the pre-computing must never keep the Coder (and the GPU) alive ----------------------------------
def test_with_nothing_left_to_do_it_sends_no_request_and_waits_ten_minutes(repo, tmp_path, embedder):
    fake = FakeClient([{"summary": "a"}, {"summary": "b"}, {"summary": "c"}], embedder=embedder)
    coder = make(repo, tmp_path, fake)
    p = pre(coder)
    while p.step():                    # do all the work there is
        pass
    calls = (fake.online_calls, len(fake.embedded), len(fake.chats))
    p.wait = 0
    for _ in range(5):
        assert p.step() is None
    assert p.wait == precompute.IDLE_RECHECK_SEC
    assert (fake.online_calls, len(fake.embedded), len(fake.chats)) == calls     # not one more request


def test_when_the_coder_is_offline_it_waits_five_minutes_between_tries(repo, tmp_path, embedder):
    coder = make(repo, tmp_path, FakeClient(online=False, embedder=embedder))
    p = pre(coder)
    assert p.step() is None and p.wait == precompute.OFFLINE_RECHECK_SEC


def test_the_loop_waits_the_chosen_time_and_returns_to_normal_afterwards(repo, tmp_path, embedder):
    coder = make(repo, tmp_path, FakeClient([{"summary": "a"}] * 5, embedder=embedder))
    p = pre(coder)
    waits = []
    original = p.stopping.wait

    def fake_wait(seconds):
        waits.append(seconds)
        return len(waits) > 3                       # stop after three rounds
    p.stopping.wait = fake_wait
    p._loop()
    assert waits[0] == p.tick and set(waits) <= {p.tick, precompute.IDLE_RECHECK_SEC}


def test_background_calls_carry_the_marker_and_foreground_calls_do_not():
    seen = []

    def handler(request):
        seen.append(request.headers.get("x-coder-background"))
        return httpx.Response(200, json={"message": {"content": "x"}, "embeddings": [[0.1]]})
    c = client_with(handler)
    c.online()
    c.online(background=True)
    c.chat([], background=True)
    c.embed(["a"], background=True)
    c.chat([])
    assert seen == [None, "1", "1", "1", None]


# --- long and complex tasks (found by the first long/complex benchmark, 2026-10-09) -------------------
def test_json_cut_off_by_the_length_limit_is_repaired_not_lost():
    from coder.client import repair_truncated_json
    cut = '{"summary": "Three problems.", "key_points": ["CUDA out of memory", "Connection reset", "KeyError tok'
    value = parse_json(cut)
    assert value["summary"] == "Three problems." and value["key_points"][:2] == ["CUDA out of memory", "Connection reset"]
    assert repair_truncated_json('{"a": [1, 2, {"b": "x') == {"a": [1, 2, {"b": "x"}]}
    assert repair_truncated_json("not json") is None
    assert repair_truncated_json('{"answer": "long text that never ends') == {"answer": "long text that never ends"}
    with pytest.raises(CoderError):
        parse_json("totally not json")


def test_a_cut_off_summary_is_kept_and_marked(repo, tmp_path):
    cut = '{"summary": "Found two problems.", "key_points": ["first problem", "second problem", "third prob'
    fake = FakeClient([cut])
    fake.last_done_reason = "length"
    out = make(repo, tmp_path, fake).summarize("line\n" * 10 + "ERROR boom", "list every problem")
    assert out.startswith("Found two problems.") and "- first problem" in out and "- second problem" in out
    assert "cut at the length limit" in out


def test_summaries_get_a_big_enough_output_budget():
    assert tasks.SUMMARY_TOKENS >= 1000 and tasks.ASK_TOKENS >= 900
    seen = {}

    class Spy(FakeClient):
        def chat(self, messages, schema=None, max_tokens=800, background=False):
            seen["max"] = max_tokens
            return json.dumps({"summary": "s", "key_points": []})
    tasks.summarize(Spy(), "x", "q")
    assert seen["max"] == tasks.SUMMARY_TOKENS


def test_long_or_trace_questions_read_more_of_the_code():
    c = service.Coder
    assert c._chunks_for("where is the budget stop?") == service.ASK_CHUNKS
    assert c._chunks_for("Trace what happens when the button is pressed") == service.ASK_CHUNKS_WIDE
    assert c._chunks_for("walk me through the start-up") == service.ASK_CHUNKS_WIDE
    assert c._chunks_for("word " * 40) == service.ASK_CHUNKS_WIDE


def test_names_the_model_invented_are_flagged_and_the_answer_is_not_cached(repo, tmp_path):
    answer = {"answer": "It calls `start_blocker` and then `kaggle.api.competitions.submissions_create()` "
                        "using `src/budget.py`.", "refs": ["src/budget.py:1-6"], "confidence": "high"}
    fake = FakeClient([answer, answer])
    coder = make(repo, tmp_path, fake)
    out = coder.ask_repo("which meter stops the start?")
    assert "Not found anywhere in the indexed code" in out
    assert "competitions" in out and "submissions_create" in out
    assert "start_blocker" not in out.split("Not found anywhere")[1]          # the real name is not flagged
    assert "answer #" not in out
    coder.ask_repo("which meter stops the start?")
    assert len(fake.chats) == 2                                                # asked again: not cached


def test_real_names_paths_and_commands_are_not_flagged(repo, tmp_path):
    coder = make(repo, tmp_path, None)
    coder.index.refresh()
    text = "Run `python scripts/x.py`, see `src/budget.py`, call `start_blocker()` or `budget.over_stop` or `meters`."
    assert coder._unverified_names(text) == []
    assert coder._unverified_names("uses `totally_invented_helper()` and `Imaginary.thing`") == [
        "totally_invented_helper", "Imaginary", "thing"]


def test_a_wide_question_sends_more_pieces_to_the_model(repo, tmp_path):
    for i in range(12):
        (repo / "src" / f"mod{i}.py").write_text(f"def step_{i}():\n    return 'start button pressed {i}'\n")
    fake = FakeClient([ANSWER])
    coder = make(repo, tmp_path, fake)
    coder.ask_repo("Trace step by step what happens when the start button is pressed")
    sent = "".join(m["content"] for m in fake.chats[0])
    assert sent.count("<file path=") > service.ASK_CHUNKS


# --- experiment C: the repo map in the prompt (docs/CODER_IMPROVEMENTS.md) -------------------------
def test_the_repo_map_is_sent_with_the_question_and_can_be_switched_off(repo, tmp_path, monkeypatch):
    fake = FakeClient([ANSWER, ANSWER])
    coder = make(repo, tmp_path, fake)
    coder.ask_repo("which meter stops the start?")
    system = fake.chats[0][0]["content"]
    assert "<repo_map>" in system and "src/budget.py" in system and "start_blocker" in system
    assert "never name anything absent from this list" in system
    monkeypatch.setenv("CODER_REPO_MAP", "0")
    coder._map = None
    coder.cache.conn.execute("DELETE FROM answers")        # ask the model again, not the cache
    coder.cache.conn.commit()
    coder.ask_repo("which meter stops the start?")
    assert len(fake.chats) == 2 and "<repo_map>" not in fake.chats[1][0]["content"]


def test_the_map_is_identical_between_calls_so_ollama_can_cache_it(repo, tmp_path):
    """Ollama only reuses a cached prompt prefix if it is byte-identical, so the map must not
    change between questions within its cache window."""
    second = {**ANSWER, "answer": "a different answer"}
    fake = FakeClient([ANSWER, second])
    coder = make(repo, tmp_path, fake)
    coder.ask_repo("where is the start blocker")
    coder.ask_repo("what does over_stop return")      # also in this fixture's budget.py
    assert len(fake.chats) == 2
    assert fake.chats[0][0]["content"] == fake.chats[1][0]["content"]
    assert fake.chats[0][1]["content"] != fake.chats[1][1]["content"]   # only the question changed


def test_the_map_prefers_a_written_summary_over_bare_names(repo, tmp_path):
    coder = make(repo, tmp_path, FakeClient())
    coder.index.refresh()
    key = next(r["path"] for r in coder.index.files() if r["path"].endswith("budget.py"))
    assert "start_blocker" in coder.prompt_map()
    coder.index.set_summary(key, "Free-limit meters and the auto-stop.", coder.index.file_hash(key))
    coder._map = None
    line = next(l for l in coder.prompt_map().splitlines() if "budget.py" in l)
    assert line.endswith("Free-limit meters and the auto-stop.") and "L):" in line


def test_a_huge_map_is_shortened_but_still_lists_every_file():
    """150 files with long name lists: the names get cut, every path stays."""
    lines = [f"src/module{i}.py (120L): " + ", ".join(f"function_with_a_long_name_{j}" for j in range(12))
             for i in range(150)]
    out = tasks.shrink_map("\n".join(lines), limit=14_000)
    assert len(out) <= 14_000
    assert out.count("\n") + 1 == 150                     # no file was dropped
    assert "src/module0.py" in out and "src/module149.py" in out
    assert "..." in out and "more files not listed" not in out


def test_an_absurd_map_drops_files_last_and_says_how_many():
    lines = [f"src/a_very_long_path_number_{i}/and_a_file_name_{i}.py (9L): names" for i in range(2000)]
    out = tasks.shrink_map("\n".join(lines), limit=3_000)
    assert len(out) <= 3_000 and "more files not listed" in out
    assert "src/a_very_long_path_number_0" in out


def test_system_text_without_a_map_is_unchanged():
    assert tasks.system_text("") == tasks.SYSTEM
    assert tasks.system_text("   ") == tasks.SYSTEM
    assert tasks.system_text("src/x.py (3L): f").startswith(tasks.SYSTEM)
