"""Start the coder helper on Kaggle: a coding model for Claude Code (notebooks/coder.ipynb).

Runs INSTEAD of Jarvis (same ngrok address, same free GPU hours), so it refuses to start
while Jarvis is running. Flow: secrets -> free-limit check -> Ollama + coder model (30B,
else the 14B) -> reranker -> proxy (coder_proxy.py) behind ngrok -> watchdog (idle stop,
free-limit stop, usage recording). Logs show timings and counts only, never code or answers.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config

MODELFILE = "FROM {base}\nPARAMETER num_ctx {ctx}\nPARAMETER temperature 0.2\nPARAMETER top_p 0.9\n"
MIN_KEY_LENGTH = 32
SPEED_PROMPT_LINES = 300       # ~3000 tokens of code to read, for the reading-speed number


def now_ist() -> datetime:
    return datetime.now(ZoneInfo(config.TIMEZONE))


def modelfile(base: str) -> str:
    return MODELFILE.format(base=base, ctx=config.CODER_CONTEXT_TOKENS)


def ollama_env() -> dict[str, str]:
    """Ollama settings for the coder: long context fits because the cache is stored smaller."""
    env = {"OLLAMA_FLASH_ATTENTION": "1" if config.CODER_FLASH_ATTENTION else "0",
           "OLLAMA_NUM_PARALLEL": "1",           # one user: the whole context memory for one request
           "OLLAMA_MAX_LOADED_MODELS": "2",      # the coder + the small embedding model
           "OLLAMA_KEEP_ALIVE": "-1"}
    if config.CODER_FLASH_ATTENTION and config.CODER_KV_CACHE_TYPE:
        env["OLLAMA_KV_CACHE_TYPE"] = config.CODER_KV_CACHE_TYPE
    return env


def key_fingerprint(key: str) -> str:
    """First 8 characters of the key's SHA-256: lets you compare the key here with the one on the
    laptop (`coder_status` shows the same) without revealing it."""
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def kernel_active(status_output: str) -> bool:
    """Does `kaggle kernels status` say the notebook is running or queued?"""
    text = status_output.lower()
    return "running" in text or "queued" in text


def speed_report(resp: dict) -> str:
    """Numbers from an Ollama answer (durations are in nanoseconds)."""
    def rate(count_key: str, duration_key: str) -> str:
        count, duration = resp.get(count_key) or 0, resp.get(duration_key) or 0
        return f"{count} tok at {count / (duration / 1e9):.0f}/s" if count and duration else "n/a"
    return (f"load {(resp.get('load_duration') or 0) / 1e9:.1f}s, "
            f"reading {rate('prompt_eval_count', 'prompt_eval_duration')}, "
            f"writing {rate('eval_count', 'eval_duration')}")


NAMES = {"day": "today's GPU time", "week": "the GPU time of the last 7 days",
         "requests": "this month's free link requests", "data": "this month's free link data"}


def limit_warning(meters, now, elapsed_s: float = 0.0) -> dict | None:
    """What to tell the user about the free limits, or None while there is plenty left. The
    tightest meter wins. `minutes_left` counts to the automatic stop, and for GPU-time meters the
    Coder's own session cap is counted too, so the number never promises more than it can give."""
    if not meters:
        return None
    from budget import describe_reset, minutes_until_stop
    worst = max(meters, key=lambda m: m.fraction)
    left = minutes_until_stop(worst, config.STOP_AT)
    if left is None:                                     # link meters: no minutes, only a percentage
        if worst.fraction < config.NOTIFY_AT:
            return None
        left_minutes = None
    else:
        session_left = max(0, int(config.CODER_MAX_SESSION_MIN - elapsed_s / 60))
        left_minutes = min(left, session_left)
        if worst.fraction < config.NOTIFY_AT and left_minutes > config.CODER_WARN_MIN_LEFT:
            return None
    return {"meter": worst.name, "label": NAMES.get(worst.name, worst.name),
            "percent": worst.percent, "minutes_left": left_minutes,
            "resets": describe_reset(worst, now),
            "text": (f"{NAMES.get(worst.name, worst.name)} is at {worst.percent}% of the free limit"
                     + (f"; about {left_minutes} min of Coder time left today" if left_minutes is not None else "")
                     + f". It stops itself at {config.STOP_AT:.0%}. {describe_reset(worst, now)}")}


def stop_reason(idle_s: float, elapsed_s: float, over_limit: str | None) -> str | None:
    """Why the run should end now, or None."""
    if over_limit:
        return f"free limit reached ({over_limit})"
    if elapsed_s >= config.CODER_MAX_SESSION_MIN * 60:
        return f"{config.CODER_MAX_SESSION_MIN} min session cap"
    if idle_s >= config.CODER_IDLE_MIN * 60:
        return f"{config.CODER_IDLE_MIN} min without a request"
    return None


class UsageRecorder:
    """Adds this run's GPU time and link traffic to the saved usage numbers, so Jarvis' free-limit
    guard (and the Start button) count it. Each time it reads the LATEST saved memory, changes only
    those numbers and saves, so a Jarvis session saved in between is kept. If the memory cannot be
    read it saves nothing (an empty memory must never replace the real one) and tries again later."""

    def __init__(self, store, state, clock=now_ist, clock_seconds=time.time):
        self.store, self.state, self.clock, self.seconds = store, state, clock, clock_seconds
        self.recorded_until = clock_seconds()
        self.overhead_pending = config.SESSION_OVERHEAD_MIN * 60.0   # Kaggle's own start-up time
        self.unsaved_requests = 0
        self.unsaved_bytes = 0

    def record(self):
        """Save the time and traffic since the last call. Returns the meters, or None if not saved."""
        from budget import current_meters, usage_snapshot
        from memory import Memory
        now, t = self.clock(), self.seconds()
        used = (t - self.recorded_until) + self.overhead_pending
        requests, nbytes = self.state.take_traffic()
        self.unsaved_requests += requests
        self.unsaved_bytes += nbytes
        try:
            memory = Memory(None, self.store.load())
            memory.add_usage(now.date().isoformat(), used)
            memory.add_net(now.strftime("%Y-%m"), self.unsaved_requests, self.unsaved_bytes)
            saved = self.store.save(memory.to_bytes(), usage_snapshot(memory, now))
        except Exception as e:
            print(f"[coder] could not record usage ({type(e).__name__}); will try again")
            saved = False
        if not saved:
            return None
        self.recorded_until, self.overhead_pending = t, 0.0
        self.unsaved_requests = self.unsaved_bytes = 0
        return current_meters(memory, now)


def ollama_json(path: str, body: dict, timeout: float = 900) -> dict:
    request = urllib.request.Request(config.OLLAMA_URL + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def create_alias(base: str) -> None:
    """`jarvis-coder` = the chosen model with our context size baked in, so even OpenAI-style
    clients (which can't send num_ctx) always use the one fixed value."""
    work = config.WORK_DIR / "coder"
    work.mkdir(parents=True, exist_ok=True)
    (work / "Modelfile").write_text(modelfile(base))
    env = {**os.environ, "OLLAMA_HOST": config.OLLAMA_URL.removeprefix("http://")}
    result = subprocess.run(["ollama", "create", config.CODER_ALIAS, "-f", "Modelfile"], cwd=work,
                            env=env, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"ollama create failed: {result.stderr.strip()[-200:]}")


def wanted_models() -> tuple[str, ...]:
    """Which model to try first, then the fallback. `config.CODER_MODEL` is frozen at import from
    the environment, which a Start-button run does not have; experiment F needs to compare the 30B
    with the 14B, so the choice is also read from the run's secrets bundle (`PVA_CODER_MODEL`)
    here, at call time. An unknown name is ignored rather than failing the whole run."""
    chosen = config.CODER_MODEL
    try:
        from_bundle = config.get_secret("PVA_CODER_MODEL", required=False)
    except Exception:
        from_bundle = None
    if from_bundle:
        name = from_bundle.strip()
        if name in (config.CODER_MODEL, config.CODER_FALLBACK_MODEL):
            chosen = name
            print(f"[coder] model from the run's settings: {chosen}")
        else:
            print(f"[coder] ignoring an unknown PVA_CODER_MODEL; using {chosen}")
    others = tuple(m for m in (config.CODER_MODEL, config.CODER_FALLBACK_MODEL) if m != chosen)
    return (chosen, *others)


def load_coder_model(pull) -> str:
    """Download, load and test the coder model; the smaller one if the first fails. Prints the
    speed numbers (load, reading and writing tokens per second). Returns the model used."""
    last_error = "no model tried"
    for base in wanted_models():
        t = time.time()
        try:
            config.PHASE = f"downloading {base}"
            if not pull(base):
                raise RuntimeError("download failed or timed out")
            print(f"[coder] {base} downloaded in {time.time() - t:.0f}s")
            create_alias(base)
            config.PHASE = f"loading {base} onto the GPUs"
            t = time.time()
            first = ollama_json("/api/chat", {
                "model": config.CODER_ALIAS, "stream": False, "keep_alive": -1,
                "messages": [{"role": "user", "content": "Say OK."}], "options": {"num_predict": 5}})
            print(f"[coder] {base} loaded and answered in {time.time() - t:.0f}s ({speed_report(first)})")
            probe_speed()
            return base
        except Exception as e:
            last_error = f"{type(e).__name__}: {str(e)[:160]}"
            print(f"[coder] {base} did not start ({last_error}); trying the smaller model")
    raise RuntimeError(f"No coder model could be started. Last error: {last_error}")


def probe_speed() -> None:
    """Two short tests so the log says how fast this model really is on this run."""
    try:
        read = ollama_json("/api/chat", {
            "model": config.CODER_ALIAS, "stream": False, "keep_alive": -1,
            "messages": [{"role": "user", "content": "def f(x): return x + 1\n" * SPEED_PROMPT_LINES
                          + "\nHow many functions are defined above? Answer with the number only."}],
            "options": {"num_predict": 8}})
        write = ollama_json("/api/chat", {
            "model": config.CODER_ALIAS, "stream": False, "keep_alive": -1,
            "messages": [{"role": "user", "content": "Write a Python function that reverses a string."}],
            "options": {"num_predict": 120}})
        print(f"[coder] speed: reading ({speed_report(read)}); writing ({speed_report(write)})")
    except Exception as e:
        print(f"[coder] speed test skipped ({type(e).__name__})")


def other_run_active(username: str, env: dict, slug: str) -> bool:
    result = subprocess.run(["kaggle", "kernels", "status", f"{username}/{slug}"], env={**os.environ, **env},
                            capture_output=True, text=True, timeout=120)
    return result.returncode == 0 and kernel_active(result.stdout)


def watchdog(state, recorder, server, started: float, over_limit_of, warning: dict | None = None) -> None:
    """Every 5 s: end the run when idle, past the session cap, or at a free limit. Every usage
    record also refreshes the limit warning that /health publishes, and says it once in the log."""
    last_record = time.time()
    over_limit = None
    announced: set[str] = set()
    while not server.should_exit:
        time.sleep(5)
        if time.time() - last_record >= config.CODER_USAGE_EVERY_MIN * 60:
            last_record = time.time()
            meters = recorder.record()
            over_limit = over_limit_of(meters)
            if warning is not None:
                warning.clear()
                note = limit_warning(meters, now_ist(), time.time() - started) if meters else None
                if note:
                    warning.update(note)
                    if note["meter"] not in announced:   # once per meter: the log stays readable
                        announced.add(note["meter"])
                        print(f"[coder] free-limit warning: {note['text']}")
        reason = stop_reason(state.idle_seconds(), time.time() - started, over_limit)
        if reason:
            print(f"[coder] stopping: {reason}")
            server.should_exit = True


def run() -> None:
    started = time.time()
    ctx: dict = {}
    threading.Thread(target=_startup_guard, args=(ctx,), daemon=True).start()
    import main
    threading.Thread(target=main._heartbeat, args=(ctx,), daemon=True).start()
    try:
        _run(ctx, started)
    except BaseException:
        if "recorder" in ctx and not ctx.get("ready"):
            ctx["recorder"].record()      # a start that crashes still used GPU time
        raise


def _startup_guard(ctx: dict) -> None:
    time.sleep(config.STARTUP_TIMEOUT_MIN * 60)
    if not ctx.get("ready"):
        print(f"[coder] not ready after {config.STARTUP_TIMEOUT_MIN} min: stopping to save GPU time.")
        if "recorder" in ctx:
            ctx["recorder"].record()
        os._exit(1)


def _run(ctx: dict, started: float) -> None:
    import setup_env
    setup_env.install_basics()
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "httpx", "fastapi", "uvicorn"], check=False)

    import crypto
    from budget import Budget, current_meters, describe_reset, start_blocker
    from memory import Memory
    from persistence import MemoryStore

    key = crypto.load_key(config.get_secret("MEMORY_KEY"))
    coder_key = config.get_secret("CODER_KEY")
    if len(coder_key) < MIN_KEY_LENGTH:
        raise RuntimeError("CODER_KEY is too short. Make one with: python scripts/make_key.py")
    ngrok_token = config.get_secret("NGROK_AUTHTOKEN")
    ngrok_domain = config.get_secret("NGROK_DOMAIN")
    force = bool(config.get_secret("CODER_FORCE", required=False))
    username, kaggle_env = config.kaggle_credentials()

    if not force and other_run_active(username, kaggle_env, config.JARVIS_NOTEBOOK_SLUG):
        print("\n[coder] NOT STARTING: Jarvis is running. They share one address and one memory, "
              "so run only one at a time. Close Jarvis first (or start Coder with 'force').")
        ctx["ready"] = True
        return

    store = MemoryStore(username, key, kaggle_env)
    memory = Memory(None, store.load())
    config.NGROK_IN_USE = True           # the link meters count too
    now = now_ist()
    meters = current_meters(memory, now)
    from coder_proxy import ProxyState
    state = ProxyState()
    ctx["recorder"] = recorder = UsageRecorder(store, state)
    for m in meters:
        print(f"[budget] {m.name:8} {m.percent:3d}% of free limit")
    budget = Budget(config.NOTIFY_AT, config.STOP_AT)
    blocked = start_blocker(meters, budget)
    if blocked:
        print(f"\n[budget] NOT STARTING: '{blocked.name}' is at {blocked.percent}% of its free limit "
              f"(auto-stop at {config.STOP_AT:.0%}). {describe_reset(blocked, now)}")
        recorder.record()
        ctx["ready"] = True
        return

    # --- Ollama + models ---------------------------------------------------------------
    config.PHASE = "installing Ollama"
    setup_env.install_ngrok(None)
    models_dir = setup_env.install_ollama(None)
    config.PHASE = "starting Ollama"
    setup_env.start_ollama(models_dir, gpus=config.CODER_GPUS, extra_env=ollama_env())
    t = time.time()
    model = load_coder_model(setup_env._pull)
    config.PHASE = "downloading the embedding model"
    if not setup_env._pull(config.CODER_EMBED_MODEL):
        print("[coder] the embedding model did not download: meaning-search is off this run")
    devices = setup_env.ollama_devices()
    print(f"[coder] Ollama computes on: {', '.join(devices) or 'unknown'}")
    if devices and all(d.startswith("cpu") for d in devices):
        print("[coder] WARNING: no GPU found, so answers will be very slow. Check Accelerator = GPU T4 x2.")

    config.PHASE = "loading the reranker"
    import coder_rerank
    import torch
    reranker = coder_rerank.load("cuda:1" if torch.cuda.device_count() > 1 else "cuda:0")
    print(f"[coder] models ready {time.time() - t:.0f}s after Ollama started")

    # --- proxy + tunnel ----------------------------------------------------------------
    config.PHASE = "starting the web app"
    import uvicorn
    import tunnel
    from coder_proxy import create_app
    warning: dict = {}
    first = limit_warning(meters, now, time.time() - started)
    if first:
        warning.update(first)
        print(f"[coder] free-limit warning: {first['text']}")
    app = create_app(coder_key, config.OLLAMA_URL, state, rerank=reranker,
                     warning=lambda: dict(warning) or None)
    server = uvicorn.Server(uvicorn.Config(app, host=config.APP_HOST, port=config.APP_PORT,
                                           log_level="warning", access_log=False))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    ngrok = tunnel.start(ngrok_token, ngrok_domain, None, coder=True)
    state.touch()          # the idle clock starts now, not when the (long) start-up began
    print(f"\n[ready] in {time.time() - started:.0f}s")
    print(f"[coder] key fingerprint {key_fingerprint(coder_key)} (length {len(coder_key)}); "
          "`coder_status` on the laptop shows the same if the keys match")
    print(f"[ready]  -> https://{ngrok_domain.removeprefix('https://').strip('/')}  (coder: key required; model {model})")
    print(f"[ready] closes after {config.CODER_IDLE_MIN} min without a request, or at {config.STOP_AT:.0%} "
          "of any free limit\n")
    ctx["ready"] = True

    def over_limit_of(meters):
        over = budget.over_stop(meters) if meters else None
        return over.name if over else None

    threading.Thread(target=watchdog, args=(state, recorder, server, started, over_limit_of, warning),
                     daemon=True).start()
    try:
        while server_thread.is_alive():
            server_thread.join(timeout=1)
    except KeyboardInterrupt:
        print("[coder] stopped by you")
        server.should_exit = True
        server_thread.join(timeout=10)
    finally:
        recorder.record()
        ngrok.terminate()
        subprocess.run(["pkill", "-f", "ollama serve"], capture_output=True)
        print("[coder] finished. If you used 'Run All' (not 'Save & Run All'), click Stop Session "
              "in Kaggle now to save GPU hours.")
