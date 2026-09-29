"""Start everything. The notebook just calls run(); on a home PC, scripts/run_home.py does."""
import os
import subprocess
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config


def run() -> None:
    ctx: dict = {}
    if not config.HOME:  # a hung start on Kaggle would burn free GPU hours
        threading.Thread(target=_startup_guard, args=(ctx,), daemon=True).start()
    threading.Thread(target=_heartbeat, args=(ctx,), daemon=True).start()
    run_type = os.environ.get("KAGGLE_KERNEL_RUN_TYPE")  # "Batch" = Save & Run All
    if run_type:
        print(f"[setup] Kaggle run type: {run_type}")
        _catch_stop_signal()
    try:
        _run(ctx)
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            print(f"[session] run interrupted while: {_phase()}")
        # A start that crashes still used GPU time: record it, so the free-limit
        # guard stays accurate.
        if "memory" in ctx and not ctx.get("ready"):
            _record_failed_start(ctx)
        raise


def _phase() -> str:
    return config.PHASE + (f"; {config.LLM_PHASE}" if config.LLM_PHASE else "")


def _heartbeat(ctx: dict, every: float = 20) -> None:
    """A line every 20 s until ready: silent steps (downloads) are visible, and if Kaggle
    stops the run, the log shows when and during which step."""
    start = time.time()
    while True:
        time.sleep(every)
        if ctx.get("ready"):
            return
        print(f"[setup] still starting ({time.time() - start:.0f}s): {_phase()}")


def _catch_stop_signal() -> None:
    """If Kaggle (or anyone) stops the run, say so in the log before exiting; the normal
    failed-start path then records the GPU time."""
    import signal

    def stopped(signum, frame):
        print(f"[session] got stop signal {signum} while: {_phase()}")
        raise SystemExit(1)
    try:
        signal.signal(signal.SIGTERM, stopped)
    except (ValueError, OSError):  # not the main thread (e.g. tests)
        pass


def _startup_guard(ctx: dict) -> None:
    """A start that hangs (e.g. a stuck download) would burn free GPU hours for up to
    12 hours. If Jarvis isn't ready in time: record the GPU time and stop the run."""
    time.sleep(config.STARTUP_TIMEOUT_MIN * 60)
    if not ctx.get("ready"):
        print(f"[setup] not ready after {config.STARTUP_TIMEOUT_MIN} min: stopping to save "
              "GPU time. Start Jarvis again later.")
        if "memory" in ctx:
            _record_failed_start(ctx)
        os._exit(1)  # ends the Kaggle run


def _record_failed_start(ctx: dict) -> None:
    from datetime import datetime
    try:
        now = datetime.now(ZoneInfo(config.TIMEZONE))
        ctx["memory"].add_usage(*ctx["timer"].tick(now))
        from budget import usage_snapshot
        ok = ctx["store"].save(ctx["memory"].to_bytes(), usage_snapshot(ctx["memory"], now))
        print(f"[budget] GPU time of this failed start {'recorded' if ok else 'NOT recorded'}")
    except Exception as e:
        print(f"[budget] could not record GPU time: {type(e).__name__}: {e}")


def _keep_new_identity(net, store, memory, now) -> None:
    """Save a NEW Tailscale identity at once: if this start fails later, the next run is
    still the same device (not jarvis-1, jarvis-2 ...)."""
    if not net or not net.fresh_login:
        return
    from budget import usage_snapshot
    blob = net.state_blob()
    if blob and store.save(memory.to_bytes(), usage_snapshot(memory, now),
                           extras={config.TAILSCALE_STATE_FILE: blob}):
        net.fresh_login = False
        print("[tailscale] new identity saved (next runs reuse it)")


def link_plan(ts_authkey: str | None, ngrok: tuple[str, str] | None) -> dict:
    """Which links to open. tailscale_only: ngrok is not started (it is kept aside as the
    fallback in case Tailscale fails). ngrok: (token, domain) to start, or None."""
    tailscale_only = bool(ts_authkey) and not config.USE_NGROK and not config.HOME
    return {"tailscale_only": tailscale_only,
            "ngrok": None if tailscale_only else ngrok,
            "spare": ngrok if tailscale_only and config.NGROK_FALLBACK else None}


def _run(ctx: dict) -> None:
    started = time.time()
    import setup_env
    import setup_home
    setup = setup_home if config.HOME else setup_env
    setup.install_basics()

    import crypto
    from budget import (Budget, current_meters, describe_reset, minutes_until_stop,
                        start_blocker, usage_snapshot)
    from memory import Memory
    from persistence import LocalMemoryStore, MemoryStore
    from timer import SessionTimer

    # --- secrets (never printed) ---------------------------------------------
    key = crypto.load_key(config.get_secret("MEMORY_KEY"))
    # Tailscale (optional, private): with TS_AUTHKEY, ngrok becomes optional too.
    ts_authkey = config.get_secret("TS_AUTHKEY", required=False)
    # At home the tunnel is optional: without it Jarvis is reachable on this PC only.
    ngrok_token = config.get_secret("NGROK_AUTHTOKEN", required=not config.HOME and not ts_authkey)
    ngrok_domain = config.get_secret("NGROK_DOMAIN", required=bool(ngrok_token))
    plan = link_plan(ts_authkey, (ngrok_token, ngrok_domain) if ngrok_token else None)
    ngrok_token, ngrok_domain = plan["ngrok"] or (None, None)
    allowed = config.get_secret("ALLOWED_EMAIL", required=False)
    all_emails = [e.strip().lower() for e in allowed.split(",") if e.strip()] if allowed else []
    password = config.get_secret("APP_PASSWORD", required=False)

    def use_ngrok_logins() -> list[str]:
        """Google sign-in happens at ngrok, so it only applies while ngrok is used."""
        config.NGROK_IN_USE = bool(ngrok_token)
        if config.NGROK_IN_USE and not all_emails and not password:
            raise RuntimeError("Add the ALLOWED_EMAIL secret (Google sign-in) or APP_PASSWORD. "
                               "The assistant never starts without a login.")
        return all_emails if ngrok_token else []
    allowed_emails = use_ngrok_logins()

    # --- memory + free-limit check, BEFORE the slow GPU setup --------------------
    if config.HOME:
        store = LocalMemoryStore(config.HOME_DIR, key)
    else:
        username, kaggle_env = config.kaggle_credentials()
        store = MemoryStore(username, key, kaggle_env)
    data = store.load()
    print("[memory] loaded" if data else "[memory] first run: starting with empty memory")
    memory = Memory(None, data)  # embedding model is attached after setup

    now = datetime.now(ZoneInfo(config.TIMEZONE))
    memory.add_usage(now.date().isoformat(), config.SESSION_OVERHEAD_MIN * 60)
    timer = SessionTimer(now, config.IDLE_TIMEOUT_MIN * 60)  # counts setup time too
    ctx.update(memory=memory, store=store, timer=timer)

    budget = Budget(config.NOTIFY_AT, config.STOP_AT)
    meters = current_meters(memory, now)
    for m in meters:
        left = minutes_until_stop(m, config.STOP_AT)
        print(f"[budget] {m.name:8} {m.percent:3d}% of free limit"
              + (f"  ({left} min until auto-stop)" if left is not None else ""))
    blocked = start_blocker(meters, budget)
    if blocked:
        print(f"\n[budget] NOT STARTING: '{blocked.name}' is at {blocked.percent}% of its free "
              f"limit (auto-stop at {config.STOP_AT:.0%}). This keeps the project free. "
              f"{describe_reset(blocked, now)}")
        store.save(memory.to_bytes(), usage_snapshot(memory, now))  # record this GPU time
        ctx["ready"] = True  # already recorded
        return
    for m in budget.warnings(meters):
        print(f"[budget] warning: {m.name} is above {config.NOTIFY_AT:.0%}")

    # --- Tailscale joins the tailnet in the background, while the models load ---------
    import tailnet
    net_holder: dict = {}
    net_thread = threading.Thread(target=lambda: net_holder.update(net=tailnet.setup(
        config.WORK_DIR, store.extras.get(config.TAILSCALE_STATE_FILE))), daemon=True)
    net_thread.start()
    if plan["tailscale_only"]:  # the only way in: make sure it works BEFORE the GPU work
        net_thread.join(timeout=150)
        if not net_holder.get("net"):
            if not plan["spare"]:
                raise RuntimeError("Tailscale did not start (see the [tailscale] line above), and "
                                   "there is no ngrok fallback. Stopped before loading the models.")
            print("[tailscale] could not start, so this run uses the ngrok link (Google sign-in)")
            ngrok_token, ngrok_domain = plan["spare"]
            allowed_emails = use_ngrok_logins()
        _keep_new_identity(net_holder.get("net"), store, memory, now)

    # --- GPU setup --------------------------------------------------------------------
    if config.WEB_SEARCH:  # before the LLM warms up in the background
        import websearch
        websearch.register()
    import reminders as reminders_module  # reminder tools too (they work without internet)
    ntfy_topic = config.get_secret("NTFY_TOPIC", required=False)
    push = reminders_module.Push(ntfy_topic, config.NTFY_SERVER) if ntfy_topic else None
    reminders = reminders_module.Reminders(memory, push)
    reminders_module.register(reminders, lambda: datetime.now(ZoneInfo(config.TIMEZONE)))
    import home  # home devices + cameras, only if HA_URL and HA_TOKEN are set
    home.setup()
    import bridge as bridge_module  # appliances via the home helper, only with BRIDGE_KEY
    home_bridge = bridge_module.setup()

    # The LLM downloads, loads (GPU 0) and warms up in the background ...
    paths, llm_job = setup.prepare()

    import llm
    import tunnel
    import uvicorn
    from app import Assistant, PasswordGate, create_app, now_ist
    from memory import E5Embedder
    from stt import SpeechToText
    from tts import TextToSpeech

    # ... while the speech models load here (GPU 1); first calls are slow, so do them now.
    config.PHASE = "loading the speech models"
    t = time.time()
    device = f"cuda:{paths.speech_device_index}" if paths.device == "cuda" else "cpu"
    memory.set_embedder(E5Embedder(paths.embed, device=device))
    stt = SpeechToText(paths.whisper, paths.speech_device_index, paths.device)
    tts = TextToSpeech(paths.kokoro, device)
    for lang in tts.pipelines:
        tts.synthesize("Hello.", lang)
    import numpy as np
    list(stt.model.transcribe(np.zeros(16000, dtype=np.float32), language="en")[0])
    print(f"[setup] speech models ready in {time.time() - t:.0f}s")

    config.PHASE = "waiting for the LLM"
    try:
        llm_model = llm_job.result()
    except Exception as e:
        print(f"[setup] LLM start failed: {str(e)[:200]}\n[setup] Ollama log:\n"
              f"{setup.ollama_log_tail()}\n[setup] reinstalling Ollama and trying once more")
        setup.reinstall_ollama_online()
        llm_model = setup.ensure_llm()
    config.LLM_MODEL = llm_model

    # Phase 5: fine-tuned model, if one is attached -> blind A/B test, then the winner
    tuned = setup.install_tuned_model(key)
    picker = llm.ModelPicker(llm_model, tuned, memory.rating_stats(),
                             config.TUNED_MODE, config.AB_MIN_RATINGS)
    if tuned:
        stats = memory.rating_stats()
        print(f"[models] base {llm_model}: 👍/👎 {stats.get(llm_model, (0, 0))}  |  "
              f"tuned {tuned}: 👍/👎 {stats.get(tuned, (0, 0))}  ->  using: {picker.decision}")

    def warm_up_llm():
        for model in picker.in_use():
            llm.load_model(model)  # instant if the preload worked
            t0 = time.time()
            reply = "".join(llm.stream_reply([{"role": "user", "content": "Say OK."}], model,
                                             max_tokens=40))
            # If the reply is long or slow, the model may be "thinking" despite think=False.
            print(f"[setup] {model} test reply in {time.time() - t0:.1f}s "
                  f"({len(reply)} chars; {llm.speed_report()})")

    def report_devices():
        devices = setup.ollama_devices()
        print(f"[setup] Ollama computes on: {', '.join(devices) or 'unknown'}")
        if devices and all(d.startswith("cpu") for d in devices):
            print("[setup] WARNING: Ollama found no GPU, so replies will be very slow. "
                  "Check Accelerator = GPU T4 x2.")

    try:
        warm_up_llm()
    except Exception as e:
        print(f"[setup] LLM test failed: {e}\n[setup] Ollama log:\n{setup.ollama_log_tail()}")
        print("[setup] reinstalling Ollama from the internet and trying once more")
        setup.reinstall_ollama_online()
        warm_up_llm()
    report_devices()
    print(f"[setup] all models ready {time.time() - t:.0f}s after the speech models started")

    # --- web app + tunnel ---------------------------------------------------------
    config.PHASE = "starting the web app"
    assistant = Assistant(stt, tts, memory, store, timer, restart_ollama=setup.start_ollama,
                          picker=picker, reminders=reminders)
    pending = len(reminders.pending())
    pushed = reminders.schedule_pushes(now_ist())  # the next 3 days, if ntfy is set up
    print(f"[reminders] {pending} pending" + (f", {pushed} phone notification(s) scheduled"
                                              if push else ""))
    # Google sign-in (at ngrok), else the password page. Only a home PC without a tunnel
    # may run without either: then the app is reachable from this computer alone.
    gate = None if allowed_emails or not password else PasswordGate(password, key)
    web_app = create_app(assistant, gate)
    server = uvicorn.Server(uvicorn.Config(web_app, host=config.APP_HOST,
                                           port=config.APP_PORT, log_level="warning",
                                           access_log=False))
    assistant.server = server
    net_thread.join(timeout=120)
    net = net_holder.get("net")
    if net:  # the same app on a second local port: only `tailscale serve` talks to it
        ts_server = uvicorn.Server(uvicorn.Config(web_app, host=config.APP_HOST,
                                                  port=config.TAILSCALE_PORT, log_level="warning",
                                                  access_log=False))
        assistant.servers.append(ts_server)
        threading.Thread(target=ts_server.run, daemon=True).start()

        def tailscale_state():
            blob = net.state_blob()
            return {config.TAILSCALE_STATE_FILE: blob} if blob else {}
        assistant.extra_files = tailscale_state
    elif not ngrok_token and not config.HOME:
        raise RuntimeError("No way in: Tailscale did not start and there is no ngrok secret.")
    start_url = config.get_secret("START_URL", required=False)  # e.g. GitHub Start Jarvis page
    assistant.start_url = start_url if start_url and start_url.startswith("https://") else None
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    assistant.bridge = home_bridge
    # The home helper uses Tailscale when it's on; only without it does ngrok need to let
    # the helper's path past Google sign-in.
    ngrok = tunnel.start(ngrok_token, ngrok_domain, allowed_emails or None,
                         bridge_path=bool(home_bridge) and not net) if ngrok_token else None
    if home_bridge:
        woke = home_bridge.announce()  # the helper at home connects now
        print(f"[bridge] home helper {'told that Jarvis is up' if woke else 'not told (ntfy)'}; "
              "waiting for it to connect")

    timer.touch(now_ist())
    ctx["ready"] = True  # from here on the watchdog and close() record usage
    if net and net.fresh_login:
        assistant.save()  # keep the new Tailscale identity, so the next run is the same device
    threading.Thread(target=assistant.watchdog, daemon=True).start()
    if config.HOME:
        import local_voice  # "Hey Jarvis" on this PC's mic and speakers
        local_voice.start(assistant)
    links = []
    if net:
        links.append(f"{net.url}  (Tailscale: private, your tailnet only)")
        if not tailnet.allowed_logins():
            print("[tailscale] WARNING: no one is allowed yet: add ALLOWED_EMAIL or TAILSCALE_USERS")
    if ngrok_token:
        links.append("https://" + ngrok_domain.removeprefix("https://").strip("/")
                     + f"  ({'Google sign-in' if allowed_emails else 'password page'})")
    if not links:
        links.append(f"http://{config.APP_HOST}:{config.APP_PORT}  "
                     f"({'password page' if gate else 'no login'}, this computer only)")
    print(f"\n[ready] in {time.time() - started:.0f}s")
    for link in links:
        print(f"[ready]  -> {link}")
    if config.IDLE_TIMEOUT_MIN:
        print(f"[ready] closes after {config.IDLE_TIMEOUT_MIN} min without use, "
              f"or automatically at {config.STOP_AT:.0%} of any free limit\n")
    else:
        print("[ready] always on (home PC). Stop it with Ctrl+C.\n")

    try:
        while server_thread.is_alive():
            server_thread.join(timeout=1)
    except KeyboardInterrupt:
        print("[session] stopped by you")
        server.should_exit = True
        server_thread.join(timeout=10)
    finally:
        assistant.stopped.set()
        if memory.dirty:
            assistant.save()
        if ngrok:
            ngrok.terminate()
        if net:
            net.stop()
        if config.HOME:
            print("[session] finished. Memory saved.")
        else:
            subprocess.run(["pkill", "-f", "ollama serve"], capture_output=True)
            print("[session] finished. If you used 'Run All' (not 'Save & Run All'), "
                  "click Stop Session in Kaggle now to save GPU hours.")
