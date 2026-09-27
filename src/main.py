"""Start everything. The notebook just calls run()."""
import subprocess
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config


def run() -> None:
    started = time.time()
    import setup_env
    setup_env.install_basics()

    import crypto
    from budget import Budget, current_meters, minutes_until_stop
    from memory import Memory
    from persistence import MemoryStore
    from timer import SessionTimer

    # --- secrets (never printed) ---------------------------------------------
    key = crypto.load_key(config.get_secret("MEMORY_KEY"))
    username, kaggle_env = config.kaggle_credentials()
    ngrok_token = config.get_secret("NGROK_AUTHTOKEN")
    ngrok_domain = config.get_secret("NGROK_DOMAIN")
    allowed = config.get_secret("ALLOWED_EMAIL", required=False)
    allowed_emails = [e.strip().lower() for e in allowed.split(",") if e.strip()] if allowed else []
    password = config.get_secret("APP_PASSWORD", required=False)
    if not allowed_emails and not password:
        raise RuntimeError("Add the ALLOWED_EMAIL secret (Google sign-in) or APP_PASSWORD. "
                           "The assistant never starts without a login.")

    # --- memory + free-limit check, BEFORE the slow GPU setup --------------------
    store = MemoryStore(username, key, kaggle_env)
    data = store.load()
    print("[memory] loaded" if data else "[memory] first run: starting with empty memory")
    memory = Memory(None, data)  # embedding model is attached after setup

    now = datetime.now(ZoneInfo(config.TIMEZONE))
    memory.add_usage(now.date().isoformat(), config.SESSION_OVERHEAD_MIN * 60)
    timer = SessionTimer(now, config.IDLE_TIMEOUT_MIN * 60)  # counts setup time too

    budget = Budget(config.NOTIFY_AT, config.STOP_AT)
    meters = current_meters(memory, now)
    for m in meters:
        left = minutes_until_stop(m, config.STOP_AT)
        print(f"[budget] {m.name:8} {m.percent:3d}% of free limit"
              + (f"  ({left} min until auto-stop)" if left is not None else ""))
    blocked = budget.over_stop(meters) or budget.too_little_left(meters, config.MIN_START_MIN)
    if blocked:
        print(f"\n[budget] NOT STARTING: '{blocked.name}' is at {blocked.percent}% of its free "
              f"limit (auto-stop at {config.STOP_AT:.0%}). This keeps the project free. "
              "Day limits reset at midnight IST, week limits as old days roll off, "
              "link limits next month.")
        store.save(memory.to_bytes())  # record this short session's GPU time
        return
    for m in budget.warnings(meters):
        print(f"[budget] warning: {m.name} is above {config.NOTIFY_AT:.0%}")

    # --- GPU setup --------------------------------------------------------------------
    paths, llm_model = setup_env.prepare()
    config.LLM_MODEL = llm_model

    import llm
    import tunnel
    import uvicorn
    from app import Assistant, PasswordGate, create_app, now_ist
    from memory import E5Embedder
    from stt import SpeechToText
    from tts import TextToSpeech

    device = f"cuda:{paths.speech_device_index}"
    memory.set_embedder(E5Embedder(paths.embed, device=device))

    # Phase 5: fine-tuned model, if one is attached -> blind A/B test, then the winner
    tuned = setup_env.install_tuned_model(key)
    picker = llm.ModelPicker(llm_model, tuned, memory.rating_stats(),
                             config.TUNED_MODE, config.AB_MIN_RATINGS)
    if tuned:
        stats = memory.rating_stats()
        print(f"[models] base {llm_model}: 👍/👎 {stats.get(llm_model, (0, 0))}  |  "
              f"tuned {tuned}: 👍/👎 {stats.get(tuned, (0, 0))}  ->  using: {picker.decision}")

    # --- speech models + warm-up (first calls are slow, so do them now) -------------
    stt = SpeechToText(paths.whisper, paths.speech_device_index)
    tts = TextToSpeech(paths.kokoro, device)
    for lang in tts.pipelines:
        tts.synthesize("Hello.", lang)
    import numpy as np
    list(stt.model.transcribe(np.zeros(16000, dtype=np.float32), language="en")[0])
    def warm_up_llm():
        for model in picker.in_use():
            "".join(llm.stream_reply([{"role": "user", "content": "Say OK."}], model))

    t = time.time()
    try:
        warm_up_llm()
    except Exception as e:
        print(f"[setup] LLM test failed: {e}\n[setup] Ollama log:\n{setup_env.ollama_log_tail()}")
        print("[setup] reinstalling Ollama from the internet and trying once more")
        setup_env.reinstall_ollama_online()
        warm_up_llm()
    print(f"[setup] LLM warm-up done in {time.time() - t:.1f}s")

    # --- web app + tunnel ---------------------------------------------------------
    assistant = Assistant(stt, tts, memory, store, timer, restart_ollama=setup_env.start_ollama,
                          picker=picker)
    gate = None if allowed_emails else PasswordGate(password, key)
    server = uvicorn.Server(uvicorn.Config(create_app(assistant, gate), host=config.APP_HOST,
                                           port=config.APP_PORT, log_level="warning",
                                           access_log=False))
    assistant.server = server
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    ngrok = tunnel.start(ngrok_token, ngrok_domain, allowed_emails or None)

    timer.touch(now_ist())
    threading.Thread(target=assistant.watchdog, daemon=True).start()
    url = "https://" + ngrok_domain.removeprefix("https://").strip("/")
    login = "Google sign-in" if allowed_emails else "password page"
    print(f"\n[ready] in {time.time() - started:.0f}s  ->  {url}  ({login})")
    print(f"[ready] closes after {config.IDLE_TIMEOUT_MIN} min without use, "
          f"or automatically at {config.STOP_AT:.0%} of any free limit\n")

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
        ngrok.terminate()
        subprocess.run(["pkill", "-f", "ollama serve"], capture_output=True)
        print("[session] finished. If you used 'Run All' (not 'Save & Run All'), "
              "click Stop Session in Kaggle now to save GPU hours.")
