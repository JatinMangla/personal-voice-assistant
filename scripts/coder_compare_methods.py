"""Compare `ask_repo` with `investigate` (the agentic loop) on the benchmark's trace questions.

    .venv\\Scripts\\python scripts\\coder_compare_methods.py              # 3 repeats each
    .venv\\Scripts\\python scripts\\coder_compare_methods.py --repeats 1  # a quick look

Why this exists instead of a benchmark variant: the trace score wanders by up to 5 steps on the
same question (gotcha 35), so one pass each cannot tell two methods apart. Every question is asked
`--repeats` times by both methods, interleaved so neither gets a warmer model, and the MEDIAN is
compared. The grading step lists are the benchmark's own, so the numbers line up with
`coder_bench.py` and the table in docs/CODER_IMPROVEMENTS.md.

Cost: with 3 repeats that is 9 `ask_repo` calls plus 9 `investigate` runs (each 1 call per step
plus 2), so roughly 50-60 model calls and 8-12 minutes of GPU time. Check the free limit first.

Needs a running Coder (Start Coder) and CODER_KEY in the shell. Nothing real is touched: a copy of
the index, and a fresh throwaway answer cache for every single pass.
"""
import argparse
import importlib.util
import json
import os
import shutil
import statistics as st
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from coder import service                      # noqa: E402
from coder.client import CoderClient           # noqa: E402

_spec = importlib.util.spec_from_file_location("coder_bench", ROOT / "scripts" / "coder_bench.py")
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)

METHODS = ("ask_repo", "investigate")


class Recording(CoderClient):
    """The normal client, but it remembers what every call cost."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls: list[dict] = []

    def chat(self, messages, schema=None, max_tokens=800, background=False):
        body = {"model": "jarvis-coder", "messages": messages, "stream": False,
                "options": {"num_predict": max_tokens, "temperature": 0.2}}
        if schema:
            body["format"] = schema
        start = time.time()
        resp = self._call("POST", "/api/chat", background=background, json=body)
        data = resp.json()
        self.last_done_reason = data.get("done_reason")
        self.calls.append({"seconds": round(time.time() - start, 1),
                           "prompt_tokens": data.get("prompt_eval_count") or 0,
                           "reply_tokens": data.get("eval_count") or 0})
        return data["message"]["content"]


def fresh_coder(client) -> service.Coder:
    """A Coder with a copy of the real index but an empty answer cache, so every pass really asks."""
    home = Path(tempfile.mkdtemp(prefix="coder-compare-"))
    real = service.default_home() / "index.db"
    if real.exists():
        shutil.copy2(real, home / "index.db")
    coder = service.Coder(home, service.default_roots(ROOT), client=client)
    coder.index.refresh()
    return coder


def grade(answer: str, steps: dict) -> tuple[list[str], list[str]]:
    low = answer.lower()
    got = [name for name, alts in steps.items() if any(a in low for a in alts)]
    return got, [name for name in steps if name not in got]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repeats", type=int, default=3,
                       help="passes per question per method; the median is compared (default 3)")
    parser.add_argument("--save", default="", help="also write the raw numbers to this JSON file")
    args = parser.parse_args()

    if not os.environ.get("CODER_KEY"):
        sys.exit("CODER_KEY is not set in this shell.")
    client = Recording(os.environ.get("CODER_URL", "").strip()
                       or "https://volley-gnarly-constable.ngrok-free.dev",
                       os.environ["CODER_KEY"].strip())
    if not client.online():
        sys.exit("The Coder is not online: press Start Coder on GitHub and wait for [ready].")
    print(client.warning_line().strip() or "(no free-limit warning yet)")

    scores = {m: {i: [] for i in range(len(bench.TRACES))} for m in METHODS}
    cost = {m: [] for m in METHODS}
    misses = {m: {i: [] for i in range(len(bench.TRACES))} for m in METHODS}

    for rep in range(args.repeats):
        for method in METHODS:
            for i, (question, steps) in enumerate(bench.TRACES):
                coder = fresh_coder(client)
                before, start = len(client.calls), time.time()
                answer = getattr(coder, method)(question)
                wall = time.time() - start
                # A pass that made NO model call did not measure anything: the first run of this
                # script scored 9 such passes as real results (the Coder had briefly gone away and
                # `_offline_until` hid it for 30 s), and the "+0 steps" tie was meaningless. A
                # fallback or a cache hit must stop the run, not quietly become a number.
                made_calls = len(client.calls) - before
                if not made_calls or "Closest code instead" in answer or "(cached" in answer:
                    sys.exit(f"\n{method} on trace{i + 1} did not reach the model "
                             f"({made_calls} calls). The run is void, so nothing is scored. "
                             "Check /health and the free limits, then start again.")
                got, missed = grade(answer, steps)
                scores[method][i].append(len(got))
                misses[method][i].append(missed)
                cost[method].append({
                    "calls": len(client.calls) - before, "seconds": round(wall, 1),
                    "prompt_tokens": sum(c["prompt_tokens"] for c in client.calls[before:])})
                print(f"rep{rep + 1} {method:12} trace{i + 1}: {len(got):2}/{len(steps)}  "
                      f"{len(client.calls) - before:2} calls {wall:5.0f}s  missed {missed}")

    print("\n" + "=" * 72)
    medians = {}
    for method in METHODS:
        per = [st.median(scores[method][i]) for i in range(len(bench.TRACES))]
        medians[method] = sum(per)
        total_of = sum(len(s) for _, s in bench.TRACES)
        print(f"{method:12} medians {[int(p) for p in per]} = {sum(per):.0f}/{total_of}   "
              f"raw {[scores[method][i] for i in range(len(bench.TRACES))]}")
    for method in METHODS:
        rows = cost[method]
        print(f"{method:12} per question: {st.mean(r['calls'] for r in rows):.1f} calls, "
              f"{st.mean(r['seconds'] for r in rows):.0f}s, "
              f"{st.mean(r['prompt_tokens'] for r in rows):.0f} prompt tokens")

    spread = max(max(scores[m][i]) - min(scores[m][i])
                 for m in METHODS for i in range(len(bench.TRACES)))
    gap = medians["investigate"] - medians["ask_repo"]
    print(f"\ninvestigate - ask_repo = {gap:+.0f} steps; the widest spread within one question "
          f"was {spread} steps.")
    print("A gap no bigger than that spread is NOT a result: say so rather than keeping it."
          if abs(gap) <= spread else
          "The gap is wider than the noise, so it is worth recording in CODER_IMPROVEMENTS.md.")

    if args.save:
        Path(args.save).write_text(json.dumps(
            {"repeats": args.repeats,
             "scores": {m: {str(i): v for i, v in d.items()} for m, d in scores.items()},
             "misses": {m: {str(i): v for i, v in d.items()} for m, d in misses.items()},
             "cost": cost, "medians": medians}, indent=1), encoding="utf-8")
        print("saved", args.save)


if __name__ == "__main__":
    main()
