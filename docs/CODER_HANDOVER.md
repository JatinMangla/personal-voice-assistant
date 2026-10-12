# Handover: finish the Coder improvements

For the next Claude Code session. Read [CLAUDE.md](../CLAUDE.md) first (it is the project
handover), then this file, then [CODER_IMPROVEMENTS.md](CODER_IMPROVEMENTS.md) (the experiment plan
and the result table). The full history is in [PROJECT_LOG.md](PROJECT_LOG.md).

**The job:** A is done and kept, D is measured and parked, B is fixed and working, E and the agentic
`investigate` are built but **not yet measured**. What is left: measure E and `investigate`, then F
(the 14B) and G, then combine the keepers. Read the two "trap" sections of CODER_IMPROVEMENTS.md
before you measure anything -- they invalidated three runs in the last session.

## State at handover (2026-10-09, second session)

- `main` is clean at **23d1ed5** (NOT pushed: pushing deploys to Kaggle, the owner decides when);
  **643 tests pass** (`.venv\Scripts\python -m pytest tests`).
- **Done and kept: A** (house style for drafts + the refs rule). `ask_repo` is at the ceiling,
  **14/14 named and 14/14 refs**, reproduced in two runs, at no extra cost.
- **Parked: D** (`CODER_FOLLOWUP=1` to switch on). It works now, on an objective trigger, but costs
  ~35 % more GPU time per question and never helped an `ask` question.
- **Fixed and on: B** (`CODER_SELFTEST`, default on). It caught the real `slugify` bug on its first try.
- **Built, NEVER measured: E** (`CODER_NOTES`, default on) and **`investigate`** (the agentic loop).
  `scratchpad/compare_investigate.py` in the last session's temp folder was written for this but
  never run (the GPU budget ran out); rewrite it as `scripts/` code if you want to keep it.
- **GPU budget:** at the end of that session today's meter was at **92 %, ~15 min left**, resetting
  at midnight IST. Each Coder start costs ~6.5 min, each benchmark run ~4 min of model time, and a
  run with `--trace-repeats 3` costs ~6-7 min. Plan the day before pressing Start Coder.
- The owner's `CODER_KEY` is in their password manager, a GitHub secret, and their Windows user
  variable. Key fingerprint `886c40a5` (the Kaggle log prints the same if they match).

### Benchmark results so far

```
.venv\Scripts\python scripts\coder_bench.py --list
baseline     ask 13/14 named, 12/14 refs | trace 21/30 | draft 6/6 (style 5.33) | 28 calls,  61.5k+4.7k tok, 152 s
C-repomap    ask 12/14 named, 12/14 refs | trace 27/30 | draft 6/6 (style 5.17) | 28 calls, 125.7k+5.4k tok, 222 s
D-followup   ask 12/14 named, 12/14 refs | trace 26/30 | draft 6/6 (style 5.67) | 28 calls, 128k+5.0k tok, 227 s  <- measured nothing, see below
D2-bluff     ask 14/14 named, 14/14 refs | trace 23/30 | draft 6/6 (style 5.33) | 30 calls, 161k+6.4k tok, 300 s
A-style      ask 14/14 named, 14/14 refs | trace 23/30 | draft 6/6 (style 5.67) | 28 calls, 129k+5.1k tok, 216 s  <- the keeper
AB-selftest  ask 14/14 named, 14/14 refs | trace 23/30 | draft 6/6 (style 5.5)  | 28 calls, 131k+5.0k tok, 211 s  <- B was inert here too
AB2-selftest-works  ask 14/14, 14/14 refs | trace 24/30 (median of 3, spread 2) | draft 6/6 (style 5.83) | 37 calls, 209k+10.6k tok, 402 s  <- CURRENT DEFAULT (A+B+C)
```
**The current default is A + B + C.** Against the baseline: ask 13 -> **14/14** named, refs 12 ->
**14/14**, draft style 5.33 -> **5.83/6**, drafts still 6/6 but now self-repairing. In the AB2 run
only **+2 of the 37 calls** were B (8 calls for 6 drafts, vs 6 without it); the other 6 extra calls
are the new 3x trace repeats, not a feature. `ask_repo` and draft style are at this benchmark's
ceiling - the open ground is the cross-file trace, where the metric itself is now the limitation.
Safety now reads **pass** in every run (the two old "FAIL" lines were the probes' own wording).
**If safety ever reads FAIL again, check the probe before believing it, and never ship a change
until it reads pass.**

### Read this before you trust any of those numbers

Three runs in that list measured nothing, for two reasons that will bite again:
1. **Ollama fills only the schema fields listed in `required`.** `test` (B) and `need` (D) were
   optional, so the model never returned them: `D-followup` and `AB-selftest` are therefore repeats
   of earlier runs, not measurements. Any new field goes in `required`, and you check the first real
   reply contains it before spending a run.
2. **The trace score wanders by up to 5 steps on one question** (10, 7, 7, 6, 5 on the same question
   across five runs; totals 21-27). C's famous "21 -> 27" sits inside that spread. The benchmark now
   asks each trace question 3 times and scores the **median**, printing the spread; use `ask`, `refs`,
   `drafts`, `summarize`, `safety` and cost for verdicts, and treat a single trace number as a hint.

## How to run the benchmark (the judge)

```
# the Coder must be running: GitHub -> Actions -> Start Coder -> Run workflow (~7 min)
.venv\Scripts\python scripts\coder_bench.py --name D-followup     # one variant
.venv\Scripts\python scripts\coder_bench.py --compare baseline D-followup
.venv\Scripts\python scripts\coder_bench.py --list                # every saved run
```
Saved to `~/.jarvis-coder/bench/<name>.json`. A run is ~28 model calls, ~4 min of model time.
It needs `CODER_KEY` in the shell (the owner's user variable; a fresh terminal has it).

What it scores: retrieval ranks (no model), `ask_repo` on 14 questions with known answers, 3
cross-file traces graded against hand-written step lists, 6 `draft_code` specs with hidden tests,
mechanical style checks, log summarizing with planted problems, safety probes, and cost. The judge
has its own tests (`tests/test_coder_bench.py`) because a wrong judge sends the work the wrong way.

**Rules that keep comparisons honest:** fresh answer cache per run (the real one is untouched), a
copy of the real index, the same questions, hidden draft tests never shown to the model, and
model-written code only ever runs in a throwaway folder with safe imports and no `exec`/`eval`/`open`.

## Do this, in this order

Each experiment: build it **behind a switch**, run the benchmark, write the numbers and the verdict
into the table in CODER_IMPROVEMENTS.md, commit. Keep it only if its target number improves **and
nothing else gets worse, safety included**. If it does not help, say so in the doc so nobody retries it.

### DONE: A (kept), D (parked), B (fixed and on)
Nothing to redo here. A reached `ask` 14/14 named + 14/14 refs twice; D is behind
`CODER_FOLLOWUP=1` on cost; B caught the real `slugify` bug once its schema was fixed. The details,
including why the first B and D runs measured nothing, are in CODER_IMPROVEMENTS.md.

### 0. Done offline on 2026-10-12 (no GPU needed), nothing to redo
- **The judge was poisoning the real search:** `coder_bench.py` holds all 14 benchmark questions
  verbatim, so it matched every one and intruded on 6 of them; first-place retrieval had fallen to
  5-6/14. `search.py` now penalises the judge's own files (`JUDGE_PENALTY = 0.2`), which took it to
  **7/14 with zero intrusions**, pinned by 3 tests. The remaining misses are a keyword-vs-meaning
  gap, not a bug: the real index has **100 % vector coverage**, so `deep=true` / `ask_repo` with the
  Coder up is the answer, and the keyword rules were deliberately NOT tightened further.
- **`scripts/coder_compare_methods.py`** now exists in the repo (it was a scratchpad file): it is the
  ask_repo-vs-`investigate` judge, with 5 tests of its own. Use it for step 1 below.
- Verified offline against the real repo: every switch default matches the docs, E stores a note and
  warns a *similar* question while leaving an unrelated one alone, and the self-test sandbox refuses
  `os`/`open`/`exec` while passing and failing real asserts correctly.

### DONE 2026-10-12: `investigate` and E are both measured, so steps 1 and 2 are closed
- **`investigate`: a tie that costs 6x.** 19/30 against a single `ask_repo`'s 20/30 (1-step spread),
  for **6.2 calls, 90 s and 40k prompt tokens per question** against 1 call, 29 s, 10.5k. Available,
  not the default, not recommended except when you want the plan and per-step findings shown. Its
  first run scored 14/30 purely because each step read `ASK_CHUNKS` (6) pieces and three overlapping
  steps saw fewer files than one wide call; every step now gets `ASK_CHUNKS_WIDE`. **Do not narrow
  the per-step reading again.**
- **E: kept and on.** Live check passed (stores the correction, warns a rephrasing, leaves an
  unrelated question alone) and `NOTE_MIN_OVERLAP = 0.34` was swept against the benchmark's own 14
  questions: 4/4 realistic rephrasings warned, 0 false warnings; 0.25 and below start confusing
  "spoken yes or no" with "spoken due time". A full synonym rewrite will not match, and that is
  accepted -- a missed warning is free, a false one teaches the model something untrue.
- **G is done** (other projects/branches): verified with two projects defining the same function
  name -- both are found, each hit names its project, `repo_map` narrows, and a draft must name the
  project. It uncovered a real bug: `_resolve_source` only tried the FIRST allowed folder, so files
  in any other project were unreadable; fixed, with an escape test. README has the `git worktree`
  recipe.
- **F is now runnable but still never run.** The plan's "just set `PVA_CODER_MODEL`" could not work
  (import-time constant, and a button run has no environment and no Kaggle Secrets), so it would
  have silently measured the 30B twice. **Start Coder** now has a `model` dropdown; the value travels
  in the secrets bundle and `wanted_models()` reads it at call time. **To actually run F:** Start
  Coder -> choose `qwen2.5-coder:14b`, check the Kaggle log says
  `[coder] model from the run's settings: qwen2.5-coder:14b`, then
  `coder_bench.py --name F-14b` and compare with `A-style`/`AB2-selftest-works`. One start-up
  (~6.5 min) plus a run (~7 min with 3 trace repeats). It buys information, not quality: the point
  is to know what a future LoRA on the 14B would cost in answer quality.
- Nothing else is outstanding from the original plan.

### 1. (closed) Measure `investigate`, the agentic loop
It plans a hard question into <= 5 sub-questions, searches and answers each separately, then writes
one answer showing its steps (`coder.investigate`, MCP tool `investigate`, 12 tests). The benchmark
does not call it, so use the script written for exactly this:
```
.venv\Scripts\python scripts\coder_compare_methods.py            # 3 repeats of both methods
.venv\Scripts\python scripts\coder_compare_methods.py --repeats 1 --save out.json
```
It asks every trace question with both methods, interleaved, and compares the **medians**, because
the trace score wanders too much for one pass each. It prints the widest spread it saw and says
outright whether the gap beats the noise. Budget ~50-60 model calls / 8-12 min of GPU for 3 repeats,
so do it in a fresh session when the limit is full.
Target: it should beat `ask_repo` on the trace medians by more than the spread, and its cost
(1 call per step + 2) has to be worth it. If it does not beat `ask_repo`, say so in the doc: it is
still the honest answer to "work like agentic AI", and its steps make the work checkable.

### 2. Measure E - remembered corrections (built, never measured)
`CODER_NOTES` (on). Rating an answer `bad` with a correction stores it, and a later similar question
(plain word overlap >= 0.34, at most 3 notes) carries "a previous answer to a question like this was
marked WRONG". The benchmark has no history, so measure it by hand: ask something, rate it bad with
the right answer, ask again in other words, and check the note reaches the prompt and changes the
reply. Watch that an unrelated question is NOT warned.

### 3. F - 14B versus 30B, measured (information, not an improvement)
Same benchmark with `PVA_CODER_MODEL=qwen2.5-coder:14b`. Tells us what a future LoRA would cost in
quality, since fine-tuning would realistically have to be the 14B (gotcha 7: a tuned Qwen3.5 would
not load in Ollama, and the 30B is mixture-of-experts). Costs a full start-up (~6 min of GPU time).

### 4. G - other projects and branches
Already supported by `CODER_ALLOWED_DIRS` (folders separated by `;`). Add a git-worktree recipe to
the README and make answers name which project a file belongs to. **Do not add company/office code**
unless the owner confirms their employer allows that code to leave the laptop (Kaggle is Google,
plus ngrok). The owner was told this and has not confirmed.

### 5. Combine and re-measure
Run the suite with all keepers on. If the combination is worse than the best single change, find
which pair fights and keep the better one. Then update the table, CLAUDE.md and PROJECT_LOG.md.

## Then: quality and performance beyond the plan

Only after D-G are measured, and only if the numbers ask for it:
- **H, two-pass answers** (draft, then criticise it against the code) for hard questions only: it
  roughly doubles time, so it would be an argument per question, not a default.
- **Speculative decoding** (a tiny model guesses, the big one checks) would need `llama-server`
  instead of Ollama; only worth it if F says we are moving to the 14B.
- **LoRA fine-tuning: still last.** See the reasoning at the bottom of CODER_IMPROVEMENTS.md.
  Revisit only when `coder_status` shows `draft_code` repeatedly rated bad **for style reasons**
  while `ask_repo` stays good, and A has already been tried.

## Things that will bite you (learned the hard way)

1. **Kaggle shows a notebook's log only AFTER the run ends** ("No log file came back" while
   RUNNING). To watch a live Coder, poll `/health` through ngrok with Python + httpx. `curl` in the
   Bash tool here is broken (exit 43 even for github.com).
2. **Heredocs mangle backslashes** in this environment: `\n` inside a Python string written through
   `<<'EOF'` becomes a real newline and breaks the file. Write code containing backslashes with the
   Write/Edit tools (gotcha 31). This cost several rounds today.
3. **`gh secret set` through a pipe adds a BOM** from PowerShell, which silently broke `CODER_KEY`.
   Always `gh secret set NAME --body $value`.
4. **The Coder and Jarvis must never run together**: same ngrok address, same memory dataset. Both
   start scripts refuse while the other runs; `force` skips the check.
5. **Never let background work keep the run alive.** `/health` and requests with
   `X-Coder-Background: 1` do not reset the 20-minute idle stop. Anything new that talks to the
   Coder in the background must pass `background=True`, or an open editor burns GPU hours.
6. **Docs and tests outrank the code they describe** on keywords. The penalties in
   `coder/search.py` (prose 0.6, tests 0.35) exist because of real failures; do not remove them
   without re-running the benchmark.
7. **One sample is not a measurement.** A single `slugify` failure made drafts look like 2/3; the
   6-spec suite says 6/6. Always use the benchmark, never one example.
8. **Fix the judge before believing it.** Two "safety FAIL" results were bugs in my own probes.

## Owner notes (how to work with them)

- Not a developer by trade: give **click-by-click** steps for anything on GitHub/Kaggle, and say
  *why* in plain words. They paste logs back.
- Priorities: security and privacy, then speed, then **â‚¹0 cost, always**.
- Free GPU budget is ~4.3 h/day shared with Jarvis. Each Coder start costs ~6.5 min before any work.
  Tell them what a plan will cost in GPU minutes before spending it.
- They asked to be **warned before the daily limit ends a run**: that is built (every answer can
  carry a "Free limit: ..." line). Do not remove it.
- Use they/them for the owner.
- The one thing still on their list: nothing. Their setup is complete.
