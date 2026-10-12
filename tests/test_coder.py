"""The coder helper (scripts/coder): chunking, index, search, answer cache, ratings, safety."""
import os
from pathlib import Path

import pytest

from coder import cache as cache_mod
from coder import chunk, safety, search, selfcheck, service
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


# --- chunking: the other languages -------------------------------------------------------
# Each sample is real-shaped on purpose (receivers, generics, modifiers), because those are
# what a naive regex gets wrong. The expected set is (kind, name, start line).
LANGS = {
    "svc/server.go": ('''package main

type Store interface {
\tGet(key string) (string, error)
}

type Server struct {
\taddr string
}

func NewServer(addr string) *Server {
\treturn &Server{addr: addr}
}

func (s *Server) Handle(key string) error {
\treturn nil
}
''', {("interface", "Store", 3), ("struct", "Server", 7),
      ("function", "NewServer", 11), ("function", "Handle", 15)}),

    "svc/budget.rs": ('''use std::time::Duration;

pub struct Meter {
    used: u64,
}

pub trait Clock {
    fn now(&self) -> Duration;
}

impl<T: Clock> Meter<T> {
    pub async fn add(&mut self, secs: u64) -> u64 {
        secs
    }
}

pub(crate) fn stop_reason() -> Option<&'static str> {
    None
}
''', {("struct", "Meter", 3), ("trait", "Clock", 7), ("function", "now", 8),
      ("impl", "Meter", 11), ("function", "add", 12), ("function", "stop_reason", 17)}),

    "svc/Budget.cs": ('''namespace Jarvis.Budget
{
    public interface IMeter
    {
        int Used { get; }
    }

    public class GpuMeter : IMeter
    {
        private readonly int _limit;

        public GpuMeter(int limit)
        {
            _limit = limit;
        }

        public async Task<bool> ShouldStopAsync(int minutes)
        {
            return minutes >= _limit;
        }

        public Dictionary<string, List<int>> Group(string key)
        {
            return null;
        }
    }

    public enum Severity { Warn, Stop }
}
''', {("namespace", "Jarvis", 1), ("interface", "IMeter", 3), ("class", "GpuMeter", 8),
      ("function", "GpuMeter", 12), ("function", "ShouldStopAsync", 17),
      ("function", "Group", 22), ("enum", "Severity", 28)}),

    "svc/reminders.rb": ('''require "json"

module Jarvis
  class Reminders < Base
    def initialize(store)
      @store = store
    end

    def due?(now)
      false
    end
  end

  def self.load_all(path)
    {}
  end
end
''', {("module", "Jarvis", 3), ("class", "Reminders", 4), ("function", "initialize", 5),
      ("function", "due?", 9), ("function", "self.load_all", 14)}),

    "web/Devices.php": ('''<?php

interface TokenStore
{
    public function find(string $hash): ?array;
}

abstract class Devices implements TokenStore
{
    public function find(string $hash): ?array
    {
        return null;
    }

    private static function expired(int $made): bool
    {
        return false;
    }
}

function pair_code(): string
{
    return "";
}
''', {("interface", "TokenStore", 3), ("function", "find", 5), ("class", "Devices", 8),
      ("function", "find", 10), ("function", "expired", 15),
      ("function", "pair_code", 21)}),

    "db/schema.sql": ('''-- the coder index
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY
);

create unique index files_by_path on files(path);

CREATE MATERIALIZED VIEW recent AS SELECT path FROM files;

CREATE OR REPLACE FUNCTION bump_version() RETURNS trigger AS $$
BEGIN
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

ALTER TABLE files ADD COLUMN lines INTEGER;
''', {("table", "files", 2), ("index", "files_by_path", 6), ("view", "recent", 8),
      ("function", "bump_version", 10), ("table", "files", 16)}),

    "ios/Recorder.swift": ('''import AVFoundation

protocol LevelSink {
    func level(_ peak: Float)
}

final class Recorder: LevelSink {
    func level(_ peak: Float) {}

    public func isSilent(threshold: Float) -> Bool {
        return true
    }
}

extension Recorder {
    static func make() -> Recorder { Recorder() }
}

struct Settings {
    var quiet: Bool
}
''', {("protocol", "LevelSink", 3), ("function", "level", 4), ("class", "Recorder", 7),
      ("function", "level", 8), ("function", "isSilent", 10),
      ("extension", "Recorder", 15), ("function", "make", 16), ("struct", "Settings", 19)}),

    "jvm/Search.scala": ('''package jarvis

object Search {
  def rrf(ranks: Seq[Int]): Double = 0.0
}

trait Ranker {
  def score(q: String): Double
}

case class Hit(path: String)

class KeywordRanker extends Ranker {
  override def score(q: String): Double = 0.0
}
''', {("object", "Search", 3), ("function", "rrf", 4), ("trait", "Ranker", 7),
      ("function", "score", 8), ("class", "Hit", 11), ("class", "KeywordRanker", 13),
      ("function", "score", 14)}),

    "plugin/levels.lua": ('''local M = {}

function M.peak(samples)
  return 0
end

local function silent(samples)
  return true
end
''', {("function", "M.peak", 3), ("function", "silent", 7)}),

    "app/player.dart": ('''import 'dart:async';

mixin Logging {
  void log(String line) {}
}

abstract class Sink {
  void play(List<int> bytes);
}

class SpeechPlayer extends Sink with Logging {
  final List<List<int>> queue = [];
}

enum PlayState { idle, playing }
''', {("mixin", "Logging", 3), ("class", "Sink", 7), ("class", "SpeechPlayer", 11),
      ("enum", "PlayState", 15)}),

    "ex/budget.ex": ('''defmodule Jarvis.Budget do
  @limit 30

  defstruct used: 0, limit: @limit

  def new(limit) do
    %__MODULE__{limit: limit}
  end

  defp over?(budget) do
    budget.used >= budget.limit
  end

  defmacro warn_at(ratio) do
    quote do: unquote(ratio)
  end
end
''', {("module", "Jarvis.Budget", 1), ("function", "new", 6), ("function", "over?", 10),
      ("function", "warn_at", 14)}),

    "tools/report.pl": ('''use strict;

package Report;

sub summarise {
    my ($lines) = @_;
    return scalar @$lines;
}
''', {("module", "Report", 3), ("function", "summarise", 5)}),

    "stats/fit.R": ('''limit <- 30

fit_model <- function(df) {
  lm(y ~ x, data = df)
}
''', {("function", "fit_model", 3)}),
}


@pytest.mark.parametrize("path", sorted(LANGS))
def test_other_languages_find_their_definitions(path):
    text, expected = LANGS[path]
    got = {(c.kind, c.name, c.start) for c in chunk.chunk_file(path, text)}
    assert expected <= got, f"missing {expected - got}"


@pytest.mark.parametrize("path", sorted(LANGS))
def test_other_languages_chunk_line_numbers_match_the_source(path):
    """The same invariant the Python chunks keep: a chunk's text is exactly its own lines."""
    text, _ = LANGS[path]
    lines = text.splitlines()
    for c in chunk.chunk_file(path, text):
        assert "\n".join(lines[c.start - 1:c.end]) == c.text


@pytest.mark.parametrize("path", sorted(LANGS))
def test_other_languages_invent_no_definitions(path):
    """Nothing is named that is not in the expected set: a false definition is worse than a
    window, because search would show the wrong function name for a hit."""
    text, expected = LANGS[path]
    named = {(c.kind, c.name, c.start) for c in chunk.chunk_file(path, text) if c.name}
    assert named == expected, f"unexpected {named - expected}"


# "return Compute(x);" and "while (Ready())" look exactly like a C# method head without the
# modifier rule, so every call inside a method would have started a new chunk.
@pytest.mark.parametrize("line", ["        return Compute(x);",
                                  "        if (Used >= _limit) return true;",
                                  "    foreach (var m in meters)",
                                  "    while (Ready())",
                                  "    using (var f = File.Open(path))",
                                  "        _ = Task.Run(() => Work());",
                                  "    public int Used { get; private set; }",
                                  "        private readonly int _limit;"])
def test_csharp_statements_are_not_taken_for_definitions(line):
    assert list(chunk._cs_starts([line])) == []


def test_elixir_defstruct_is_not_a_definition():
    """defstruct has no name of its own, so matching it would name the first field instead."""
    got = {c.name for c in chunk.chunk_file("b.ex", "defstruct used: 0, limit: 30\n")}
    assert "used" not in got


def test_sql_comments_and_inserts_are_not_definitions():
    text = "-- CREATE TABLE commented (x TEXT);\nINSERT INTO files VALUES (1);\n"
    assert [c.name for c in chunk.chunk_file("s.sql", text) if c.name] == []


def test_sql_quoted_name_keeps_the_whole_name():
    got = {c.name for c in chunk.chunk_file("s.sql", 'CREATE TABLE "my table" (x TEXT);\n')}
    assert "my table" in got


def test_c_and_cpp_are_indexed_but_left_on_window_chunking():
    """C/C++ declarations span lines and hide behind macros, so there is no regex for them on
    purpose: being indexed as windows still beats being invisible to search."""
    for suffix in (".c", ".h", ".cpp", ".hpp"):
        assert suffix in chunk.TEXT_EXTENSIONS
        assert suffix not in chunk.BY_REGEX
    src = "int is_silent(const float *buf, int n) {\n    return 0;\n}\n"
    pieces = chunk.chunk_file("dsp.c", src)
    assert pieces and all(c.kind == "block" and c.name == "" for c in pieces)


@pytest.mark.parametrize("path,text", [
    ("empty.go", "\n\n\n"),
    ("data.rs", "// just a comment\n"),
    ("notes.sql", "SELECT 1;\n"),
    ("blob.cs", "}\n}\n"),
    ("x.rb", "puts 1\n"),
])
def test_a_file_with_no_definitions_falls_back_to_windows(path, text):
    pieces = chunk.chunk_file(path, text)
    assert all(c.kind == "block" for c in pieces)       # never crashes, never invents a name
    lines = text.splitlines()
    for c in pieces:
        assert "\n".join(lines[c.start - 1:c.end]) == c.text


@pytest.mark.parametrize("suffix", [".js", ".cs", ".rs", ".sql", ".go", ".kt"])
def test_one_enormous_line_is_skipped_instead_of_backtracked(suffix):
    """A minified bundle puts megabytes on one line. Measured: these regexes do NOT blow up on
    it (their cost is linear), so this pins the cheaper promise -- such a line yields one
    unnamed window instead of a bogus definition named after 12,000 characters of noise."""
    huge = "    public static " + "a" * (chunk.MAX_LINE * 3) + "("
    pieces = chunk.chunk_file("big" + suffix, huge + "\n")
    assert len(pieces) == 1 and pieces[0].name == ""


def test_many_long_lines_just_under_the_cap_still_finish():
    """The cap cannot help here, so this is what really pins the no-backtracking property:
    200 lines of modifier soup, each just under the cap, measured at 0.16 s."""
    line = "    public static readonly async partial " * 95 + "int X("
    assert len(line) < chunk.MAX_LINE
    pieces = chunk.chunk_file("soup.cs", (line + "\n") * 200)
    assert pieces


# --- safety ------------------------------------------------------------------------------
@pytest.mark.parametrize("name", [".env", ".env.local", "memory.enc", "release.jks", "kaggle.json",
                                  "secrets.py", "my_password.txt", "id_rsa", "local.properties"])
def test_secret_files_are_recognised(name):
    assert safety.is_secret(Path("x") / name)


def test_secret_folder_is_recognised():
    assert safety.is_secret(Path("C:/Users/me/jarvis-keys/key.txt"))


def test_no_indexable_extension_is_also_a_secret_extension():
    """The guard that widening TEXT_EXTENSIONS must never trip: if ever a suffix lands in both
    sets, is_secret still wins in index._walk, but the overlap means somebody intended to read
    key material as source and should say so out loud."""
    assert not (chunk.TEXT_EXTENSIONS & safety.SECRET_SUFFIXES)


@pytest.mark.parametrize("name", ["server.key", "cert.pem", "api_key.sql", "secrets.go",
                                  "db_password.rb", "credentials.cs", "apikey.php",
                                  "passwd.pl", "my.private_key.rs"])
def test_secrets_stay_refused_now_that_more_extensions_are_indexed(name):
    assert safety.is_secret(Path("repo") / name)


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


def test_an_answer_rated_bad_is_never_served_from_the_cache(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    c = cache_mod.Cache(tmp_path / "c.db")
    first = c.put("ask_repo", "q", "wrong answer", [f])
    assert c.get("ask_repo", "q")["id"] == first
    c.rate(first, "bad")
    assert c.get("ask_repo", "q") is None            # asked again instead of repeating it
    second = c.put("ask_repo", "q", "right answer", [f])
    assert c.get("ask_repo", "q") == {"id": second, "answer": "right answer"}
    c.rate(second, "good")
    assert c.get("ask_repo", "q")["id"] == second    # a good answer still comes back


# --- the measurement harness must never outrank the code it measures -----------------------------
def test_the_benchmark_file_does_not_outrank_the_code_it_asks_about(repo, tmp_path):
    """Measured 2026-10-12: coder_bench.py holds all 14 benchmark questions as string literals, so
    it matched every one and pushed the real code down on 4 of them. It is a judge, not an answer."""
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts" / "coder_bench.py").write_text(
        'RETRIEVAL = [\n'
        '    ("Which meter stops the start?", "src/budget.py", "start_blocker"),\n'
        ']\n', encoding="utf-8")
    coder = service.Coder(tmp_path / "home", [repo])
    coder.index.refresh()
    hits = search.search(coder.index, "which meter stops the start", 5)
    paths = [h["path"] for h in hits]
    assert paths, "nothing found at all"
    assert "src/budget.py" in paths[0], f"the judge file won: {paths}"


def test_a_question_about_the_benchmark_itself_still_finds_it(repo, tmp_path):
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts" / "coder_bench.py").write_text(
        "def summary_line(run):\n"
        '    """One line per saved benchmark variant."""\n'
        "    return run\n", encoding="utf-8")
    coder = service.Coder(tmp_path / "home", [repo])
    coder.index.refresh()
    hits = search.search(coder.index, "which function prints the benchmark summary line", 5)
    assert any("coder_bench.py" in h["path"] for h in hits), "asking about the judge must find it"


def test_the_judge_penalty_is_the_harshest_one():
    """Below tests and prose: a file whose job is to quote questions is never the answer."""
    assert search.JUDGE_PENALTY < search.TEST_PENALTY < search.PROSE_PENALTY < 1.0
    for name in ("scripts/coder_bench.py", "coder_bench.py", "scripts/coder_compare_methods.py"):
        assert search.JUDGE_PATH.search(name), name
    for name in ("src/coder_main.py", "scripts/coder/search.py", "benchmarks/mine.py"):
        assert not search.JUDGE_PATH.search(name), name


# --- G: two projects (or a branch worktree) side by side ----------------------------------------
TWO_A = '''def start_blocker(meters):
    """Which meter forbids starting right now."""
    return next((m for m in meters if m.used >= 5), None)
'''
TWO_B = '''def start_blocker(limits):
    """The quota that prevents a launch at this moment."""
    return limits[0] if limits else None
'''


@pytest.fixture
def two_projects(tmp_path):
    one = (tmp_path / "jarvis").resolve()
    two = (tmp_path / "other-project").resolve()
    for root, code in ((one, TWO_A), (two, TWO_B)):
        (root / "src").mkdir(parents=True)
        (root / "src" / "budget.py").write_text(code, encoding="utf-8")
    return one, two


def test_a_hit_says_which_project_it_is_in(two_projects, tmp_path):
    """With CODER_ALLOWED_DIRS holding several folders (another repo, or a git worktree of another
    branch), an answer that did not name the project would be useless."""
    one, two = two_projects
    coder = service.Coder(tmp_path / "home", [one, two])
    coder.index.refresh()
    hits = search.search(coder.index, "start_blocker", 8)
    paths = [h["path"] for h in hits]
    assert "jarvis/src/budget.py" in paths and "other-project/src/budget.py" in paths
    assert all(p.split("/")[0] in ("jarvis", "other-project") for p in paths)


def test_each_project_is_searchable_on_its_own_wording(two_projects, tmp_path):
    one, two = two_projects
    coder = service.Coder(tmp_path / "home", [one, two])
    coder.index.refresh()
    only_two = search.search(coder.index, "quota that prevents a launch", 5)
    assert only_two and only_two[0]["path"].startswith("other-project/")
    only_one = search.search(coder.index, "which meter forbids starting", 5)
    assert only_one and only_one[0]["path"].startswith("jarvis/")


def test_repo_map_can_be_narrowed_to_one_project(two_projects, tmp_path):
    one, two = two_projects
    coder = service.Coder(tmp_path / "home", [one, two])
    coder.index.refresh()
    text = coder.repo_map("other-project")
    assert "other-project/src/budget.py" in text and "jarvis/" not in text


def test_a_draft_must_name_the_project_and_cannot_escape(two_projects):
    one, two = two_projects
    roots = [one, two]
    for allowed, expected in (("jarvis/src/new.py", one), ("other-project/src/new.py", two)):
        got = selfcheck.target_path(allowed, roots)
        assert got and got[0] == expected, allowed
    for refused in ("src/new.py", "../escape.py", "nope/src/new.py", "/abs/new.py"):
        assert selfcheck.target_path(refused, roots) is None, refused


def test_a_file_is_only_read_through_its_own_project_name(two_projects, tmp_path):
    one, two = two_projects
    coder = service.Coder(tmp_path / "home", [one, two])
    coder.index.refresh()
    assert coder._resolve_source("jarvis/src/budget.py") is not None
    assert coder._resolve_source("other-project/src/budget.py") is not None
    assert coder._resolve_source("../outside.py") is None

def test_a_project_prefix_cannot_be_used_to_reach_somewhere_else(two_projects, tmp_path):
    """The prefixed form must resolve ONLY inside the project it names, and never escape."""
    one, two = two_projects
    (one / "src" / "only_in_one.py").write_text("x = 1\n", encoding="utf-8")
    (two / "secretish.py").write_text("y = 2\n", encoding="utf-8")
    coder = service.Coder(tmp_path / "home", [one, two])
    coder.index.refresh()

    got = coder._resolve_source("jarvis/src/only_in_one.py")
    assert got and got.parent.parent == one
    # project two's name must not fetch project one's file
    assert coder._resolve_source("other-project/src/only_in_one.py") is None
    for escape in ("jarvis/../other-project/secretish.py", "jarvis/../../outside.py",
                   "../escape.py", "jarvis/.env", "nope/src/budget.py"):
        resolved = coder._resolve_source(escape)
        assert resolved is None or any(
            str(resolved).startswith(str(r)) for r in (one, two)), escape
