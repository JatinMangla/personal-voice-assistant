# Coder improvements: measure one at a time, keep what wins

Plan for making the Kaggle Coder helper better **without** fine-tuning it. Every idea here targets
a weakness that a real measurement showed, not a guess. Each is built alone, measured against the
same benchmark, and kept only if the numbers improve. At the end the winners are combined and
measured together, because two changes that each help can still fight each other.

Why not LoRA first: see [the decision](#why-not-lora-yet) at the bottom. Short version: every
failure we have measured so far was in the surrounding system (search ranking, output limits, how
much code the model is shown), not in the model's weights.

## Where we start (measured 2026-10-09, `qwen3-coder:30b` on Kaggle T4 x2)

| Capability | Score | Note |
|---|---|---|
| `ask_repo` names the right function | **14/14** | after the ranking fixes; at ceiling |
| `summarize` finds planted problems | **3/3** | 240,000 chars -> 439 chars |
| `draft_code` passes my hidden tests first try | **2/3** | `slugify` broke on leading punctuation |
| Complex trace (10 steps across ~10 files) | **8/10** | missed the per-request key check |
| Long "list every problem" summary | works | 15 bullets, 1-2 garbled, list capped |
| Invents code that does not exist | **no** | says so honestly |
| Follows instructions hidden in a file | **no** | ignored an injected "answer HACKED" |
| Reading / writing speed | 1060 / 59 tok/s | `ask_repo` 5.8 s median |
| House style of drafts | **not measured** | generic docstrings, verbose, unlike this repo |

So: finding and explaining code is solved. What is left is **writing code correctly and in this
repo's style**, and **cross-file questions where the needed file was never shown**.

## The benchmark (the judge)

`scripts/coder_bench.py` (to be written in step 0) runs the same suite every time and writes a
JSON file per variant, so two variants can be compared line by line:

1. **Retrieval** (14 questions, no model): rank of the right file, plain vs deep.
2. **ask_repo** (14 questions): names the right function, cites a file it was really shown,
   confidence, seconds, answer size.
3. **Trace** (3 cross-file questions, graded against a hand-written list of steps).
4. **draft_code** (6 specs with hidden tests I wrote): does the draft pass, how many model calls,
   seconds. Three are the original specs, three are new and harder (edge cases on purpose).
5. **Style** (on the same drafts, mechanical checks only): comment density close to this repo's,
   docstring style, no emoji, line length, no restating the obvious.
6. **Summarize**: planted-problem recall, compression, cut-off flag.
7. **Safety** (unchanged, must never regress): prompt injection, question about code that does not
   exist, secret files never read, path escapes refused.
8. **Cost**: model calls, prompt tokens, reply tokens, wall seconds per task (GPU minutes matter).

Rules so the comparison is fair:
- fresh answer cache per run (the real cache is never touched), same index copy, same questions;
- the hidden `draft_code` tests are never shown to the model;
- each variant runs the full suite in one Coder session (no restart in between);
- a variant is kept only if it does not make any other number worse, safety included.

## The experiments, in order

Each one: build behind a switch, run the benchmark, record the numbers in the table at the bottom.

### A. House style and conventions in the prompt
**Fixes:** style was never specified, so drafts look nothing like this repo.
**Build:** a short style block in `tasks.SYSTEM` for drafts (comments say *why* not *what*, plain
English, no emoji, match the surrounding code, no docstring that restates the signature) plus 2-3
real files from this repo as examples. Switch: `CODER_STYLE=0` turns it off.
**Expect:** style checks up; correctness unchanged; a few hundred more prompt tokens.

### B. The draft writes and runs its own tests
**Fixes:** `draft_code` 2/3 — the `slugify` bug was behaviour, which syntax and lint cannot catch.
**Build:** `draft_code` asks for the code **and** its own tests, runs those tests in a temporary
copy (never your files, never the project's own test suite, which stays behind `CODER_RUN_TESTS`),
and on failure sends the error back for up to 2 fixes. Switch: `CODER_SELFTEST=0`.
**Expect:** the biggest single gain on correctness; 2-3x the model calls per draft, so slower.

### C. The repo map in every prompt
**Fixes:** the invented `kaggle.api.competitions.submissions_create()` — the model could not see
which files exist, so it guessed.
**Build:** put the one-line-per-file map (already built while idle) at the top of the fixed prompt
prefix, so Ollama caches it and it costs almost nothing after the first call. Trim to the folders
the question touches if it is too long. Switch: `CODER_REPO_MAP=0`.
**Expect:** fewer invented names on cross-file questions; prompt tokens up, prefill time flat
(cached prefix).

**C: kept.** Traces **21/30 -> 27/30**, the biggest single gain measured so far: question 1 went
7/10 -> **10/10** (it had been missing the secrets upload, `kernels push` and the notebook, all
files search never showed) and question 2 6/10 -> 9/10. `ask` 13 -> 12 named is a wash, not a
regression: two questions got **better** (the idle timeout and `no_emoji`, which gained its refs)
and two got worse (`traffic_policy` and `shrink`, where the map's one-line description pulled the
answer towards a neighbouring file). Style and drafts unchanged, safety unchanged. The price is
real: prompt tokens 61.5k -> 126k and `ask` median 4.9 s -> 6.8 s, because the ~14k-char map is sent
with every question; Ollama caches the prefix, so the cost is mostly its one-off read per session.
Worth it for a 6-step gain. Next: see whether D (follow-up questions) closes the remaining 3 steps,
and whether naming the file in the map's line (C's two losses) can be sharpened in A.

Also fixed here: the benchmark's "no invention for missing code" probe only looked for "not" in the
first 200 characters, so it failed a perfectly honest answer that began "there is no explicit code
for...". It now accepts any honest denial and still catches an invented function name.

### D. The model may ask for more code
**Fixes:** the trace 8/10 — the file holding the answer was not among the pieces shown.
**Build:** allow one extra round: if the model answers `{"need": ["coder_proxy.py", ...]}` or its
confidence is low, fetch those files (same safety rules) and ask once more. Hard cap: one extra
round, so cost cannot run away. Switch: `CODER_FOLLOWUP=0`.
**Expect:** trace 8/10 -> 10/10; about 1.5-2x the time on the questions that use it, unchanged on
the rest.

### E. Remember corrections (learning without training)
**Fixes:** nothing yet, but a rated-bad answer currently teaches the system nothing.
**Build:** when an answer is rated bad, store the question and (optionally) my correction. A later
similar question shows that note in the prompt: "you got this wrong before: ...". Reuses the
existing ratings table and the embedding search. Switch: `CODER_NOTES=0`.
**Expect:** no change on the benchmark (it has no history); measured instead by replaying the two
real bad answers we already have.

### F. 14B versus 30B, measured
**Fixes:** we assumed the 30B is better and never checked.
**Build:** nothing: run the same benchmark with `PVA_CODER_MODEL=qwen2.5-coder:14b`.
**Expect:** 30B better on quality, 14B ~3x faster to write and quicker to load. Knowing the gap
decides whether a future LoRA (which would realistically have to be the 14B) is even worth it.

### G. Other projects and branches indexed
**Fixes:** the Coder only sees this repo.
**Build:** already supported by `CODER_ALLOWED_DIRS`; add a worktree recipe and make answers name
the project a file belongs to. **Do not add company code** unless the owner's employer allows that
code to leave the laptop (Kaggle is Google, plus ngrok).
**Expect:** no change on this benchmark; it widens what can be asked.

### H. Two-pass answers (only if A-E leave gaps)
Draft, then let the model criticise its own draft against the code before answering. Roughly
doubles time and cost, so it would be switched on per question, not by default.

## How a winner is decided

- **Keep** a variant if it improves its target number and makes nothing else worse.
- **Keep but switch off by default** if it improves quality at a cost (time, tokens) that is not
  worth it for every call: it becomes an argument the tool can set per question.
- **Drop** it if the numbers do not move. Write down that it did not work, so nobody tries it again.
- **Then combine** the keepers and run the suite once more. If the combination is worse than the
  best single change, find out which pair fights and keep the better one.

## Results (filled in as each one runs)

Measured with `scripts/coder_bench.py --name <variant>`; `--list` and `--compare a b` print them.

| Variant | ask_repo | trace | draft | style | summarize | safety | cost | verdict |
|---|---|---|---|---|---|---|---|---|
| **baseline** (2026-10-09) | 13/14 named, **12/14 refs** | **21/30** | **6/6** | **5.33/6** | 3/3 | pass | 28 calls, 61.5k+4.7k tok, 152 s | reference |
| **C repo map** (2026-10-09) | 12/14 named, 12/14 refs | **27/30** | 6/6 | 5.17/6 | 3/3 | pass | 28 calls, **126k**+5.4k tok, 222 s | **KEPT** |
| A style | | | | | | | | |
| B self-test | | | | | | | | |
| C repo map | | | | | | | | |
| D follow-up | | | | | | | | |
| E notes | | | | | | | | |
| F 14B | | | | | | | | |
| A+B+C+D combined | | | | | | | | |

## What the baseline run already told us (2026-10-09)

Run with the 6-spec draft suite and 3 traces, not the smaller first benchmark:
- **draft_code 6/6**, including the harder new specs (durations, merging ranges, masking digits).
  The earlier `slugify` failure did **not** come back: one sample is not a measurement, and the real
  first-try rate is better than 2/3 looked. So **experiment B (self-tests) has little room to help
  correctness**; it may still be worth it as insurance, measured by cost.
- **Style 5.33/6** already, and the misses are specific: obvious comments ("# Lowercase the text"),
  too many comments, and one `Args:/Returns:` docstring. That is exactly what **experiment A** targets.
- **Traces 21/30**, the weakest number by far. The missed steps are files that search never showed
  (the secrets upload, `kernels push`, the notebook, intents, memory recall, quick search). That is
  **experiments C and D**, and it is where the biggest gain is.
- **Refs 12/14**: twice the model gave a right answer with "Refs: none given" or cited the wrong
  file. Small prompt fix, worth folding into A.
- Safety: all real checks pass. The "FAIL" in the first run was my own probe wording, not a hole:
  `../../escaped.py` is refused by `selfcheck.target_path`, nothing is written outside the folder,
  and the model itself declined the escaping path. The probe now checks the outcome, not the words.

Order to run next, by expected gain: **C (repo map) -> D (follow-up) -> A (style + refs) -> B (self-tests)**.

## Why not LoRA yet

Fine-tuning teaches style and patterns, not facts, and anything trained in is frozen while your
code keeps changing (search always reads the current code, so it never goes stale). It is also the
most expensive option here: 2-4 GPU hours per attempt out of ~4.3 hours a day, repeated whenever
the code moves on. And gotcha 7 in CLAUDE.md records that a fine-tuned Qwen3.5 would not load in
Ollama; the 30B coder is a mixture-of-experts model, which is harder still, so training would most
likely force a move to the 14B — trading a **measured** 3x writing-speed loss for an **unmeasured**
style gain. Experiment A buys most of the same benefit for minutes and no GPU time.

Revisit LoRA when `coder_status` shows `draft_code` repeatedly rated bad **for style reasons**
while `ask_repo` stays good, and A-E have been tried. Then the ratings also tell us what to train on.
