"""MCP server that gives Claude Code the coder tools (code search, repo map, ratings).

Registered for this project in .mcp.json (needs `pip install mcp` in the .venv, MCP 2.x).
Index and cache live in ~/.jarvis-coder (CODER_HOME). Only the folders in CODER_ALLOWED_DIRS
are read (default: this repository); secrets files are never read. Logs nothing about content.
The Kaggle Coder is reached with CODER_URL (https://<ngrok domain>) and CODER_KEY, set in the
environment of Claude Code; without them the offline tools (search, repo map) still work.
"""
import os
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from coder import service  # noqa: E402
from coder.precompute import Precomputer  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
coder = service.Coder(service.default_home(), service.default_roots(REPO_ROOT))
mcp = MCPServer("coder")


@mcp.tool()
def search_code(query: str, limit: int = 8, deep: bool = False) -> str:
    """Find where something is in the code. Use words or a question ("where is the budget stop").
    Returns path:lines, kind, name and the first lines of each hit. Cheaper than reading files.
    deep=true also searches by meaning and reranks (needs the Coder to be running)."""
    return coder.search_code(query, limit, deep)


@mcp.tool()
def ask_repo(question: str) -> str:
    """Ask the Coder a question about the code. It reads the best matching pieces and answers
    briefly with file:line references, so you don't have to read the files. Cached while the
    files are unchanged. Offline, it returns the closest code instead."""
    return coder.ask_repo(question)


@mcp.tool()
def investigate(question: str, max_steps: int = 5) -> str:
    """For a hard question spanning several files ("trace what happens from X to Y", "how does the
    whole pairing work"): the Coder plans sub-questions, searches and answers each one separately,
    then writes one combined answer with file:line refs. Slower and more GPU time than ask_repo
    (about one model call per step plus two), so use ask_repo for anything a single search covers."""
    return coder.investigate(question, max_steps)


@mcp.tool()
def summarize(source: str, question: str = "") -> str:
    """Reduce a long log or file to what matters. `source` = a file path inside the allowed
    folders, or the text itself. `question` says what to look for. Works without the Coder too
    (keeps the lines that look like problems)."""
    return coder.summarize(source, question)


@mcp.tool()
def draft_code(spec: str, context_paths: list[str] | None = None, fresh: bool = False) -> str:
    """A first draft of a small piece of code, written by the Coder. NOTHING is written to disk.
    The draft is checked (paths, syntax, lint; tests if CODER_RUN_TESTS=1) and fixed up to twice.
    Always review it. context_paths = files the draft should follow; fresh=true skips the cache."""
    return coder.draft_code(spec, context_paths, fresh)


@mcp.tool()
def repo_map(path: str = "") -> str:
    """One line per file: length and the functions/classes in it. Pass a folder prefix to narrow."""
    return coder.repo_map(path)


@mcp.tool()
def rate(answer_id: int, verdict: str, correction: str = "") -> str:
    """Rate a coder answer ('good' or 'bad') so we learn which jobs it does well. With 'bad', pass
    `correction` (what the right answer was): it is remembered and warns the Coder the next time a
    similar question is asked."""
    return coder.rate(answer_id, verdict, correction)


@mcp.tool()
def coder_status() -> str:
    """How many files are indexed, which folders are allowed, and the ratings so far."""
    return coder.status()


@mcp.tool()
def reindex() -> str:
    """Re-read all allowed folders now (searches already refresh changed files every 30 s)."""
    return coder.reindex()


if __name__ == "__main__":
    if os.environ.get("CODER_PRECOMPUTE", "1") != "0":
        Precomputer(coder).start()      # idle work while the Coder is running; does nothing offline
    mcp.run()
