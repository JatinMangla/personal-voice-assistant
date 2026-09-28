# CLAUDE.md: Personal Voice Assistant ("Jarvis")

Read this first. It is the handover for any Claude Code session working on this repo.
The full history (every problem, fix, measurement) is in [docs/PROJECT_LOG.md](docs/PROJECT_LOG.md);
the user-facing guide is [README.md](README.md); the original plan review is [docs/PLAN.md](docs/PLAN.md).

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
- They rejected: Tailscale (installing apps on devices), a passphrase at session start.
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
  intents.py  "remember that ..." / "forget that ..." (English/Hindi/Hinglish)
  memory.py   facts (health/diet/user/assistant-corrections always in prompt) + related turns
  llm.py      Ollama qwen3.5:9b (GPU0), think=false, streamed, tools:
              web_search / find_video / find_prices (websearch.py, pages.py)
  tts.py      Kokoro (en af_heart, hi hf_alpha) -> MP3 per sentence
  learn.py    after the reply: extract lasting facts from the USER's words (JSON schema call);
              watchdog "back-learns" old turns (turns.learned = 0) while the user is idle
  UI extras   🧠 Memory panel (GET /api/facts, POST /api/facts/delete), 🤫 Whisper mode,
              🖥 screen snapshot (desktop only), Clear (UI only), 👍/👎
  events: transcript, sentence(+audio), links(cards), learned, done(turn_id for 👍/👎)
```
Background: `Assistant.watchdog` ticks every 5 s: usage clock, budget, idle close, autosave
(10 min), Ollama health. Closing = goodbye audio, save, exit, which ends the Kaggle run (frees GPU).

## Files
| Path | What |
|---|---|
| `src/main.py` | Start-up: secrets, memory load, **budget check before GPU setup**, parallel model loading, server + tunnel. Records GPU time even if start-up crashes. |
| `src/setup_env.py` | Installs packages (cached wheels), ngrok, Ollama (from cache: copies libs, restores exec bits), starts Ollama, fine-tuned model loading. |
| `src/config.py` | All settings + `get_secret()` (env, then the Start button's encrypted bundle, then Kaggle Secrets). `ABOUT_ASSISTANT` = true facts about itself. |
| `src/app.py` | Assistant (turn flow, status, save, close, watchdog) + FastAPI routes. |
| `src/budget.py`, `src/timer.py` | Free-limit meters and the session clock. |
| `src/memory.py`, `src/crypto.py`, `src/persistence.py` | Encrypted memory and Kaggle dataset storage. |
| `src/learn.py`, `src/intents.py` | Automatic fact learning; remember/forget commands. |
| `src/websearch.py`, `src/pages.py` | Search tools, link cards, previews, prices (ddgs, no API keys). |
| `src/llm.py`, `src/tools.py` | Prompt, streaming, tool loop, model A/B picker; tool registry (future home devices need `requires_confirmation`). |
| `src/stt.py`, `src/tts.py`, `src/tunnel.py` | Speech in/out; ngrok with OAuth traffic policy. |
| `src/finetune_data.py` | Phase 5 training data (👍 turns only, private-looking text excluded). |
| `src/static/` | `index.html` (whole UI), `login.html`, `sw.js`, `manifest.json`, icons. |
| `notebooks/` | `assistant.ipynb` (Jarvis), `build_model_cache.ipynb`, `finetune.ipynb` (generated by `scripts/make_finetune_notebook.py`). |
| `scripts/` | `start_jarvis.py` (Start button), `upload_code.py` (auto-upload), `make_code_zip.py`, `make_key.py`, `make_icons.py`. |
| `.github/workflows/` | `start-jarvis.yml` (manual button), `update-kaggle-code.yml` (on push to `src/`). |

## How it is deployed
- **Code:** `git push` to `main` -> GitHub Action zips `src/` -> new version of the Kaggle
  dataset `assistant-code`. Manual fallback: `python scripts/make_code_zip.py` and upload.
- **Start:** GitHub Actions -> "Start Jarvis" -> `kaggle kernels push` of `notebooks/assistant.ipynb`
  as a new version of `jatinmangla123/jarvis` (GPU T4 x2, internet, inputs: `assistant-code`,
  `jarvis-secrets`, notebook output `build-model-cache`). Or: Kaggle editor -> Save & Run All.
- **Secrets:** Kaggle Secrets (editor runs) *and* GitHub repo secrets (button runs):
  `KAGGLE_USERNAME, KAGGLE_API_TOKEN, MEMORY_KEY, NGROK_AUTHTOKEN, NGROK_DOMAIN, ALLOWED_EMAIL,
  START_URL` (optional `APP_PASSWORD`). Button runs get them via a one-time-key encrypted
  private dataset `jarvis-secrets` + `RUN_KEY` injected into the pushed notebook.
- ngrok domain: `volley-gnarly-constable.ngrok-free.dev`. GitHub repo: `JatinMangla/personal-voice-assistant` (private).

## Developing
```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt   (+ ddgs for search tests)
.venv\Scripts\python -m pytest tests          # ~137 tests, no GPU/network needed
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
19. Memory safety: if a start treated an existing memory as "first run" and then `datasets
    create` answers "already exists", `MemoryStore` sets `blocked` and never saves (an empty
    memory must never replace the real one).

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
