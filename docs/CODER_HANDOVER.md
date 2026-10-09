# Handover: finish the Coder improvements

For the next Claude Code session. Read [CLAUDE.md](../CLAUDE.md) first (it is the project
handover), then this file, then [CODER_IMPROVEMENTS.md](CODER_IMPROVEMENTS.md) (the experiment plan
and the result table). The full history is in [PROJECT_LOG.md](PROJECT_LOG.md).

**The job:** work through the remaining experiments (D, A, B, E, F, G) one at a time, measure each
one with the benchmark, keep only what the numbers justify, then combine the keepers and measure
again. Everything is already built and pushed; nothing is half-finished.

## State at handover (2026-10-09)

- `main` is clean at **582b810**, pushed; **587 tests pass** (`.venv\Scripts\python -m pytest tests`).
- The Coder **works on Kaggle**: ready in ~6.5 min, reading ~1060 tok/s, writing ~59 tok/s,
  `ask_repo` 5-7 s. Model `qwen3-coder:30b` on both T4s, reranker on GPU 1.
- Experiment **C is done and kept** (repo map in the prompt): traces **21/30 -> 27/30**.
- Experiments **D, A, B, E, F, G are not started.**
- The owner's `CODER_KEY` is in their password manager, a GitHub secret, and their Windows user
  variable. Key fingerprint `886c40a5` (the Kaggle log prints the same if they match).

### Benchmark results so far

```
.venv\Scripts\python scripts\coder_bench.py --list
baseline    ask 13/14 named, 12/14 refs | trace 21/30 | draft 6/6 (style 5.33/6) | summarize 3/3 | 28 calls,  61.5k+4.7k tok, 152 s
C-repomap   ask 12/14 named, 12/14 refs | trace 27/30 | draft 6/6 (style 5.17/6) | summarize 3/3 | 28 calls, 125.7k+5.4k tok, 222 s
```
Both lines print "safety FAIL": that is **two false alarms from the probes themselves**, both since
fixed and verified by hand (nothing escapes the folder; an honest "there is no such code" answer was
being failed by a word match). The next run prints the corrected result. **If safety ever reads FAIL
again, check the probe before believing it, and never ship a change until it reads pass.**

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

### 1. D - let the model ask for a file (biggest remaining gain)
The 3 trace steps still missed are all "the answer is in a file search did not rank highly".
Allow **one** extra round: if the model replies `{"need": ["src/coder_proxy.py", ...]}` or its
confidence is low, fetch those files through the normal safety checks (`service._resolve_source`)
and ask once more. Hard cap of one extra round so cost cannot run away. Switch `CODER_FOLLOWUP=0`.
Target: traces 27/30 -> 30/30, `ask` not worse. Watch: time on the questions that use it.

### 2. A - style, and the two wrong refs
Style is already 5.17/6; the misses are specific and mechanical: obvious comments
("# Lowercase the text"), too many comments, one `Args:/Returns:` docstring. Add a short style block
for drafts (comments say *why* not *what*, plain English, no emoji, match the surrounding code, no
docstring that restates the signature) plus 2-3 real files from this repo as examples. Switch
`CODER_STYLE=0`.
**Fold in the refs fix:** two answers were right but gave "Refs: none given" or cited a neighbouring
file. Tell the prompt that every answer must cite a file it was actually shown, and that the repo
map is a list of what exists, not of what it has read (C's two losses were exactly this confusion).
Target: style >= 5.8/6, refs 14/14, traces unchanged.

### 3. B - drafts write and run their own tests
Lower priority than it looked: drafts score **6/6** including the harder specs, so there is little
room. Build it as insurance and judge it on cost: the draft returns code **and** its own tests, they
run in a temporary copy (never the owner's files; the project's own suite stays behind
`CODER_RUN_TESTS`), and on failure the error goes back for up to 2 fixes. Switch `CODER_SELFTEST=0`.
Keep **off by default** unless it is nearly free, since it multiplies model calls per draft.

### 4. E - remember corrections (learning without training)
A rated-bad answer currently teaches the system nothing. Store the question and (optionally) the
correction; show it as a note when a similar question comes back ("you got this wrong before: ...").
Reuse the ratings table and the embedding search. The benchmark has no history, so measure it by
replaying the two real bad answers in `~/.jarvis-coder/cache.db`. Switch `CODER_NOTES=0`.

### 5. F - 14B versus 30B, measured (information, not an improvement)
Same benchmark with `PVA_CODER_MODEL=qwen2.5-coder:14b`. Tells us what a future LoRA would cost in
quality, since fine-tuning would realistically have to be the 14B (gotcha 7: a tuned Qwen3.5 would
not load in Ollama, and the 30B is mixture-of-experts). Costs a full start-up (~6 min of GPU time).

### 6. G - other projects and branches
Already supported by `CODER_ALLOWED_DIRS` (folders separated by `;`). Add a git-worktree recipe to
the README and make answers name which project a file belongs to. **Do not add company/office code**
unless the owner confirms their employer allows that code to leave the laptop (Kaggle is Google,
plus ngrok). The owner was told this and has not confirmed.

### 7. Combine and re-measure
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
- Priorities: security and privacy, then speed, then **₹0 cost, always**.
- Free GPU budget is ~4.3 h/day shared with Jarvis. Each Coder start costs ~6.5 min before any work.
  Tell them what a plan will cost in GPU minutes before spending it.
- They asked to be **warned before the daily limit ends a run**: that is built (every answer can
  carry a "Free limit: ..." line). Do not remove it.
- Use they/them for the owner.
- The one thing still on their list: nothing. Their setup is complete.
