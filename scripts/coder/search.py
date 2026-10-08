"""Search the code index: keywords (FTS5/BM25 over code, split identifiers, names and paths)
and symbol names, merged by Reciprocal Rank Fusion, the same idea as src/memory.py's recall.
Vector search and a reranker join later (they need the embedding model)."""
import re

from .chunk import split_identifiers

CANDIDATES = 30
STOP = {"the", "a", "an", "is", "are", "of", "to", "in", "on", "for", "and", "or", "how", "what",
        "where", "which", "does", "do", "it", "this", "that", "with", "from", "by", "be", "i", "we",
        "me", "my", "can", "code", "function", "file"}


def rrf(*rankings: list, k: int = 60) -> dict:
    """Reciprocal Rank Fusion: {item: score}. Same as memory.rrf, repeated here so these
    scripts don't import the assistant's modules."""
    scores: dict = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return scores


def query_terms(query: str) -> list[str]:
    """Lower-case words of the question, identifiers split ('startBlocker' -> start, blocker),
    without filler words. Order kept, duplicates dropped."""
    words = re.findall(r"[A-Za-z0-9]+", split_identifiers(query) + " " + query.lower())
    out: list[str] = []
    for w in words:
        w = w.lower()
        if len(w) >= 2 and w not in STOP and w not in out:
            out.append(w)
    return out


def _fts(term: str) -> str:
    return f'"{term}"*'


def by_keywords(index, terms: list[str], limit: int = CANDIDATES) -> list[int]:
    if not terms:
        return []
    match = " OR ".join(_fts(t) for t in terms)
    with index.lock:
        rows = index.conn.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
            "ORDER BY bm25(chunks_fts, 1.0, 1.5, 4.0, 2.0) LIMIT ?", (match, limit)).fetchall()
    return [r[0] for r in rows]


def by_symbol(index, terms: list[str], limit: int = CANDIDATES) -> list[int]:
    """Chunks whose own name contains the question's words; more words matched = better."""
    if not terms:
        return []
    clauses = " OR ".join("lower(name) LIKE ?" for _ in terms)
    with index.lock:
        rows = index.conn.execute(
            f"SELECT id, name FROM chunks WHERE name != '' AND kind != 'module' AND ({clauses})",
            [f"%{t}%" for t in terms]).fetchall()
    scored = []
    for r in rows:
        name = r["name"].lower()
        hits = sum(1 for t in terms if t in name)
        scored.append((-hits, len(name), r["id"]))
    scored.sort()
    return [s[2] for s in scored[:limit]]


TEST_PENALTY = 0.7   # tests mention everything; real code should come first unless asked for
TEST_PATH = re.compile(r"(^|/)(tests?|androidTest)/|(^|/)test_[^/]*$|Tests?\.kt$|_test\.py$")
# Docs describe the code in the question's own words, so they beat the code on keywords while
# answering "where is it" worse. First observed 2026-10-09: "which limit forbids starting now"
# put README.md and a doc above budget.start_blocker.
PROSE_PENALTY = 0.6
PROSE_PATH = re.compile(r"\.(md|txt|rst)$", re.I)
PROSE_WORDS = ("doc", "docs", "readme", "plan", "log", "guide", "notes")
NAME_BONUS = 1.6     # the chunk's own name holds every word of the question: this IS the answer
MIN_COVERAGE_WEIGHT = 0.5   # a chunk holding none of the words keeps half its score


def _rescore(index, ranked: dict, terms: list[str]) -> tuple[dict, dict]:
    """Scale each fused score by how many of the question's words the chunk really holds (so a
    hit on one common word loses to a chunk with all of them), lift a chunk whose own NAME holds
    them, and push tests and prose down unless the question asks for them. Returns (scores, rows)."""
    if not ranked:
        return {}, {}
    marks = ",".join("?" for _ in ranked)
    with index.lock:
        rows = {r["id"]: r for r in index.conn.execute(
            f"SELECT id, path, first, last, kind, name, text FROM chunks WHERE id IN ({marks})", list(ranked))}
    wants_tests = any("test" in t for t in terms)
    wants_prose = any(t in PROSE_WORDS for t in terms)
    scores = {}
    for cid, row in rows.items():
        shown = index.display(row["path"])
        haystack = " ".join((row["text"], split_identifiers(row["text"]), row["name"], shown,
                             split_identifiers(shown))).lower()
        coverage = sum(1 for t in terms if t in haystack) / len(terms) if terms else 1.0
        weight = MIN_COVERAGE_WEIGHT + (1 - MIN_COVERAGE_WEIGHT) * coverage
        name = f"{row['name']} {split_identifiers(row['name'])}".lower()
        if terms and row["name"] and all(t in name for t in terms):
            weight *= NAME_BONUS
        if not wants_tests and TEST_PATH.search(shown):
            weight *= TEST_PENALTY
        if not wants_prose and PROSE_PATH.search(shown):
            weight *= PROSE_PENALTY
        scores[cid] = ranked[cid] * weight
    return scores, rows


def by_vectors(index, qvec, limit: int = CANDIDATES) -> list[int]:
    """Chunks whose meaning is closest to the question (cosine), from the Coder's embeddings."""
    import numpy as np
    built = index.vector_matrix()
    if built is None:
        return []
    ids, matrix = built
    q = np.asarray(qvec, dtype=np.float32)
    if q.shape[0] != matrix.shape[1] or not np.linalg.norm(q):
        return []
    sims = matrix @ (q / np.linalg.norm(q))
    best = np.argsort(-sims)[:limit]
    return [int(ids[i]) for i in best if sims[i] > 0.2]


def search(index, query: str, limit: int = 8, qvec=None, rerank=None, pool: int = CANDIDATES) -> list[dict]:
    """qvec: the question's embedding (adds meaning-search); rerank: callable(query, [docs]) ->
    scores or None, applied to the best `pool` candidates. Both are optional: without them this
    is the fast offline search."""
    terms = query_terms(query)
    rankings = [by_keywords(index, terms), by_symbol(index, terms)]
    if qvec is not None:
        rankings.append(by_vectors(index, qvec))
    scores, rows = _rescore(index, rrf(*rankings), terms)
    order = sorted(scores, key=scores.get, reverse=True)
    reranked: dict[int, float] = {}
    if rerank and len(order) > 1:
        candidates = order[:pool]
        docs = [f"{index.display(rows[c]['path'])}:{rows[c]['first']}-{rows[c]['last']} "
                f"{rows[c]['kind']} {rows[c]['name']}\n{rows[c]['text']}" for c in candidates]
        try:
            got = rerank(query, docs)
        except Exception:    # a better order is a bonus; the search itself must not fail
            got = None
        if got and len(got) == len(candidates):
            reranked = dict(zip(candidates, got))
            order = sorted(candidates, key=lambda c: -reranked[c])      # stable: ties keep RRF order
    top = order[:limit]
    return [{"path": index.display(rows[c]["path"]), "file": rows[c]["path"],
             "first": rows[c]["first"], "last": rows[c]["last"],
             "kind": rows[c]["kind"], "name": rows[c]["name"], "text": rows[c]["text"],
             "score": round(scores[c], 4), **({"rerank": round(reranked[c], 3)} if reranked else {})}
            for c in top]
