# Memory retrieval research: vectorless RAG vs ripgrep vs what we use (2026-09-28)

**Question from the owner:** memory is small now but will become vast. How should it be stored
so the right data is easy to fetch? Which is better: "vectorless RAG" or ripgrep? What do we
use today?

## 1. What Jarvis uses today

| Part | How |
|---|---|
| Storage | One SQLite database, **in RAM only**, saved as one AES-256-GCM encrypted blob to a private Kaggle dataset (`memory.py`, `persistence.py`). |
| Tables | `facts` (text, category, source, date, embedding) and `turns` (every conversation: user text, reply, language, model, rating, embedding). |
| Retrieval, facts | **Structured, no search needed:** health, diet, assistant-corrections, preferences and patterns always go into the prompt. The rest are ranked by relevance, up to 50 facts. |
| Retrieval, conversations | **Dense vector search**: `intfloat/multilingual-e5-small` (384 numbers per text), brute-force cosine similarity in numpy over all rows, top 5 above 0.80. |
| Short-term | The last 6 turns as chat history. |
| Keyword search | **None.** |

So today it is **"vector RAG with structured facts"**: no vector database, and no keyword index.

## 2. The candidates

### ripgrep ("agentic grep search")
- A very fast exact-text search over **plain files**. Claude Code itself uses it for code instead of
  vectors: the LLM greps, reads, and greps again. ([Rust Trends](https://rust-trends.com/posts/ripgrep-claude-code/),
  [LlamaIndex](https://www.llamaindex.ai/blog/is-grep-all-you-need-lexical-vs-sematic-search-for-agents))
- **Great for:** exact names, numbers, IDs, code.
- **Wrong fit for Jarvis:**
  1. It needs **plain-text files on disk**. Our memory is deliberately never on disk unencrypted,
     so this would break the security design.
  2. It matches **exact words only**. "allergy" doesn't find "एलर्जी", "mujhe allergy hai", or
     "can't eat peanuts". Jarvis is bilingual (English / Hindi / Hinglish), so this matters a lot.
  3. "Agentic" grep means the LLM searches in several rounds, and each round costs 1–2 s on a
     T4. That's too slow for voice replies (target: first audio in 2–3 s).
  4. There's no ranking: it returns every match, or none.

### Vectorless RAG (e.g. PageIndex)
- Builds a **tree of sections** (like a book's table of contents) and lets the LLM *reason* its
  way down the tree instead of using vectors. It gets 98.7 % on FinanceBench.
  ([PageIndex](https://github.com/VectifyAI/PageIndex), [Microsoft blog](https://techcommunity.microsoft.com/blog/azuredevcommunityblog/vectorless-reasoning-based-rag-a-new-approach-to-retrieval-augmented-generation/4502238))
- **Great for:** long, structured documents (reports, manuals, contracts).
- **Wrong fit as the main method:** our memory is thousands of small conversation snippets with
  no natural sections. Tree navigation also needs several LLM calls per question, which again
  is too slow for voice.
- **Worth borrowing:** the *tree* idea, applied to **time**:
  - day → week → month summaries, so years of history stay navigable ("what did I work on in March?").

### Hybrid search inside SQLite (keyword + vector + structure)
- **FTS5** (built into SQLite) gives BM25 keyword ranking, the same strength as grep/ripgrep but
  **inside the encrypted in-RAM database**, and ranked.
- Our existing **vectors** give meaning and cross-language matches.
- **Reciprocal Rank Fusion (RRF)** merges both rankings. The two fail in different ways (keywords
  miss synonyms; vectors miss exact names and numbers), so together they are much more reliable.
  ([Simon Willison](https://simonwillison.net/2024/Oct/4/hybrid-full-text-search-and-vector-search-with-sqlite/),
  [vstash paper](https://arxiv.org/pdf/2604.15484), [sqlite-hybrid-search](https://github.com/liamca/sqlite-hybrid-search))
- **Structured filters** (category, date range, language, source) answer "last week", "health",
  "decisions" questions exactly, with no search at all.

## 3. Recommendation

**Neither ripgrep nor vectorless RAG on its own. Use a hybrid in the same encrypted SQLite:**

1. **Keep:** structured facts (categories, dates) and the multilingual vectors.
2. **Add:** an FTS5 keyword index on facts and turns, merged with vector results by RRF, plus a
   small recency boost.
3. **Add:** date and category understanding for questions like "last month" or "my decisions".
   These run as a structured query before any search.
4. **Later, when history is large** (about 20k turns, or the saved file > 50 MB):
   - **Summaries tree** (the good part of vectorless RAG): daily, weekly and monthly digests of
     old conversations. Search the digests first, then drill down.
   - **Split storage:** a "hot" database (facts + the last 90 days, uploaded at every autosave)
     plus encrypted **monthly archive files** that are written once and never re-uploaded. Today
     the *whole* database is re-uploaded every 10 minutes, and that, not the search, is what
     stops scaling first.
   - **Smaller vectors:** float16 embeddings halve their size.
   - **Faster vector search:** `sqlite-vec` only if brute force ever gets slow. Numpy over 100k
     rows is still only a few milliseconds.

## 4. Growth estimate

| Time | Turns (at ~40/day) | Embeddings (float32) | Brute-force search |
|---|---|---|---|
| 1 year | ~15,000 | ~23 MB | < 5 ms |
| 5 years | ~75,000 | ~115 MB (58 MB as float16) | ~10 ms |

Search speed is not the problem for years. **Retrieval quality** (keywords + meaning + dates) and
**upload size** (split storage) are the real issues, and they are what the plan fixes.

## 5. Status
- **Built (2026-09-28, night 2):** steps 1–3 (`memory.py`, `query_hints.py`) and the
  summaries tree from step 4 (`digest.py`). Details that differ from the sketch above:
  - The FTS5 index lives in **TEMP** tables: rebuilt at every load (0.08 s for 15k turns) and
    never serialized, so it adds nothing to the upload and can't keep forgotten words in it.
  - Tokenizer `unicode61 remove_diacritics 2 categories 'L* N* Co M*'`: without `M*`, Hindi
    vowel signs count as separators and "एलर्जी" is cut into pieces.
  - Long Latin words become prefix terms ("allergic" → `allerg*` also finds "allergy");
    words that occur in more than 10 % of rows are ignored as too common.
  - Measured: ~20 ms per recall over 15,000 synthetic turns (plus the e5 query embedding).
- **Still deferred:** split storage and float16 (step 4), until the save log reports 50 MB or
  20,000 turns.
