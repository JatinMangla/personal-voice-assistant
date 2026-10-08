"""The coder helper (scripts/coder): chunking, index, search, answer cache, ratings, safety."""
import os
from pathlib import Path

import pytest

from coder import cache as cache_mod
from coder import chunk, safety, search, service
from coder.index import Index

PY = '''import os

LIMIT = 5


def start_blocker(meters, budget):
    """Which meter stops the start?"""
    for m in meters:
        if m.used >= LIMIT:
            return m
    return None


class Budget:
    """GPU time left."""
    ratio = 0.9

    def usage_summary(self, now):
        return f"left {now}"

    def stop_at(self):
        return 0.99
'''

KT = '''package io.example

class SilenceDetector(private val threshold: Int) {
    fun isSilent(peak: Int): Boolean = peak < threshold
}

fun parseQr(text: String): String? {
    return text.takeIf { it.startsWith("jarvis") }
}
'''


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "budget.py").write_text(PY)
    (root / "app").mkdir()
    (root / "app" / "Detector.kt").write_text(KT)
    (root / "README.md").write_text("# Title\nintro\n## Setup\nrun it\n")
    return root


@pytest.fixture
def coder(repo, tmp_path):
    return service.Coder(tmp_path / "home", [repo])


# --- chunking ----------------------------------------------------------------------------
def test_python_chunks_are_functions_classes_and_methods():
    got = {(c.kind, c.name) for c in chunk.chunk_file("budget.py", PY)}
    assert ("function", "start_blocker") in got
    assert ("class", "Budget") in got
    assert ("method", "Budget.usage_summary") in got
    assert ("method", "Budget.stop_at") in got
    assert any(k == "module" for k, _ in got)  # the import and constant lines


def test_python_chunk_line_numbers_match_the_source():
    lines = PY.splitlines()
    for c in chunk.chunk_file("budget.py", PY):
        assert "\n".join(lines[c.start - 1:c.end]) == c.text


def test_broken_python_falls_back_to_windows():
    got = chunk.chunk_file("bad.py", "def (:\n" * 5)
    assert got and all(c.kind == "block" for c in got)


def test_kotlin_and_markdown_chunks():
    names = {c.name for c in chunk.chunk_file("D.kt", KT)}
    assert {"SilenceDetector", "isSilent", "parseQr"} <= names
    sections = [c.name for c in chunk.chunk_file("README.md", "# A\nx\n## B\ny\n")]
    assert sections == ["A", "B"]


def test_long_chunk_is_cut_into_windows_with_correct_lines():
    text = "def big():\n" + "".join(f"    x{i} = {i}\n" for i in range(300))
    pieces = chunk.chunk_file("big.py", text)
    assert len(pieces) > 2
    lines = text.splitlines()
    for c in pieces:
        assert c.end - c.start + 1 <= chunk.WINDOW
        assert "\n".join(lines[c.start - 1:c.end]) == c.text


def test_split_identifiers():
    assert chunk.split_identifiers("startBlocker start_blocker plain") == "start blocker start blocker"


# --- safety ------------------------------------------------------------------------------
@pytest.mark.parametrize("name", [".env", ".env.local", "memory.enc", "release.jks", "kaggle.json",
                                  "secrets.py", "my_password.txt", "id_rsa", "local.properties"])
def test_secret_files_are_recognised(name):
    assert safety.is_secret(Path("x") / name)


def test_secret_folder_is_recognised():
    assert safety.is_secret(Path("C:/Users/me/jarvis-keys/key.txt"))


def test_resolve_allowed_stays_inside_the_roots(repo, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n")
    assert safety.resolve_allowed(repo / "src" / "budget.py", [repo])
    assert safety.resolve_allowed(repo / "src" / ".." / ".." / "outside.py", [repo]) is None
    assert safety.resolve_allowed(outside, [repo]) is None
    (repo / ".env").write_text("KEY=1\n")
    assert safety.resolve_allowed(repo / ".env", [repo]) is None


# --- index -------------------------------------------------------------------------------
def test_index_skips_secrets_binaries_and_junk_folders(repo, tmp_path):
    (repo / ".env").write_text("TOKEN=abc\n")
    (repo / "keys.enc").write_text("zzz")
    (repo / "secrets.py").write_text("PASSWORD = 'hunter2'\n")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "lib.py").write_text("def hidden(): pass\n")
    (repo / "bin.txt").write_bytes(b"abc\0def")
    idx = Index(tmp_path / "i.db", [repo])
    idx.refresh()
    paths = {Path(r["path"]).name for r in idx.files()}
    assert paths == {"budget.py", "Detector.kt", "README.md"}


def test_refresh_only_rereads_changed_files(repo, tmp_path):
    idx = Index(tmp_path / "i.db", [repo])
    assert idx.refresh() == {"added": 3, "updated": 0, "removed": 0, "unchanged": 0}
    assert idx.refresh()["unchanged"] == 3
    target = repo / "src" / "budget.py"
    target.write_text(PY + "\n\ndef extra():\n    return 1\n")
    stats = idx.refresh()
    assert stats["updated"] == 1 and stats["unchanged"] == 2
    target.unlink()
    assert idx.refresh()["removed"] == 1
    assert idx.stats()["files"] == 2


def test_touched_but_identical_file_is_not_reindexed(repo, tmp_path):
    idx = Index(tmp_path / "i.db", [repo])
    idx.refresh()
    target = repo / "src" / "budget.py"
    os.utime(target, (target.stat().st_atime, target.stat().st_mtime + 100))
    assert idx.refresh()["unchanged"] == 3


def test_removed_chunks_leave_the_keyword_index(repo, tmp_path):
    idx = Index(tmp_path / "i.db", [repo])
    idx.refresh()
    (repo / "src" / "budget.py").write_text("x = 1\n")
    idx.refresh()
    assert search.search(idx, "start blocker meters") == [] or \
        all("start_blocker" not in h["text"] for h in search.search(idx, "start blocker meters"))


# --- search ------------------------------------------------------------------------------
def test_question_finds_the_function(coder):
    out = coder.search_code("which meter blocks the start")
    first = out.splitlines()[0]
    assert "src/budget.py" in first and "start_blocker" in first


def test_camel_case_name_is_found_from_plain_words(coder):
    assert "SilenceDetector" in coder.search_code("silence detector").splitlines()[0]


def test_symbol_name_beats_a_mention(coder):
    top = coder.search_code("parseQr").splitlines()[0]
    assert "parseQr" in top and "Detector.kt" in top


def test_path_words_are_searchable(coder):
    assert "README.md" in coder.search_code("readme setup")


def test_no_match_and_empty_query(coder):
    assert coder.search_code("zzzzqqq") == "No matches."
    assert "Give a question" in coder.search_code("   ")


def test_search_sees_a_new_file_after_the_refresh_interval(coder, repo, monkeypatch):
    assert coder.search_code("gizmo") == "No matches."
    (repo / "src" / "gizmo.py").write_text("def make_gizmo():\n    return 1\n")
    monkeypatch.setattr(service, "REFRESH_EVERY", 0.0)
    assert "make_gizmo" in coder.search_code("gizmo")


def test_search_output_is_short(coder):
    for line in coder.search_code("budget", 3).splitlines():
        assert len(line) < 200


def test_tests_rank_below_real_code_unless_asked_for(repo, tmp_path):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_budget.py").write_text(
        "def test_start_blocker_stops():\n    assert start_blocker([], None) is None\n")
    (repo / "src" / "latest.json").write_text('{"start_blocker": 1}\n')
    c = service.Coder(tmp_path / "h2", [repo])
    plain = c.search_code("start blocker").splitlines()
    assert "src/budget.py" in plain[0]
    asked = c.search_code("test start blocker").splitlines()
    assert "tests/test_budget.py" in asked[0]
    assert search.TEST_PATH.search("tests/x.py") and search.TEST_PATH.search("app/FooTest.kt")
    assert not search.TEST_PATH.search("src/latest.json") and not search.TEST_PATH.search("src/contest.py")


def test_rrf_prefers_items_found_by_both():
    scores = search.rrf(["a", "b"], ["b", "c"])
    assert max(scores, key=scores.get) == "b"


def test_hostile_query_does_not_break_fts(coder):
    for q in ['"', "a OR", "NEAR(", "x*y", "') DROP TABLE chunks; --", "AND NOT"]:
        assert isinstance(coder.search_code(q), str)


# --- repo map ----------------------------------------------------------------------------
def test_repo_map_lists_names_and_filters_by_prefix(coder):
    full = coder.repo_map()
    assert "src/budget.py" in full and "start_blocker" in full and "app/Detector.kt" in full
    only = coder.repo_map("src")
    assert "budget.py" in only and "Detector.kt" not in only
    assert coder.repo_map("nope") == "No files under that path."


# --- cache and ratings -------------------------------------------------------------------
def test_cache_hit_until_a_used_file_changes(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    c = cache_mod.Cache(tmp_path / "c.db")
    aid = c.put("ask_repo", "What does a do?", "it sets x", [f])
    hit = c.get("ask_repo", "  what does   A do? ", )
    assert hit == {"id": aid, "answer": "it sets x"}
    f.write_text("x = 2\n")
    assert c.get("ask_repo", "what does a do?") is None


def test_cache_is_stale_when_a_file_disappears_and_per_task(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    c = cache_mod.Cache(tmp_path / "c.db")
    c.put("summarize", "q", "ans", [f])
    assert c.get("ask_repo", "q") is None
    f.unlink()
    assert c.get("summarize", "q") is None


def test_ratings_are_stored_per_task(tmp_path, coder):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    a = coder.cache.put("summarize", "q1", "ans", [f])
    b = coder.cache.put("summarize", "q2", "ans", [f])
    assert "good" in coder.rate(a, "GOOD")
    coder.rate(b, "bad")
    coder.rate(b, "good")  # a later rating replaces the earlier one
    assert coder.cache.stats()["summarize"] == {"answers": 2, "good": 2, "bad": 0}
    assert "Unknown" in coder.rate(999, "good")
    assert "Unknown" in coder.rate(a, "meh")


def test_status_text(coder):
    coder.search_code("budget")
    text = coder.status()
    assert "3 files" in text and "Folders:" in text


# --- configuration -----------------------------------------------------------------------
def test_default_roots(monkeypatch, tmp_path):
    monkeypatch.delenv("CODER_ALLOWED_DIRS", raising=False)
    assert service.default_roots(tmp_path) == [tmp_path]
    monkeypatch.setenv("CODER_ALLOWED_DIRS", os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")]))
    assert service.default_roots(tmp_path) == [tmp_path / "a", tmp_path / "b"]
