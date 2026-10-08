"""Split a source file into chunks that mean something (a function, a class, a section),
so a search hit is the whole function and not a random 500 characters."""
import ast
import re
import warnings
from dataclasses import dataclass

MAX_LINES = 120     # a longer piece is cut into windows
WINDOW = 60
OVERLAP = 10

TEXT_EXTENSIONS = {".py", ".kt", ".kts", ".java", ".js", ".ts", ".html", ".css", ".md", ".json",
                   ".yml", ".yaml", ".toml", ".txt", ".gradle", ".xml", ".properties", ".cfg",
                   ".ini", ".sh", ".ps1", ".bat"}

_KOTLIN_DEF = re.compile(
    r"^\s*(?:(?:private|public|internal|protected|override|suspend|data|open|abstract|sealed|"
    r"inline|enum|companion|operator|tailrec)\s+)*(fun|class|object|interface)\s+"
    r"(?:<[^>]+>\s*)?([A-Za-z_][\w.]*)")
_JS_DEF = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:(function)\*?\s+(\w+)|(class)\s+(\w+)|(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?(?:\(|function))")
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$")


@dataclass(frozen=True)
class Chunk:
    start: int   # first line, 1-based
    end: int     # last line, 1-based
    kind: str    # function, method, class, object, interface, section, module, block
    name: str    # "" when it has no name
    text: str


def split_identifiers(text: str) -> str:
    """'startBlocker' and 'start_blocker' both give 'start blocker', so a search for the
    words finds the name. Only identifiers that are really compound are expanded."""
    pieces = []
    for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text):
        if "_" in ident or re.search(r"[a-z0-9][A-Z]", ident):
            spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", ident).replace("_", " ")
            pieces.append(spaced.lower())
    return " ".join(pieces)


def chunk_file(path: str, text: str) -> list[Chunk]:
    lines = text.splitlines()
    if not lines:
        return []
    suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if suffix == ".py":
        found = _python(text, lines)
        if found is not None:
            return _bound(found)
    elif suffix in (".kt", ".kts", ".java"):
        return _bound(_by_starts(lines, _kotlin_starts(lines)))
    elif suffix in (".js", ".ts"):
        return _bound(_by_starts(lines, _js_starts(lines)))
    elif suffix == ".md":
        return _bound(_by_starts(lines, _heading_starts(lines)))
    return _bound(_windows(lines, 0, len(lines), "block", ""))


def _make(lines: list[str], first: int, last: int, kind: str, name: str) -> Chunk:
    """first/last are 0-based, last exclusive."""
    return Chunk(first + 1, last, kind, name, "\n".join(lines[first:last]))


def _windows(lines, first, last, kind, name) -> list[Chunk]:
    out, at = [], first
    while at < last:
        end = min(at + WINDOW, last)
        out.append(_make(lines, at, end, kind, name))
        if end == last:
            break
        at = end - OVERLAP
    return out


def _bound(chunks: list[Chunk]) -> list[Chunk]:
    """Cut any chunk longer than MAX_LINES into overlapping windows; drop empty ones."""
    out = []
    for c in chunks:
        if not c.text.strip():
            continue
        if c.end - c.start + 1 <= MAX_LINES:
            out.append(c)
        else:
            parts = c.text.splitlines()
            for w in _windows(parts, 0, len(parts), c.kind, c.name):
                out.append(Chunk(c.start + w.start - 1, c.start + w.end - 1, c.kind, c.name, w.text))
    return out


def _python(text: str, lines: list[str]) -> list[Chunk] | None:
    try:
        with warnings.catch_warnings():  # old files with bad escapes like "\S" are still readable
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    pieces: list[tuple[int, int, str, str]] = []  # first, last (0-based, exclusive), kind, name

    def start_of(node) -> int:
        decorators = [d.lineno for d in getattr(node, "decorator_list", [])]
        return min([node.lineno, *decorators]) - 1

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            pieces.append((start_of(node), node.end_lineno, "function", node.name))
        elif isinstance(node, ast.ClassDef):
            methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            head_end = start_of(methods[0]) if methods else node.end_lineno
            pieces.append((start_of(node), head_end, "class", node.name))
            for m in methods:
                pieces.append((start_of(m), m.end_lineno, "method", f"{node.name}.{m.name}"))
    covered = [False] * len(lines)
    for first, last, _, _ in pieces:
        for i in range(first, min(last, len(lines))):
            covered[i] = True
    out = [_make(lines, f, l, k, n) for f, l, k, n in pieces]
    i = 0
    while i < len(lines):  # module-level lines that no def/class covers (imports, constants)
        if covered[i]:
            i += 1
            continue
        j = i
        while j < len(lines) and not covered[j]:
            j += 1
        out.append(_make(lines, i, j, "module", ""))
        i = j
    return sorted(out, key=lambda c: c.start)


def _kotlin_starts(lines):
    for i, line in enumerate(lines):
        m = _KOTLIN_DEF.match(line)
        if m:
            kind = "function" if m.group(1) == "fun" else m.group(1)
            yield i, kind, m.group(2)


def _js_starts(lines):
    for i, line in enumerate(lines):
        m = _JS_DEF.match(line)
        if m:
            if m.group(1):
                yield i, "function", m.group(2)
            elif m.group(3):
                yield i, "class", m.group(4)
            else:
                yield i, "function", m.group(5)


def _heading_starts(lines):
    in_code = False
    for i, line in enumerate(lines):
        if line.startswith("```"):
            in_code = not in_code
        m = None if in_code else _HEADING.match(line)
        if m:
            yield i, "section", m.group(2)


def _by_starts(lines, starts) -> list[Chunk]:
    """Each definition runs to the next one; lines before the first are a 'module' chunk."""
    starts = list(starts)
    if not starts:
        return _windows(lines, 0, len(lines), "block", "")
    out = []
    if starts[0][0] > 0:
        out.append(_make(lines, 0, starts[0][0], "module", ""))
    for n, (at, kind, name) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        out.append(_make(lines, at, end, kind, name))
    return out
