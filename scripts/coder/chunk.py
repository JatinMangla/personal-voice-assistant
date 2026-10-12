"""Split a source file into chunks that mean something (a function, a class, a section),
so a search hit is the whole function and not a random 500 characters."""
import ast
import re
import warnings
from dataclasses import dataclass

MAX_LINES = 120     # a longer piece is cut into windows
WINDOW = 60
OVERLAP = 10
# A minified bundle or a generated file puts a whole file on one line, which cannot be a
# definition worth naming. Measured: none of the regexes below actually backtrack badly (cost
# is linear in the line length), so this is not a hang fix -- it just stops the indexer
# spending its time on lines no human wrote.
MAX_LINE = 4000

TEXT_EXTENSIONS = {".py", ".kt", ".kts", ".java", ".js", ".ts", ".html", ".css", ".md", ".json",
                   ".yml", ".yaml", ".toml", ".txt", ".gradle", ".xml", ".properties", ".cfg",
                   ".ini", ".sh", ".ps1", ".bat",
                   # other languages, so the Coder can be pointed at a Go or C# project too
                   ".go", ".rs", ".cs", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx",
                   ".rb", ".rake", ".php", ".sql", ".swift", ".scala", ".sc", ".lua", ".r",
                   ".pl", ".pm", ".dart", ".ex", ".exs",
                   ".jsx", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}

_KOTLIN_DEF = re.compile(
    r"^\s*(?:(?:private|public|internal|protected|override|suspend|data|open|abstract|sealed|"
    r"inline|enum|companion|operator|tailrec)\s+)*(fun|class|object|interface)\s+"
    r"(?:<[^>]+>\s*)?([A-Za-z_][\w.]*)")
_JS_DEF = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:(function)\*?\s+(\w+)|(class)\s+(\w+)|(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?(?:\(|function))")
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$")

# One regex per language, in the style of _KOTLIN_DEF: it must match the FIRST line of a
# definition, and group "name" is what search will show. C and C++ are deliberately NOT here:
# a declaration can span lines, a return type can be any template soup, and macros hide the
# shape, so a regex would name the wrong function in a fair share of hits. They are indexed
# and cut into line windows instead, which still beats being invisible to search.
_GO_DEF = re.compile(
    r"^(?:func\s+(?:\([^)]*\)\s*)?(?P<fname>\w+)"          # func Name / func (r T) Name
    r"|type\s+(?P<tname>\w+)\s+(?P<tkind>struct|interface)\b)")
# "impl<T: Clock> Meter<T>" has no space after the keyword, hence \s* and the generic skip.
_RUST_DEF = re.compile(
    r"^\s*(?:(?:pub(?:\([^)]*\))?|async|unsafe|extern(?:\s+\"[^\"]*\")?|const|default)\s+)*"
    r"(?P<kw>fn|struct|enum|trait|impl|mod|union)\s*"
    r"(?:<[^<>]*>)?\s+(?P<name>[A-Za-z_]\w*)")
# C#: a method is told from a field by the "(" that must follow the name on the same line, and
# from a plain call like "return Compute(x);" by a modifier or a return type being in front.
_CS_MODIFIER = (r"public|private|protected|internal|static|sealed|abstract|virtual|override|"
                r"async|partial|readonly|extern|unsafe|new|required")
_CS_DEF = re.compile(
    rf"^\s*(?P<mods>(?:(?:{_CS_MODIFIER})\s+)*)"
    r"(?:(?P<kw>class|struct|interface|enum|record|namespace)\s+(?P<name>[A-Za-z_]\w*)"
    r"|(?:[A-Za-z_][\w.?\[\]]*(?:<[^()]*>[\w?\[\]]*)?\s+)?"       # the return type, if any
    r"(?P<mname>[A-Za-z_]\w*)\s*(?:<[^<>()]*>\s*)?\()")
_RUBY_DEF = re.compile(r"^\s*(?P<kw>def|class|module)\s+(?P<name>[A-Za-z_][\w.:?!=]*)")
_PHP_DEF = re.compile(
    r"^\s*(?:(?:public|private|protected|static|final|abstract|readonly)\s+)*"
    r"(?P<kw>function|class|interface|trait|enum)\s+&?\s*(?P<name>[A-Za-z_]\w*)")
# SQL: CREATE/ALTER of anything, plus the optional OR REPLACE / IF NOT EXISTS noise.
_SQL_DEF = re.compile(
    r"^\s*(?:CREATE|ALTER)\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+|UNIQUE\s+|MATERIALIZED\s+)*"
    r"(?P<kw>TABLE|VIEW|INDEX|TRIGGER|FUNCTION|PROCEDURE|TYPE)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[\w.]+)", re.I)
_SWIFT_DEF = re.compile(
    r"^\s*(?:(?:public|private|internal|fileprivate|open|static|final|override|mutating|"
    r"@objc|convenience|required|dynamic)\s+)*"
    r"(?P<kw>func|class|struct|enum|protocol|extension|actor)\s+(?P<name>[A-Za-z_]\w*)")
_SCALA_DEF = re.compile(
    r"^\s*(?:(?:private|protected|final|sealed|abstract|implicit|override|case|lazy)\s+)*"
    r"(?P<kw>def|class|object|trait|enum)\s+(?P<name>[A-Za-z_]\w*)")
_DART_DEF = re.compile(
    r"^\s*(?:(?:abstract|final|base|sealed|interface|mixin)\s+)*"
    r"(?P<kw>class|enum|extension|mixin)\s+(?P<name>[A-Za-z_]\w*)")
_LUA_DEF = re.compile(r"^\s*(?:local\s+)?(?P<kw>function)\s+(?P<name>[\w.:]+)")
_PERL_DEF = re.compile(r"^\s*(?P<kw>sub|package)\s+(?P<name>[A-Za-z_][\w:]*)")
# R has no def keyword: a name assigned a function literal is the only reliable shape.
_R_DEF = re.compile(r"^(?P<name>[A-Za-z_.][\w.]*)\s*(?:<-|=)\s*function\b")
# "defstruct" is left out on purpose: it has no name, so it would take the first field's name.
_ELIXIR_DEF = re.compile(r"^\s*(?P<kw>defmodule|defmacrop?|defp?)\s+(?P<name>[A-Za-z_][\w.?!]*)")

# What each keyword means for a search hit. Anything not here keeps the keyword itself.
_KIND_WORDS = {"fn": "function", "func": "function", "def": "function", "defp": "function",
               "defmacro": "function", "defmacrop": "function", "defmodule": "module",
               "sub": "function", "package": "module"}
# ".cs" is not here: it has its own scanner, which needs a rule a plain regex cannot express.
_TABLE = {
    (".go",): _GO_DEF,
    (".rs",): _RUST_DEF,
    (".rb", ".rake"): _RUBY_DEF,
    (".php",): _PHP_DEF,
    (".sql",): _SQL_DEF,
    (".swift",): _SWIFT_DEF,
    (".scala", ".sc"): _SCALA_DEF,
    (".dart",): _DART_DEF,
    (".lua",): _LUA_DEF,
    (".ex", ".exs"): _ELIXIR_DEF,
    (".pl", ".pm"): _PERL_DEF,
    (".r",): _R_DEF,
}
BY_REGEX = {suffix: pattern for suffixes, pattern in _TABLE.items() for suffix in suffixes}


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
    elif suffix in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".mts", ".cts"):
        return _bound(_by_starts(lines, _js_starts(lines)))
    elif suffix == ".md":
        return _bound(_by_starts(lines, _heading_starts(lines)))
    elif suffix == ".cs":
        return _bound(_by_starts(lines, _cs_starts(lines)))
    elif suffix in BY_REGEX:
        return _bound(_by_starts(lines, _regex_starts(lines, BY_REGEX[suffix])))
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
        if len(line) > MAX_LINE:
            continue
        m = _KOTLIN_DEF.match(line)
        if m:
            kind = "function" if m.group(1) == "fun" else m.group(1)
            yield i, kind, m.group(2)


def _js_starts(lines):
    for i, line in enumerate(lines):
        if len(line) > MAX_LINE:    # a minified bundle is one enormous line
            continue
        m = _JS_DEF.match(line)
        if m:
            if m.group(1):
                yield i, "function", m.group(2)
            elif m.group(3):
                yield i, "class", m.group(4)
            else:
                yield i, "function", m.group(5)


def _cs_starts(lines):
    """C# needs one extra rule the shared scanner cannot express: a method is only a method
    if an access modifier is in front of it. Without that, "return Compute(x);" reads as a
    definition of Compute, and every call inside a method would start a new chunk."""
    for i, line in enumerate(lines):
        if len(line) > MAX_LINE:
            continue
        m = _CS_DEF.match(line)
        if not m:
            continue
        if m.group("kw"):
            yield i, m.group("kw").lower(), m.group("name")
        elif m.group("mods").strip():
            yield i, "function", m.group("mname")


def _regex_starts(lines, pattern):
    """One table-driven language: group 'name' is the definition's name and 'kw' its keyword.
    Go needs two shapes in one regex ("func X" and "type X struct"), so it uses 'fname' with no
    keyword and 'tname' with the keyword in 'tkind' instead."""
    for i, line in enumerate(lines):
        if len(line) > MAX_LINE:    # a minified or generated line holds no definition
            continue
        m = pattern.match(line)
        if not m:
            continue
        groups = m.groupdict()
        name = groups.get("name") or groups.get("fname") or groups.get("tname")
        if not name:
            continue
        keyword = (groups.get("kw") or groups.get("tkind") or "").lower()
        if groups.get("fname"):
            keyword = "function"
        yield i, _KIND_WORDS.get(keyword, keyword or "function"), name.strip("\"`[]")


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
