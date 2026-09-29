# CLAUDE.md: Personal Voice Assistant ("Jarvis")

Read this first. It is the handover for any Claude Code session working on this repo.
The full history (every problem, fix, measurement) is in [docs/PROJECT_LOG.md](docs/PROJECT_LOG.md);
the user-facing guide is [README.md](README.md); the original plan review is [docs/PLAN.md](docs/PLAN.md);
**what to do next** is in [docs/FUTURE_PLANS.md](docs/FUTURE_PLANS.md);
memory retrieval research and the chosen direction (hybrid SQLite: FTS5 + vectors + RRF +
date/category filters; later time-summary tree + split storage) is [docs/MEMORY_RESEARCH.md](docs/MEMORY_RESEARCH.md).

## What this is
A private, Alexa-style voice assistant that the owner (GitHub `JatinMangla`, Kaggle
`jatinmangla123`, in India) opens as a web app / PWA on phone and laptop. It runs on
**Kaggle's free GPU** (T4 x2) and is reached through a fixed **ngrok** address behind **Google
sign-in**. English + Hindi, voice in and voice out, with long-term encrypted memory, web
search, link cards, price search, screen snapshots, and optional style fine-tuning.

## The owner: how to work with them
- Not a developer by trade here: give **step-by-step, click-by-click** instructions for anything
  they must do on Kaggle / GitHub / ngrok, and say *why* in plain words. They paste logs back.
- Priorities: **1) security/privacy, 2) speed, 3) ₹0 cost, always.** Never suggest anything
  paid or needing a credit card.
- They rejected a passphrase at session start. They first rejected Tailscale, but on
  2026-09-30 asked for it (they already use the Tailscale app and Termux on their phone).
  Tailscale is now the private link (`tailnet.py`). With TS_AUTHKEY set, Jarvis is
  **Tailscale-only** (`config.USE_NGROK = False`, owner is the only user): ngrok is not
  started; `main.link_plan()` keeps it only as a fallback if Tailscale fails
  (`NGROK_FALLBACK`), and a Tailscale-only start with no fallback stops BEFORE the models
  load. "Make it public later" = `USE_NGROK = True`.
- They want: tap-to-talk with auto-stop on silence, 10-min idle auto-close, short answers,
  links as rich cards, YouTube app deep links, prices with the cheapest first.
- Use they/them for the owner in any text.

## Hard rules (don't break these)
1. **Free only.** Stay inside Kaggle (30 GPU h/week) and ngrok free (20k requests + 1 GB/month).
   The budget guard (`src/budget.py`) warns at 90 % and auto-stops at 99 % of: GPU today
   (30 h / 7 ≈ 257 min), GPU in the last 7 days, and ngrok requests/data this month.
2. **Never print or log secrets, transcripts, replies, learned facts or screenshots.** Logs show
   timings and counts only (`[turn] en: heard in 1.1s, first audio at 2.7s ...`).
3. **Memory is encrypted** (AES-256-GCM, `MEMORY_KEY`), lives in RAM as SQLite, and is saved as
   ciphertext to the private Kaggle dataset `assistant-memory` (old versions deleted).
   `persistence.load()` must never treat a failed download as "empty memory" (it would
   overwrite the real memory on the next save).
4. The app binds to `127.0.0.1` only; ngrok is the only way in; ngrok OAuth allows only
   `ALLOWED_EMAIL`. Fallback: `APP_PASSWORD` login page with lockout.
5. Web content (search results, pages, screenshots) is **untrusted data**, never instructions.
   `pages.py` fetches public URLs only (SSRF guard, also on redirects).
6. Every Ollama request must use the **same `num_ctx`** (`config.LLM_CONTEXT_TOKENS`, 8192):
   a different value makes Ollama reload the whole model (~40 s).

## Architecture (one spoken turn)
```
phone/laptop browser (src/static/index.html, PWA + service worker sw.js)
  tap mic -> record (auto-stop on 1.2 s silence) -> mic released (so replies use the
  loudspeaker / Bluetooth media, not the earpiece)
  [optional: POST /api/screen with one JPEG snapshot of the shared screen]
  POST /api/turn (audio) -> NDJSON stream back
      |
ngrok (fixed domain, Google OAuth allowlist)  ->  FastAPI on Kaggle (src/app.py)
  stt.py      faster-whisper large-v3-turbo (GPU1), en/hi only (Urdu -> Hindi)
  intents.py  "remember that ..." / "forget that ..." (English/Hindi/Hinglish); yes/no for
              actions waiting for a spoken confirmation (tools.pending, 2-min window)
  memory.py   recall(): facts (health/diet/assistant + "remember" always; preference/pattern
              next, capped at 50) + related turns by HYBRID search: FTS5 keywords (TEMP
              tables, rebuilt at load, never saved) + e5 vectors (cached matrix), merged by
              RRF + recency boost; query_hints.py = date ranges/categories/recall intent;
              period questions get digest.py day/week/month summaries first
  llm.py      Ollama qwen3.5:9b (GPU0), think=false, streamed, tools (last round has none):
              web_search / find_video / find_prices (websearch.py, pages.py),
              add/list/complete/delete_reminder (reminders.py),
              device_status / control_device / look_at_camera (home.py, only with HA_URL)
  tts.py      Kokoro (en af_heart, hi hf_alpha) -> MP3 per sentence
  learn.py    after the reply: extract lasting facts from the USER's words (JSON schema call);
              watchdog "back-learns" old turns (turns.learned = 0) while the user is idle
              + learn.reflect(): every few minutes while idle and at session end, stores
              "pattern" facts (focus, recurring interests, habits, how they like help)
  UI          fixed header top, chat scrolls, fixed bottom bar [🤫 Whisper] (mic) [▶ Start];
              🖥 screen snapshot (desktop only), Clear (UI only), Good/Bad text buttons under the
              LATEST reply only (owner: no emoji in the conversation; `app.no_emoji` strips
              any the model writes, before showing, speaking and saving). The owner asked for
              NO memory button: memory is automatic (/api/facts endpoints still exist, unused)
  events: transcript, sentence(+audio), links(cards), done(turn_id for 👍/👎); after the
  reply: free-limit notices, then due reminders ("Reminder: ..."), once per session
```
Background: `Assistant.watchdog` ticks every 5 s: usage clock, budget, idle close, autosave
(10 min), Ollama health; while idle (and no learn call running): back-learn one old turn, else
write one digest, else reflect (every 5 min). Closing = goodbye audio, save, exit, which ends
the Kaggle run (frees GPU). Saves also upload `usage.json.enc` (numbers only) for the Start
button's limit check.

**Home PC (`PVA_HOSTING=home`, `scripts/run_home.py`):** `setup_home.py` instead of
`setup_env.py`, `LocalMemoryStore` (encrypted file in `~/.jarvis`), no GPU meters, no idle
close, ngrok optional (without it: 127.0.0.1 only), CPU fallback (qwen3:4b, Whisper small
int8), and `local_voice.py`: "Hey Jarvis" (openWakeWord) on the PC's mic and speakers.

## Files
| Path | What |
|---|---|
| `src/main.py` | Start-up: secrets, memory load, **budget check before GPU setup**, parallel model loading, server + tunnel. Records GPU time even if start-up crashes. |
| `src/setup_env.py` | Installs packages (cached wheels), ngrok, Ollama (from cache: copies libs, restores exec bits), starts Ollama, fine-tuned model loading. |
| `src/config.py` | All settings + `get_secret()` (env, then the Start button's encrypted bundle, then Kaggle Secrets). `ABOUT_ASSISTANT` = true facts about itself. |
| `src/app.py` | Assistant (turn flow, status, save, close, watchdog) + FastAPI routes. |
| `src/budget.py`, `src/timer.py` | Free-limit meters and the session clock. |
| `src/memory.py`, `src/crypto.py`, `src/persistence.py` | Encrypted memory, hybrid search; Kaggle dataset storage (`MemoryStore`) and home file storage (`LocalMemoryStore`). |
| `src/query_hints.py`, `src/digest.py` | Date/category/keyword rules for recall; day/week/month summaries. |
| `src/learn.py`, `src/intents.py` | Automatic fact learning; remember/forget commands; spoken yes/no. |
| `src/reminders.py` | Reminders, to-dos, bills + payment log, optional ntfy push. |
| `src/home.py` | Home Assistant devices/cameras from an allowlist (`HOME_DEVICES`). |
| `src/setup_home.py`, `src/local_voice.py` | Home-PC start-up; wake word loop. |
| `src/bridge.py`, `home_bridge/` | Home helper link: WebSocket `/api/bridge/ws` (BRIDGE_KEY; the only path that skips ngrok OAuth, see `tunnel.traffic_policy(bridge_path=True)`), tools `appliance_status` / `appliance_control` (always spoken yes); ntfy "up" wake-up on a topic derived from the key. `home_bridge/samsung_bridge.py` runs on the owner's Android phone (Termux): Samsung washer over CoAP-DTLS with `smartthings-local` (`/operational/state/vs/0`, `x.com.samsung.da.state` Run/Pause/Ready, `/remotectrl/0`), finish notification via Termux:API. |
| `src/websearch.py`, `src/pages.py` | Search tools, link cards, previews, prices (ddgs, no API keys). |
| `src/llm.py`, `src/tools.py` | Prompt, streaming, tool loop, model A/B picker; tool registry: `requires_confirmation`/`confirm_if` park the call as `tools.pending` until a rule-checked spoken yes; `attach_image` for camera snapshots. |
| `src/stt.py`, `src/tts.py`, `src/tunnel.py` | Speech in/out; ngrok with OAuth traffic policy. |
| `src/tailnet.py` | Tailscale: on Kaggle downloads the static binaries, `tailscaled --tun=userspace-networking`, `tailscale up --auth-key=TS_AUTHKEY --hostname=jarvis`, `tailscale serve --bg --https=443 http://127.0.0.1:8001`. The node's state dir (identity + cert) is saved as `tailscale.tar.enc` in the memory dataset (`MemoryStore.extras`) so every run is the same device/URL (Let's Encrypt ~5 certs/week per name). The app's second port 8001 is Tailscale-only: middleware requires `Tailscale-User-Login` in ALLOWED_EMAIL/TAILSCALE_USERS, and its traffic isn't counted against ngrok. Started in a background thread after the budget check (parallel to model loading). |
| `src/finetune_data.py` | Phase 5 training data (👍 turns only, private-looking text excluded). |
| `src/static/` | `index.html` (whole UI), `login.html`, `sw.js`, `manifest.json`, icons. |
| `notebooks/` | `assistant.ipynb` (Jarvis), `build_model_cache.ipynb`, `finetune.ipynb` (generated by `scripts/make_finetune_notebook.py`). |
| `scripts/` | `start_jarvis.py` (Start button; refuses before pushing if `usage.json.enc` says a limit is used up), `run_home.py` (home PC), `upload_code.py` (auto-upload), `make_code_zip.py`, `make_key.py`, `make_icons.py`. |
| `.github/workflows/` | `start-jarvis.yml` (manual button), `update-kaggle-code.yml` (on push to `src/`). |

## How it is deployed
- **Code:** `git push` to `main` -> GitHub Action zips `src/` -> new version of the Kaggle
  dataset `assistant-code`. Manual fallback: `python scripts/make_code_zip.py` and upload.
- **Start:** GitHub Actions -> "Start Jarvis" -> `kaggle kernels push` of `notebooks/assistant.ipynb`
  as a new version of `jatinmangla123/jarvis` (GPU T4 x2, internet, inputs: `assistant-code`,
  `jarvis-secrets`, notebook output `build-model-cache`). Or: Kaggle editor -> Save & Run All.
- **Secrets:** Kaggle Secrets (editor runs) *and* GitHub repo secrets (button runs):
  `KAGGLE_USERNAME, KAGGLE_API_TOKEN, MEMORY_KEY, NGROK_AUTHTOKEN, NGROK_DOMAIN, ALLOWED_EMAIL,
  START_URL` (optional `APP_PASSWORD`, `NTFY_TOPIC`, `HA_URL`, `HA_TOKEN`, `HOME_DEVICES`,
  `BRIDGE_KEY`, `TS_AUTHKEY`, `TAILSCALE_USERS`; with `TS_AUTHKEY` the ngrok secrets are
  optional). Button runs get them via a one-time-key encrypted
  private dataset `jarvis-secrets` + `RUN_KEY` injected into the pushed notebook.
- ngrok domain: `volley-gnarly-constable.ngrok-free.dev`. GitHub repo: `JatinMangla/personal-voice-assistant` (private).

## Developing
```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt   (+ ddgs for search tests)
.venv\Scripts\python -m pytest tests          # ~296 tests, no GPU/network needed
node --check src/static/sw.js                  # and the <script> of index.html
```
- Nothing GPU-related can run locally; the owner runs Kaggle and pastes logs. Make logs
  diagnostic (step timings, device, speed) without leaking content.
- Commit messages end with `Co-Authored-By: Claude ...`; push to `main` deploys code.
- Keep `CLAUDE.md` and `docs/PROJECT_LOG.md` updated when something important changes.

## Kaggle / Ollama / ngrok gotchas we hit (don't rediscover them)
1. Kaggle's new API token is a single string (`KAGGLE_API_TOKEN`, often `KGAT_...`); no kaggle.json.
2. Kaggle returns **403** (not 404) for a private dataset that doesn't exist yet: check
   `kaggle datasets list --mine` before calling it "first run".
3. Ollama's installer now needs `zstd` (installed via apt first).
4. Kaggle notebook **outputs lose the executable bit**: after copying Ollama's `lib/ollama`
   from the cache, `chmod -R a+rx` it (else `llama-server: permission denied`, GPU not found).
5. Libraries can't be loaded straight from `/kaggle/input` reliably: copy them to local disk.
6. First model load reads ~6 GB from the input disk: 50–110 s; use a long timeout, load in
   parallel with the speech models. Input-disk speed varies a lot between runs.
7. A fine-tuned **Qwen3.5** GGUF can't load in Ollama (separate vision mmproj): Phase 5 tunes **Qwen3-8B**.
8. Runs started through the Kaggle API (**kernels push**) get **no Kaggle Secrets** (Kaggle
   limitation). Solved with the encrypted `jarvis-secrets` bundle.
9. `ConcurrencyViolation ... Sequence number must match Draft` = the notebook was changed at the
   same time (an open editor tab). Close editor tabs; the start script retries.
10. The kaggle CLI can print an error and still exit 0: check the text.
11. Input mount paths differ (`/kaggle/input/<slug>` vs `/kaggle/input/datasets/<user>/<slug>`,
    `/kaggle/input/notebooks/<user>/<slug>`): always find inputs by a marker file via glob.
12. Windows `Compress-Archive` writes backslash paths: build zips with Python (`make_code_zip.py`).
13. Phones route audio to the **earpiece** while a page holds the mic: release the mic after
    recording and play via `<audio>`.
14. ngrok free shows an interstitial page once; API calls send `ngrok-skip-browser-warning`.
    ngrok offline = `ERR_NGROK_3200`; the PWA service worker serves our page instead.
15. ddgs (search) needs no key; the office network of the dev laptop sometimes blocks
    DuckDuckGo DNS: that's local, Kaggle is fine.
16. faster-whisper often labels Hindi as Urdu: restrict to en/hi and map ur -> hi.
17. `/kaggle/input` (model cache) read speed varies wildly: the same run took 275 s to copy
    Ollama libs, 175 s for speech models, 320 s for the LLM (vs 33/66/48 s on a good day).
    The internet is fast and steady, so `config.DOWNLOAD_FIRST = True`: download Ollama, the
    LLM and the speech models (the last two in parallel); the cache is only the fallback.
18. Opening the mic fresh for every question (needed for loudspeaker playback) restarts the
    browser's automatic gain, so speech starts quiet: lower silence thresholds in the page,
    `stt.normalize()` boosts soft audio (≤ 30x), and **Whisper mode** (🤫 button, `X-Quiet: 1`)
    turns off browser noise suppression and uses a softer Silero VAD.
20. The Start button once went green but started nothing: `kaggle kernels status` said
    RUNNING (an open editor session or a stuck older run counts), so the script skipped the
    push. Now it also checks the ngrok link (offline = `ngrok-error-code` header) and fails
    red with advice; the workflow has a `force` checkbox. `main._startup_guard` stops any run
    not ready within `STARTUP_TIMEOUT_MIN` (20) so a hang can't burn GPU hours.
25. Jarvis knows its own limits: `budget.usage_summary()` (GPU min left today, reset time
    in IST, 7-day hours, link usage, 10-min idle rule) goes into the dynamic tail of every
    system prompt, so "how much time is left today?" is answered from live numbers.
    `describe_reset()` adds the reset time (midnight IST / 1st of month) to every warning.
24. UI shell: `html, body { overflow: hidden }` + `main { position: fixed; inset: 0 }`, and
    only `#log` scrolls; `100vh`/`100dvh` caused a body scrollbar on some phones/PWA.
    Breakpoints: <=360px (small phones), max-height 520px (landscape: limits hidden),
    >=768px (tablet/laptop). The in-app start link is labelled "▶ Start" (owner's wish).
21. Earpiece again "sometimes": `getUserMedia` with `echoCancellation: true` puts phones in
    call mode (earpiece, Bluetooth HFP). Jarvis never listens while speaking, so AEC is off;
    playback waits 600 ms after the mic is released. Media audio then follows the phone:
    loudspeaker, or wired/Bluetooth headphones when connected.
22. `InvalidDataError` on some recordings (phone-side, mid-session): `stt.decode()` retries
    via a temp file named by magic bytes; still unreadable -> `AudioUnreadable` -> page says
    "tap again" (no spoken error); log shows size/type only.
23. Speed: the system prompt is ordered static -> facts -> language/time/related so Ollama
    reuses the cached prefix; `OLLAMA_NUM_PARALLEL=2` so the learning call doesn't evict the
    chat's cache. Start-up: the LLM downloads+loads+warms in a background job while the
    speech models download and load (first answer after load took ~47 s = GPU init).
26. Devices (researched 2026-09-29): **Samsung SmartThings API becomes paid ($4.99/month
    "Personal Plan") from October 2026** (Home Assistant docs), so the Samsung washer can't
    be connected for ₹0 through SmartThings. **Free local routes exist** (community,
    reverse-engineered, MIT): Method 1 = HTTPS on TCP 8888 (~2018-2022 models; client cert
    signed with Samsung's publicly known AC14K_M CA + token the appliance posts back to port
    8889; github.com/perseus177/ha-samsung-washer-local; start/pause/stop, programme,
    remaining time); Method 2 = CoAP-DTLS on UDP 49152-49160 (newer, Tizen RT 3.x/DAWIT
    3.0+; github.com/QuiteYellow/SmartThings-Local Python lib, HA: mbillow/localthings).
    Both need a device on the home Wi-Fi (Kaggle can't reach the LAN).
    `scripts/find_samsung.py` tells which method an appliance speaks (OCF discovery 5683,
    DTLS 5684/4915x, TCP 8888). Don't run it on the office network. Owner's washer:
    **WW12DG6B24ASTL** (2024 DG; the SmartThings-Local tested list has a WW11DG). First scan
    (old scanner without 5683/5684) found nothing; rescan pending, then setup_cert.py --test. **Qubo (Hero Electronix) has no Home Assistant integration**
    (closed cloud; Alexa/Google only); free routes = a local smart plug (on/off, if the
    purifier resumes after power returns) or an IR blaster if it has a remote. Owner has
    not said they have Home Assistant or a home machine for it yet.
27. (2026-09-29, suspected, confirm on the next run) Two Kaggle runs were **cancelled**
    (`KernelWorkerStatus.CANCEL_ACKNOWLEDGED`) ~45 s in, both while pip installed
    `websockets`; runs that never installed it were fine. Never pip-install `websockets` on
    Kaggle; uvicorn gets `wsproto` instead (only with BRIDGE_KEY). The **Jarvis log** button
    (`.github/workflows/jarvis-log.yml`, `gh workflow run "Jarvis log"`) shows the status and
    log of the last run with seconds since start. Tailscale itself started fine (4 s). A
    cancelled first login left machines `jarvis` and `jarvis-1` in the tailnet: the owner
    must delete old ones so the name `jarvis` is free; new logins are now saved at once.
19. Memory safety: if a start treated an existing memory as "first run" and then `datasets
    create` answers "already exists", `MemoryStore` sets `blocked` and never saves (an empty
    memory must never replace the real one).

## Status (2026-09-28, night 2): FUTURE_PLANS worked through
- Built and unit-tested (296 tests), **not yet run on Kaggle or a device**: hybrid memory
  search, spoken-yes confirmation, reminders/bills (+ ntfy), Start-button limit check,
  digests, home-PC mode, wake word, Home Assistant devices/cameras. Audit fixes: tool loop
  always answers, preference facts capped, forget removes copies, no overlapping learn calls.
- Deliberately NOT built: split storage and float16 vectors (trigger: the save log line
  `[memory] saved ... (x MB, n turns ...)` says "time to split storage" at 50 MB / 20k turns).
- Flash attention is a switch (`OLLAMA_FLASH_ATTENTION`, off): on a T4 it may be slower;
  compare the `[turn]` lines. The speed report now shows `model load Xs` to explain the 47 s.
- Next: see docs/FUTURE_PLANS.md, "What to check on the next run".

## Status (2026-09-28, later)
- Owner reported: (a) "Share screen" missing on the phone PWA: expected, phone browsers have
  no screen capture; (b) Jarvis said it only remembers the current chat: memory WAS loaded
  (`[memory] loaded`), but facts told before automatic learning existed were never extracted:
  fixed with back-learning + honest prompt + 🧠 Memory panel; (c) had to speak louder since the
  loudspeaker fix: fixed (see gotcha 18) + Whisper mode; (d) an 11-minute start: see gotcha 17.

## Status (2026-09-28)
- **Verified on Kaggle by the owner:** start-up (~148 s ready), GPU use, voice loop (first audio
  2–3 s, 25–28 tokens/s), web search, memory save/load, idle close, automatic code upload.
- **Built, not yet confirmed on a real device:** GitHub Start button with encrypted secrets
  bundle, PWA install + offline Start button, loudspeaker/Bluetooth fix, link cards, YouTube
  app deep link, price search, screen sharing, fact learning incl. corrections, shorter answers.
- **Pending owner actions:** add `MEMORY_KEY` etc. as GitHub repo secrets (they had to recover
  `MEMORY_KEY` from Kaggle Secrets via a throwaway notebook); test the above on phone/laptop.
- **Later / ideas:** move to a home PC (removes cloud + tunnel exposure), home devices via
  Home Assistant tools with spoken confirmation, Phase 5 fine-tune after 500+ 👍 replies,
  rebuild the model cache to include `ddgs` (saves ~17 s per start).
