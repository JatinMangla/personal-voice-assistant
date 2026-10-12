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
MAP_PROMPT_SYMBOLS = 6       # names per file in the map that goes INTO the prompt (experiment C)
MAP_CACHE_SEC = 120.0        # the map is rebuilt from the index at most this often
MAX_READ_BYTES = 5_000_000
MAX_CONTEXT_FILES = 6
MAX_CONTEXT_CHARS = 30_000
DRAFT_TRIES = 3
FOLLOWUP_FILE_CHARS = 14_000     # one requested file is trimmed to this (experiment D)
FOLLOWUP_TOTAL_CHARS = 40_000    # and all of them together to this, so the context cannot overflow
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
        self._map: tuple[float, str] | None = None   # (built at, text) for the prompt's repo map

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
    def prompt_map(self) -> str:
        """The repo map as it goes into the prompt (experiment C): one short line per file, the
        file's own summary when the Coder has written one, else its first few names. Byte-identical
        between calls within MAP_CACHE_SEC, so Ollama can reuse the cached prompt prefix.
        CODER_REPO_MAP=0 switches it off."""
        if os.environ.get("CODER_REPO_MAP", "1") == "0":
            return ""
        now = time.time()
        if self._map and now - self._map[0] < MAP_CACHE_SEC:
            return self._map[1]
        lines = []
        for r in self.index.files():
            shown = r["summary"] or ", ".join(self.index.symbols(r["path"])[:MAP_PROMPT_SYMBOLS])
            lines.append(f"{self.index.display(r['path'])} ({r['lines']}L): {shown}".rstrip(": "))
        text = "\n".join(lines)
        self._map = (now, text)
        return text

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
        asked_for: list[str] = []
        extra_pieces: list[dict] = []
        try:
            hits = self._deep_search(question, self._chunks_for(question))
            if not hits:
                return "No matching code found."
            repo_map = self.prompt_map()
            may_ask = self._followup_on()
            notes = self._notes_for("ask_repo", question)
            result = tasks.ask(self.client, question, hits, repo_map=repo_map, may_ask=may_ask,
                               notes=notes)
            wanted = result["need"] or (self._cited_but_unseen(result["refs"], hits) if may_ask else [])
            if may_ask and wanted:
                extra_pieces, asked_for = self._fetch_needed(wanted, hits)
                if extra_pieces:
                    result = tasks.ask_again(self.client, question, hits, extra_pieces,
                                             repo_map=repo_map, notes=notes)
                elif not result["answer"]:
                    # It asked for files none of which could be read: let it answer with what it has.
                    result = tasks.ask(self.client, question, hits, repo_map=repo_map, may_ask=False,
                                       notes=notes)
        except Offline:
            return self._without_coder(question, OFFLINE_NOTE)
        except CoderError as e:
            return self._without_coder(question, f"The Coder could not answer ({e}). ")
        refs = self._valid_refs(result["refs"], hits, asked_for)
        unverified = self._unverified_names(result["answer"])
        text = f"{result['answer']}\nRefs: {', '.join(refs) or 'none given'}\nConfidence: {result['confidence']}"
        if asked_for:
            text += "\n(it asked to read these too: " + ", ".join(asked_for) + ")"
        if unverified:
            text += ("\nNot found anywhere in the indexed code (the model may have invented them; verify "
                     "before relying on them): " + ", ".join(unverified))
        if result.get("cut"):
            text += "\n(the answer was cut at the length limit: ask about one part at a time for the rest)"
        if result["confidence"] == "low" or not result["answer"] or unverified or result.get("cut"):
            return text + "\n(not cached; check the code or ask again with other words)"
        # The follow-up files are part of the answer too: if one of them changes, the cached
        # answer must go stale with it.
        files = [Path(h["file"]) for h in hits + extra_pieces]
        answer_id = self.cache.put("ask_repo", question, text, files)
        return f"{text}\n(answer #{answer_id}; rate it with the rate tool)" + self._warning()

    def investigate(self, question: str, max_steps: int = tasks.MAX_STEPS) -> str:
        """A hard, cross-file question answered in steps: the Coder plans sub-questions, searches
        and answers each one on its own, then writes the findings up as one answer. Costs about one
        model call per step plus two, so it is for questions that one `ask_repo` cannot cover.

        It is sequential on purpose: the Coder answers one request at a time (measured), so running
        the steps at once would not be faster, only harder to follow."""
        self.touch()
        if not question.strip():
            return "Ask a question about the code."
        cached = self.cache.get("investigate", question)
        if cached:
            return f"{cached['answer']}\n(cached, answer #{cached['id']})"
        self.index.refresh(max_age=REFRESH_EVERY)
        if not self.client:
            return self._without_coder(question, "CODER_URL and CODER_KEY are not set. ")
        max_steps = max(1, min(max_steps, tasks.MAX_STEPS))
        repo_map = self.prompt_map()
        seen_files: list[Path] = []
        lines: list[str] = []
        try:
            steps = tasks.plan(self.client, question, repo_map=repo_map, max_steps=max_steps)
            findings: list[tuple[str, str]] = []
            for step in steps:
                # Measured 2026-10-12: with ASK_CHUNKS (6) per step, three overlapping
                # sub-questions saw only 10 files between them, FEWER than the 11 a single wide
                # ask_repo sees with ASK_CHUNKS_WIDE. Splitting the question must not narrow the
                # reading, so every step gets the wide budget.
                hits = self._deep_search(step, ASK_CHUNKS_WIDE)
                if not hits:
                    findings.append((step, "no matching code was found for this step"))
                    lines.append(f"- {step}\n  nothing found")
                    continue
                result = tasks.ask(self.client, step, hits, repo_map=repo_map)
                refs = self._valid_refs(result["refs"], hits)
                seen_files += [Path(h["file"]) for h in hits]
                answer = result["answer"] or "no answer for this step"
                findings.append((step, answer + (f" (refs: {', '.join(refs)})" if refs else "")))
                lines.append(f"- {step}\n  {answer[:300]}" + (f"\n  refs: {', '.join(refs)}" if refs else ""))
            final = tasks.write_up(self.client, question, findings, repo_map=repo_map)
        except Offline:
            return self._without_coder(question, OFFLINE_NOTE)
        except CoderError as e:
            return self._without_coder(question, f"The Coder could not investigate ({e}). ")
        shown = {self.index.display(p.as_posix()) for p in seen_files}
        refs = [r for r in final["refs"] if r.rsplit(":", 1)[0] in shown or r in shown][:10]
        unverified = self._unverified_names(final["answer"])
        text = (f"{final['answer']}\nRefs: {', '.join(refs) or 'none given'}\n"
                f"Confidence: {final['confidence']}\nLooked at it in {len(findings)} steps:\n"
                + "\n".join(lines))
        if unverified:
            text += ("\nNot found anywhere in the indexed code (the model may have invented them; verify "
                     "before relying on them): " + ", ".join(unverified))
        if final["confidence"] == "low" or not final["answer"] or unverified:
            return text + "\n(not cached; check the code or ask again with other words)"
        answer_id = self.cache.put("investigate", question, text, list(dict.fromkeys(seen_files)))
        return f"{text}\n(answer #{answer_id}; rate it with the rate tool)" + self._warning()

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
    def _valid_refs(refs: list[str], hits: list[dict], extra: list[str] | None = None) -> list[str]:
        """Only references to files that were really shown to the model (it may invent others).
        `extra`: files it asked for in a follow-up round, which it was also shown."""
        shown = {h["path"] for h in hits} | set(extra or [])
        return [r for r in refs if r.rsplit(":", 1)[0] in shown or r in shown][:8]

    def _cited_but_unseen(self, refs: list[str], hits: list[dict]) -> list[str]:
        """Files the answer leans on that the model was never shown (experiment D's real trigger).

        Measured 2026-10-09: invited to ask for files, the model never did in 17 questions. It
        answers cross-file questions at "high" confidence from the repo map's one-liners and cites
        files it has not read. Given only an unrelated piece it DOES ask correctly, so the mechanism
        works and only the trigger was wrong: a citation of something it was not shown is objective
        evidence that it needed that file, so we fetch it and ask again instead of trusting the guess.
        """
        shown = {h["path"] for h in hits}
        wanted: list[str] = []
        for ref in refs:
            path = ref.rsplit(":", 1)[0].strip()
            if path and path not in shown and path not in wanted and self._resolve_source(path):
                wanted.append(path)
        return wanted[:tasks.MAX_NEED]

    def _notes_for(self, task: str, question: str) -> list[dict]:
        """Experiment E: past answers to similar questions that were rated wrong. CODER_NOTES=0 off."""
        if os.environ.get("CODER_NOTES", "1") == "0":
            return []
        return self.cache.notes_for(task, question)

    @staticmethod
    def _followup_on() -> bool:
        """Experiment D: one extra round for files the answer leans on but was never shown.

        OFF by default, because it was measured and it lost: traces 27/30 -> 23/30 (one trace fell
        10/10 -> 6/10) for 35 % more model time, since the extra round displaces steps from the final
        answer. It fetches the right files (hand-checked), so it stays behind CODER_FOLLOWUP=1 for
        single narrow questions; `investigate` is the better answer for cross-file ones.
        """
        return os.environ.get("CODER_FOLLOWUP", "0") != "0"

    def _fetch_needed(self, wanted: list[str], hits: list[dict]) -> tuple[list[dict], list[str]]:
        """The files the model asked for, as chunk-shaped pieces, through the normal safety checks.
        Files it was already shown are skipped, and the total is trimmed so the context cannot
        overflow. Returns (pieces, the paths that were really read)."""
        already = {h["path"] for h in hits}
        pieces, names, used = [], [], 0
        for raw in wanted[:tasks.MAX_NEED]:
            path = self._resolve_source(raw.strip().split(":")[0])
            if path is None:
                continue
            shown = self.index.display(path.as_posix())
            if shown in already or shown in names:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            room = min(FOLLOWUP_FILE_CHARS, FOLLOWUP_TOTAL_CHARS - used)
            if room < 400:
                break
            cut = text[:room]
            pieces.append({"path": shown, "file": path.as_posix(), "first": 1,
                           "last": len(cut.splitlines()), "kind": "file", "name": "",
                           "text": cut + ("\n[... file continues ...]" if len(text) > room else "")})
            names.append(shown)
            used += len(cut)
        return pieces, names

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
        return f"{body}\n(answer #{answer_id}; rate it with the rate tool)" + self._warning()

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
        draft = {"files": [], "notes": "", "test": ""}
        check = {"problems": ["no draft"], "tests": "", "selftest": "", "clean": {}}
        try:
            for attempt in range(DRAFT_TRIES):
                draft = tasks.draft(self.client, spec, context, problems or None,
                                    draft["files"] if problems else None)
                check = selfcheck.check_draft(self.roots, draft["files"], selftest=draft.get("test", ""))
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
        if check.get("selftest") and check["selftest"] != "not asked for":
            status += f"\nSelf-test: {check['selftest']}"
        text = (f"[Draft by the coder model. NOT written to disk. Review before using.]\n{blocks}\n"
                f"Notes: {draft['notes'] or 'none'}\n{status}")
        answer_id = self.cache.put("draft_code", key, text, used) if ok else None
        return text + (f"\n(answer #{answer_id}; rate it with the rate tool)" if answer_id else "")

    # --- housekeeping -----------------------------------------------------------------------
    def _warning(self) -> str:
        """The Coder's free-limit warning, from the last health check (no extra request)."""
        return self.client.warning_line() if self.client else ""

    def rate(self, answer_id: int, verdict: str, correction: str = "") -> str:
        """`correction` (only useful with 'bad'): what was actually right. It is remembered and
        shown the next time a similar question is asked (experiment E)."""
        verdict = verdict.strip().lower()
        if not self.cache.rate(answer_id, verdict):
            return "Unknown answer id, or the verdict was not 'good' or 'bad'."
        text = f"Rated answer {answer_id}: {verdict}."
        if verdict == "bad" and self.cache.add_note(answer_id, correction):
            text += (" Remembered, so a similar question is warned next time"
                     + (" (with your correction)." if correction.strip() else "."))
        return text

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
        text += self.client.warning_line() if self.client else ""   # link_status() just refreshed it
        if ratings:
            text += "\nAnswers: " + "; ".join(
                f"{task} {v['answers']} (good {v['good']}, bad {v['bad']})" for task, v in sorted(ratings.items()))
        return text

    def reindex(self) -> str:
        stats = self.index.refresh()
        return "Re-read: " + ", ".join(f"{k} {v}" for k, v in stats.items())
