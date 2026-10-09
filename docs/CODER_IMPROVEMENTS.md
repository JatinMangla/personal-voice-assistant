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

**What happened (2026-10-09):** the first attempt measured nothing, because `test` was an optional
schema field and so was never returned (see the `required` trap above). Once fixed, the very first
draft proved the point: the model's own asserts **failed its first `slugify`**, the error went back,
and its note on the second try reads *"the issue with the previous implementation was ... `replace`
`('-+', '-')` which doesn't work as expected for multiple consecutive hyphens"*. That is exactly the
`slugify` behaviour bug from the original baseline, caught and fixed without anyone looking. The
sandbox: an empty temp folder with only the draft, an import allowlist, no `open`/`exec`/`eval`, no
network, 25 s limit, and `-S -E -s` (not `-I`, which would stop the draft being importable at all).
Hostile tests are refused, not run, and a refusal is not counted as a failed draft.

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

**What the first attempt taught us (2026-10-09), before any score:** invited politely to put the
files it still needs in `need`, the model **never once asked** — 0 times in 17 questions, and the
model-call count was identical to the C run, so the first `D-followup` run measured nothing about D
(its traces 26/30 vs C's 27/30 is noise; see the variance note below). Probing it three ways showed
why, and that the mechanism itself is fine:

| What the model was shown | What it did |
|---|---|
| the usual 14 pieces + repo map | `need: none`, **confidence high**, and cited `scripts/start_coder.py` and `coder_proxy.py`, *neither of which it had been shown* |
| ONE deliberately unrelated piece | `need: [4 real paths]`, empty answer, confidence low -- correct |
| one unrelated piece, no repo map | `need: [4 INVENTED paths]` (`scripts/coder/main.py`, `server.py`, `auth.py` do not exist) |

So the model asks only when it cannot bluff at all; with a repo map and a few pieces it feels
well-informed and answers from the map's one-liners. Firmer wording did not change this. The fix was
to stop relying on it asking: **a citation of a file it was never shown is objective evidence that it
needed that file**, so `service._cited_but_unseen()` fetches those (through the normal safety checks,
which is what discards the invented paths) and asks once more. Hand-checked on the 3 trace questions:
the trigger fired on 2 of 3 and fetched exactly the right files (`start_coder.py`; then `stt.py`,
`intents.py`, `memory.py`, `llm.py`), trace 1 reaching **10/10** -- but the total was again 27/30 at
~1.7x the calls, because the write-up round loses steps elsewhere.

**Verdict after the full run (`D2-bluff`): D is OFF by default (`CODER_FOLLOWUP=1` turns it on).**
The honest reason is **cost, not quality**: the suite took 30 calls / 161k prompt tokens / 300 s
against 28 / 126k / 216-222 s without it, i.e. ~35 % more GPU time for every question, and on the
free budget a feature has to earn that. Its trace score was lower (23/30 vs 27/30) but the section
above shows the trace metric wanders by up to 6 steps on its own, so **that part is not evidence**;
do not repeat it as if it were. What is solid: it fetches the right files (hand-checked on all three
traces, trace 1 reaching 10/10 that way), and it never once helped an `ask` question, which is where
the everyday cost would fall. Keep it as a switch for a single narrow question, and use
**`investigate`** for cross-file ones: that splits the question *before* answering instead of
patching an answer afterwards.

The run also proved something useful by accident: **the ask jump to 14/14 named and 14/14 refs came
from A's refs rule, not from D** (the follow-up fired on no `ask` question at all; the refs sentence
lives in `tasks.SYSTEM`, so it was active even with `CODER_STYLE=0`). Both questions C got wrong --
the ngrok policy and `shrink` -- are right now.
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

## The trap that made two experiments look dead: Ollama only fills `required` fields

**Measured 2026-10-09, and it invalidated two runs before anyone noticed.** With structured output
(`format: <schema>`), the model fills **exactly the fields listed in the schema's `required` array
and nothing else**. A field that is only in `properties` is returned *never* -- no matter how firmly
the prompt asks for it.

That silently crippled both experiments that add a field:
- **B (self-tests):** `test` was optional, so no draft ever came back with one. The first `AB-selftest`
  run showed "28 calls, drafts 6/6, costs nothing" -- it had simply never run. Moving `test` into
  `required` produced a proper 8-assert test on the very first try.
- **D (follow-up):** `need` was optional too, so part of "the model never asks" was the schema, not
  the model. With `need` required it now returns `[]` -- *deliberately empty*, which is what finally
  proved the refusal is a real choice and not a plumbing bug.

How to spot it: the field comes back as `None`/absent rather than empty. An empty list or string
means the model considered it and declined; absent means it was never asked. The schemas are now
`DRAFT_SCHEMA_WITH_TEST` and `ASK_SCHEMA_WITH_NEED`, used only when the feature is on (so a normal
call is not forced to invent a value), and a test pins all four `required` lists.

**Rule for the next experiment that adds a field: put it in `required`, then check the first real
reply actually contains it before you spend a benchmark run.**

## STOP: the trace score is too noisy to decide anything (measured over 5 runs, 2026-10-09)

Five runs now exist. Lining up the per-question trace scores shows the metric wandering on its own:

| run | trace 1 | trace 2 | trace 3 | total | what was different |
|---|---|---|---|---|---|
| baseline | 7 | 6 | 8 | 21 | - |
| C repo map | **10** | 9 | 8 | **27** | repo map on |
| D follow-up | 7 | 9 | 10 | 26 | switch on, but it never fired = a repeat of C |
| D2 bluff trigger | 6 | 9 | 8 | 23 | follow-up fired on 2 traces |
| A style+refs | **5** | 9 | 9 | 23 | follow-up off, style on |

**Trace 1 scored 10, 7, 7, 6 and 5 on the identical question (spread 5 steps, stdev 1.9). The
totals span 21-27, a spread of 6.** Traces 2 and 3 are steadier (spread 3 and 2).

Consequences, and they are uncomfortable:
- **C's headline "21 -> 27" sits inside the noise** and should not be treated as a measured 6-step
  win any more. The repo map may still be right for other reasons (it demonstrably stopped invented
  file names), but *the trace number did not prove it*.
- **The D verdict below rests on the same shaky metric.** D is still switched off, because its
  **cost** is unambiguous (30 calls / 161k tokens / 300 s against 28 / 126k / 216-222 s) and a
  feature has to earn its cost; but "traces 27 -> 23" on its own is not evidence that it harms
  quality.
- **Never again decide a trace experiment from one run.** Three runs of each variant, and compare
  the medians, or use the stable metrics instead.

**What IS stable across all five runs:** `drafts` 6/6 every time, and `ask` named/refs move only
when something real changes -- refs went 12/14 -> **14/14 in exactly the two runs carrying A's refs
rule**, and reproduced. Prefer `ask`, `refs`, `drafts`, `summarize`, `safety` and cost for verdicts;
treat traces as a weak hint that needs repeating.

A cheap fix for a later session: the benchmark could run each trace question 3 times and keep the
median, at the price of 6 more model calls (~1 extra minute of GPU).

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
| **D follow-up, invited** (2026-10-09) | 12/14 named, 12/14 refs | 26/30 | 6/6 | 5.67/6 | 3/3 | pass | 28 calls, 128k+5.0k tok, 227 s | **measured nothing: the model never asked once.** Doubles as the noise measurement above |
| **D2 follow-up, triggered by an unseen citation** (2026-10-09) | **14/14 named, 14/14 refs** (that is A's refs rule, not D: follow-up did not fire on any ask question) | **23/30** | 6/6 | 5.33/6 | 3/3 | pass | 30 calls, **161k**+6.4k tok, **300 s** | **OFF by default.** Traces 27 -> 23 (trace 1 fell 10/10 -> 6/10) for 35 % more model time |
| **A style + refs** (2026-10-09) | **14/14 named, 14/14 refs** (reproduced) | 23/30 (noise) | 6/6 | **5.67/6** | 3/3 | pass | 28 calls, 129k+5.1k tok, **216 s** | **KEPT, on by default** |
| B self-test | | | | | | | | |
| E notes | | | | | | | | |
| F 14B | | | | | | | | |
| I investigate (agentic loop) | | | | | | | | not on the benchmark: it answers trace questions, see below |
| combined keepers | | | | | | | | |

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

## I. The agentic loop (`investigate`), and why it is NOT parallel

The owner asked for "multi subagents, working like agentic AI, for faster work". Half of that is
built, and the other half is measured away:

**Measured 2026-10-09 against the live Coder** (`scratchpad/probe_parallel*.py`, Run 2 of the day):

| What was sent | One request alone | Three at once | Speed-up |
|---|---|---|---|
| Tiny prompt ("count to 40", ~30 tokens) | 5.0 s | 6.4 s total | **2.4x** |
| Realistic prompt (16k tokens, like `ask_repo`) | 20.9 s | 62.2 s total | **1.01x = none** |

The small prompts only *looked* parallel because prefilling them is nearly free; with a real
16k-token prompt the three finished at 22 s, 42 s and 62 s, i.e. strictly one after another.
`coder_main.py` sets `OLLAMA_NUM_PARALLEL=1` on purpose (one user, the whole 32k context for one
request), and the two T4s are the bottleneck either way. **So "several subagents at once" cannot make
this Coder faster** — the requests queue, and raising the parallelism would shrink each request's
context and risk running out of GPU memory. (Trap 7 again: the first probe was one cheap sample and
it pointed the wrong way. Always use a realistic prompt.)

What *is* worth having is the agentic part: **`investigate`** plans a hard question into at most
`tasks.MAX_STEPS` (5) sub-questions, **searches and answers each one separately**, then writes the
findings up as one answer. The gain is retrieval, not concurrency: each step runs its own search, so
it sees code that a single search for the whole question never ranks — the same weakness D attacks,
from the other side. It costs one model call per step plus two, so it is a tool to reach for on
cross-file questions, never the default; `ask_repo` stays the everyday one.

It would become faster for free if the Coder ever moves to the 14B (experiment F) or is given more
parallelism, because the steps are independent by construction. Until then, sequential.

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
