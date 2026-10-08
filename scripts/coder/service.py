"""The tools Claude Code calls, as plain functions returning short text (every output token
is a token I have to read). scripts/coder_mcp.py only wraps these.

Without a running Coder (Kaggle) everything that can work offline still does: search, repo
map, a log reducer for `summarize`, cached answers. The tools that need the model say so at once.
"""
import hashlib
import os
import re
import time
from pathlib import Path

from . import safety, selfcheck, tasks
from . import search as search_mod
from .cache import Cache
from .client import CoderClient, CoderError, Offline
from .index import Index

REFRESH_EVERY = 30.0   # seconds: searching re-reads changed files at most this often
SNIPPET_LINES = 3
MAP_FILE_LIMIT = 150
MAP_SYMBOLS = 12
ASK_CHUNKS = 6
ASK_CHUNKS_WIDE = 14     # questions that trace a flow through many files need more of the code
WIDE_WORDS = ("step by step", "trace", "end to end", "from the moment", "walk me through", "every file",
              "all the files", "which files", "flow of")
WIDE_MIN_WORDS = 30
MAX_UNVERIFIED = 6
DEEP_POOL = 30
MIN_VECTOR_COVERAGE = 0.5
MAX_READ_BYTES = 5_000_000
MAX_CONTEXT_FILES = 6
MAX_CONTEXT_CHARS = 30_000
DRAFT_TRIES = 3
OFFLINE_NOTE = "The Coder is offline (press Start Coder on GitHub). "
FENCE = "````"          # longer than the usual ``` so code containing fences stays intact
_FROM_ENV = object()


def default_home() -> Path:
    return Path(os.environ.get("CODER_HOME") or Path.home() / ".jarvis-coder")


def default_roots(repo_root: Path) -> list[Path]:
    """CODER_ALLOWED_DIRS (folders separated by ';' or ':'), else only this repository."""
    raw = os.environ.get("CODER_ALLOWED_DIRS", "")
    folders = [Path(p) for p in raw.replace(";", os.pathsep).split(os.pathsep) if p.strip()]
    return folders or [repo_root]


class Coder:
    def __init__(self, home: Path, roots: list[Path], client=_FROM_ENV):
        self.roots = [Path(r).resolve() for r in roots]
        self.index = Index(home / "index.db", self.roots)
        self.cache = Cache(home / "cache.db")
        self.client = CoderClient.from_env() if client is _FROM_ENV else client
        self.last_activity = time.time()   # the idle pre-computing waits for quiet

    def touch(self) -> None:
        self.last_activity = time.time()

    # --- search ----------------------------------------------------------------------------
    @staticmethod
    def _format_hits(hits: list[dict]) -> str:
        out = []
        for n, h in enumerate(hits, 1):
            label = f"{h['kind']} {h['name']}".strip()
            lines = [l.rstrip()[:110] for l in h["text"].splitlines() if l.strip()][:SNIPPET_LINES]
            out.append(f"{n}. {h['path']}:{h['first']}-{h['last']}  {label}\n   " + "\n   ".join(lines))
        return "\n".join(out)

    def _deep_search(self, query: str, limit: int) -> list[dict]:
        """Keywords + names + meaning vectors, then reranked, when the Coder is up. Falls back
        step by step to the plain offline search."""
        qvec = rerank = None
        client = self.client
        if client and self.index.vector_coverage() >= MIN_VECTOR_COVERAGE:
            try:
                qvec = client.embed([query])[0]
            except (Offline, CoderError):
                qvec = None
        if client:
            rerank = client.rerank
        try:
            return search_mod.search(self.index, query, limit, qvec=qvec, rerank=rerank, pool=DEEP_POOL)
        except (Offline, CoderError):
            return search_mod.search(self.index, query, limit)

    def search_code(self, query: str, limit: int = 8, deep: bool = False) -> str:
        """Where is this in the code? Hits with path:lines, kind, name and the first lines.
        deep=True also uses meaning-search and the reranker (needs the Coder; a bit slower)."""
        self.touch()
        if not query.strip():
            return "Give a question or some words to search for."
        self.index.refresh(max_age=REFRESH_EVERY)
        limit = max(1, min(limit, 20))
        hits = self._deep_search(query, limit) if deep else search_mod.search(self.index, query, limit)
        return self._format_hits(hits) if hits else "No matches."

    def repo_map(self, path: str = "") -> str:
        """One line per file: its length and what it is for (or the names defined in it)."""
        self.touch()
        self.index.refresh(max_age=REFRESH_EVERY)
        rows = self.index.files(path.replace("\\", "/").lstrip("/"))
        if not rows:
            return "No files under that path."
        out = []
        for r in rows[:MAP_FILE_LIMIT]:
            names = self.index.symbols(r["path"])
            shown = ", ".join(names[:MAP_SYMBOLS]) + (f", +{len(names) - MAP_SYMBOLS} more" if len(names) > MAP_SYMBOLS else "")
            text = r["summary"] or shown
            out.append(f"{self.index.display(r['path'])} ({r['lines']} lines): {text}".rstrip(": "))
        if len(rows) > MAP_FILE_LIMIT:
            out.append(f"... {len(rows) - MAP_FILE_LIMIT} more files; pass a longer path to narrow it.")
        return "\n".join(out)

    # --- tools that use the coder model ----------------------------------------------------
    def ask_repo(self, question: str) -> str:
        """Answer a question about the code from the best matching pieces (the Coder reads them
        so Claude doesn't have to). Same question + unchanged files = instant cached answer."""
        self.touch()
        if not question.strip():
            return "Ask a question about the code."
        cached = self.cache.get("ask_repo", question)
        if cached:
            return f"{cached['answer']}\n(cached, answer #{cached['id']})"
        self.index.refresh(max_age=REFRESH_EVERY)
        if not self.client:
            return self._without_coder(question, "CODER_URL and CODER_KEY are not set. ")
        try:
            hits = self._deep_search(question, self._chunks_for(question))
            if not hits:
                return "No matching code found."
            result = tasks.ask(self.client, question, hits)
        except Offline:
            return self._without_coder(question, OFFLINE_NOTE)
        except CoderError as e:
            return self._without_coder(question, f"The Coder could not answer ({e}). ")
        refs = self._valid_refs(result["refs"], hits)
        unverified = self._unverified_names(result["answer"])
        text = f"{result['answer']}\nRefs: {', '.join(refs) or 'none given'}\nConfidence: {result['confidence']}"
        if unverified:
            text += ("\nNot found anywhere in the indexed code (the model may have invented them; verify "
                     "before relying on them): " + ", ".join(unverified))
        if result.get("cut"):
            text += "\n(the answer was cut at the length limit: ask about one part at a time for the rest)"
        if result["confidence"] == "low" or not result["answer"] or unverified or result.get("cut"):
            return text + "\n(not cached; check the code or ask again with other words)"
        files = [Path(h["file"]) for h in hits]
        answer_id = self.cache.put("ask_repo", question, text, files)
        return f"{text}\n(answer #{answer_id}; rate it with the rate tool)"

    @staticmethod
    def _chunks_for(question: str) -> int:
        """Most questions need ~6 pieces of code. A long question, or one that asks to trace a flow
        step by step, spans many files, and 6 pieces would force the model to guess the rest."""
        low = question.lower()
        wide = len(question.split()) >= WIDE_MIN_WORDS or any(w in low for w in WIDE_WORDS)
        return ASK_CHUNKS_WIDE if wide else ASK_CHUNKS

    def _unverified_names(self, answer: str) -> list[str]:
        """Names the model put in `backticks` (functions, classes, modules) that do not exist
        anywhere in the indexed code or file names. Cheap, and it catches the typical invention."""
        candidates: list[str] = []
        for span in re.findall(r"`([^`\n]{2,100})`", answer):
            if "/" in span or " " in span.strip():
                continue                                   # a path or a command, not a name
            for part in span.replace("()", "").split("."):
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{3,}", part) and part not in candidates:
                    candidates.append(part)
        missing = []
        with self.index.lock:
            for name in candidates:
                found = self.index.conn.execute(
                    "SELECT 1 FROM chunks WHERE instr(text, ?) > 0 OR instr(name, ?) > 0 LIMIT 1",
                    (name, name)).fetchone() or self.index.conn.execute(
                    "SELECT 1 FROM files WHERE instr(path, ?) > 0 LIMIT 1", (name,)).fetchone()
                if not found:
                    missing.append(name)
        return missing[:MAX_UNVERIFIED]

    def _without_coder(self, question: str, why: str) -> str:
        hits = search_mod.search(self.index, question, ASK_CHUNKS)
        return why + "Closest code instead:\n" + (self._format_hits(hits) if hits else "No matches.")

    @staticmethod
    def _valid_refs(refs: list[str], hits: list[dict]) -> list[str]:
        """Only references to files that were really shown to the model (it may invent others)."""
        shown = {h["path"] for h in hits}
        return [r for r in refs if r.rsplit(":", 1)[0] in shown or r in shown][:8]

    def _resolve_source(self, source: str) -> Path | None:
        """A path (absolute, or relative to the first allowed folder) that is allowed to be read."""
        for candidate in (Path(source), self.roots[0] / source):
            try:
                allowed = safety.resolve_allowed(candidate, self.roots)
            except (OSError, ValueError):
                allowed = None
            if allowed:
                return allowed
        return None

    def summarize(self, source: str, question: str = "") -> str:
        """Reduce a long file or log to the facts that matter. `source` is a file path inside the
        allowed folders, or the text itself. Works offline too (keeps the lines that look like
        problems), the Coder makes it a real summary."""
        self.touch()
        file_path = None
        if "\n" not in source and len(source) < 400 and source.strip():
            file_path = self._resolve_source(source.strip())
            if file_path is None:
                return "File not found, or not inside the allowed folders (or it is a secrets file)."
            if file_path.stat().st_size > MAX_READ_BYTES:
                return "File is too big (over 5 MB); give a smaller piece."
            text = file_path.read_text(encoding="utf-8", errors="replace")
            key = f"{file_path}|{question}"
        else:
            text = source
            key = hashlib.sha256((source + "|" + question).encode()).hexdigest()
        cached = self.cache.get("summarize", key)
        if cached:
            return f"{cached['answer']}\n(cached, answer #{cached['id']})"
        if not self.client:
            return self._reduced(text, "CODER_URL and CODER_KEY are not set. ")
        try:
            data = tasks.summarize(self.client, text, question)
        except Offline:
            return self._reduced(text, OFFLINE_NOTE)
        except CoderError as e:
            return self._reduced(text, f"The Coder could not summarize ({e}). ")
        body = data["summary"] + "".join(f"\n- {p}" for p in data["key_points"])
        if data.get("cut"):
            body += "\n(the answer was cut at the length limit: ask a narrower question for the rest)"
        answer_id = self.cache.put("summarize", key, body, [file_path] if file_path else [])
        return f"{body}\n(answer #{answer_id}; rate it with the rate tool)"

    @staticmethod
    def _reduced(text: str, why: str) -> str:
        return why + "Reduced without a model (first lines, last lines, lines that look like problems):\n" \
            + tasks.shrink(text, 6000)

    def draft_code(self, spec: str, context_paths: list[str] | None = None, fresh: bool = False) -> str:
        """A first draft of a small piece of code. Nothing is written to disk. The draft is checked
        first (paths, syntax, lint, optionally tests) and fixed up to twice if the check fails."""
        self.touch()
        if not spec.strip():
            return "Describe what to write."
        if not self.client:
            return "CODER_URL and CODER_KEY are not set, so no draft was made."
        context, used = [], []
        for p in (context_paths or [])[:MAX_CONTEXT_FILES]:
            path = self._resolve_source(p)
            if path is None:
                return f"Context file not found or not allowed: {p}"
            context.append((self.index.display(path.as_posix()),
                            path.read_text(encoding="utf-8", errors="replace")[:MAX_CONTEXT_CHARS]))
            used.append(path)
        key = spec + "|" + ",".join(sorted(str(u) for u in used))
        if not fresh:
            cached = self.cache.get("draft_code", key)
            if cached:
                return f"{cached['answer']}\n(cached, answer #{cached['id']}; pass fresh=true for a new try)"
        problems: list[str] = []
        draft, check = {"files": [], "notes": ""}, {"problems": ["no draft"], "tests": "", "clean": {}}
        try:
            for attempt in range(DRAFT_TRIES):
                draft = tasks.draft(self.client, spec, context, problems or None,
                                    draft["files"] if problems else None)
                check = selfcheck.check_draft(self.roots, draft["files"])
                if not check["problems"]:
                    break
                problems = check["problems"]
        except Offline:
            return OFFLINE_NOTE + "No draft was made."
        except CoderError as e:
            return f"The Coder could not write the draft ({e})."
        ok = not check["problems"]
        blocks = "\n".join(f"{FENCE}{rel}\n{content}\n{FENCE}" for rel, content in check["clean"].items())
        status = (f"Checks: passed (paths, syntax, lint). Tests: {check['tests']}" if ok else
                  f"Checks: FAILED after {DRAFT_TRIES} tries: " + "; ".join(check["problems"])[:600])
        text = (f"[Draft by the coder model. NOT written to disk. Review before using.]\n{blocks}\n"
                f"Notes: {draft['notes'] or 'none'}\n{status}")
        answer_id = self.cache.put("draft_code", key, text, used) if ok else None
        return text + (f"\n(answer #{answer_id}; rate it with the rate tool)" if answer_id else "")

    # --- housekeeping -----------------------------------------------------------------------
    def rate(self, answer_id: int, verdict: str) -> str:
        if self.cache.rate(answer_id, verdict.strip().lower()):
            return f"Rated answer {answer_id}: {verdict.strip().lower()}."
        return "Unknown answer id, or the verdict was not 'good' or 'bad'."

    def status(self) -> str:
        s = self.index.stats()
        ratings = self.cache.stats()
        if not self.client:
            link = "not set up (CODER_URL and CODER_KEY)"
        else:
            link = self.client.link_status()
        summaries = self.index.conn.execute("SELECT COUNT(*) FROM files WHERE summary IS NOT NULL").fetchone()[0]
        text = (f"Index: {s['files']} files, {s['chunks']} chunks, {summaries} file summaries, "
                f"{self.index.vector_coverage():.0%} with meaning vectors. Coder: {link}. Folders: "
                + ", ".join(str(r) for r in self.index.roots))
        if ratings:
            text += "\nAnswers: " + "; ".join(
                f"{task} {v['answers']} (good {v['good']}, bad {v['bad']})" for task, v in sorted(ratings.items()))
        return text

    def reindex(self) -> str:
        stats = self.index.refresh()
        return "Re-read: " + ", ".join(f"{k} {v}" for k, v in stats.items())
