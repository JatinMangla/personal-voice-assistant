# Project log: Personal Voice Assistant ("Jarvis")

Chronological record of requests, decisions, observations, problems, fixes and outcomes.
Newest entries at the bottom. The short handover is [CLAUDE.md](../CLAUDE.md).

---

## 2026-09-27: Plan review and first build

**Request:** review the owner's plan (`personal-voice-assistant-plan.md`: Kaggle + Gradio share
link + Whisper + Qwen3-8B + Kokoro + plain SQLite memory), find weak points, build the best
version. Priorities: security ("not hackable") and speed; the owner wanted "data kept inside
the LLM model".

**Weak points found** (full table in [PLAN.md](PLAN.md)):
- A public Gradio share link with brute-forceable basic auth.
- Plain-text memory in a Kaggle dataset, and old versions kept forever, so "forget" didn't forget.
- Transcripts could end up in notebook logs.
- No 2FA.
- A 5 GB model download on every start.
- No streaming.
- Whisper `small` weak on Hinglish.
- One GPU used out of two.
- No idle shutdown.

**Key explanation given:** training personal data *into model weights* is not more secure. A
model file can be made to repeat its training data, and a fact can't be removed from it.
Encrypted, deletable memory is safer and more accurate. Fine-tuning is for style only.

**Owner decisions:**
- Kaggle only.
- **No Tailscale:** no apps on devices.
- **No passphrase:** the key lives in Kaggle Secrets.
- Tap mic, auto-stop on silence.
- 10-minute idle close.

**Built (Phases 0–4):**
- ngrok fixed domain + Google OAuth email allowlist, with a password page as fallback.
- FastAPI + custom HTML page instead of Gradio, because Gradio's mic can't auto-stop on silence.
- faster-whisper `large-v3-turbo` on GPU1; Ollama on GPU0.
- Streaming LLM output split into sentences, then Kokoro TTS.
- AES-256-GCM memory in RAM (SQLite `serialize`), `secure_delete`.
- Model cache notebook.
- Budget/usage meters.

**Model choice:** `qwen3.5:9b` (newer than `qwen3:8b`, which is the fallback), checked on the
Ollama library at build time.

## Free-of-cost and free-limit guard

**Request:** everything must be free.

**Found:** status polling every 5 s would exceed ngrok's 20,000 requests/month.

**Fixes:**
- MP3 instead of WAV (about 6× smaller).
- Status polling every 60 s, only while visible and online.
- A usage meter on the page.

**Request:** split Kaggle's 30 GPU hours per week evenly over 7 days (≈ 257 min/day), warn at
90 % and stop at 99 %.

**Built (`budget.py`):** four meters (GPU today, GPU over a rolling 7 days, ngrok requests, ngrok
data), plus:
- a spoken warning once, and a page banner;
- auto-stop at 99 %;
- a start-up refusal if a meter is at 99 % or fewer than 5 minutes are left, checked **before**
  the GPU setup;
- a 2-minute overhead counted per session.

## Phase 5: style fine-tuning

- 👍/👎 on replies.
- `finetune.ipynb`: Unsloth QLoRA on **Qwen3-8B**. A tuned Qwen3.5 can't load in Ollama,
  because of its separate vision mmproj.
- Budget-aware training with a stop callback.
- The GGUF is encrypted in chunks with `MEMORY_KEY`.
- The assistant runs a blind A/B test of the base and tuned models, then keeps the winner after
  30 ratings each.
- Only 👍 turns are used, and private-looking turns (numbers, emails, "password", "OTP") are excluded.

## First Kaggle runs: problems and fixes (in order)

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | No `kaggle.json` | Kaggle's new API tokens are one string | Support `KAGGLE_API_TOKEN` (`config.kaggle_credentials`) |
| 2 | Cache notebook failed instantly | Ollama installer needs `zstd` | `apt-get install zstd` first; internet check cell |
| 3 | `403 Forbidden` on first run | Kaggle answers 403 for a missing private dataset | Confirm via `datasets list --mine`; bad token still reported |
| 4 | `cryptography` conflicts | `pip -U cryptography` broke Kaggle's pins | Only `-U kaggle`; don't upgrade cryptography |
| 5 | Ollama 500 on first chat | Libraries symlinked from read-only input | Copy `lib/ollama` to local disk |
| 6 | `llama-server: permission denied`, CPU only | Notebook output loses the exec bit | `chmod -R a+rx /usr/local/lib/ollama`; reinstall fallback |
| 7 | Read timeout (120 s) | First model load reads ~6 GB | Separate `load_model` with a 10-min timeout; log device/speed |
| 8 | Failed starts not counted | Crash before the first save | `main.run` records usage on crash |

**Measurements after the fixes** (Kaggle T4 x2):

| What | Result |
|---|---|
| Ready time | 358 s, then 222 s (parallel loading), then **148 s** (skip `cuda_v13` copy) |
| Setup steps | packages 19 s, ngrok 1 s, Ollama install 33 s, start 7 s |
| Model loading | LLM load 48–114 s (input disk speed varies); speech models 66–92 s |
| LLM speed | 25–28 tokens/s; first test reply 0.6 s after load (the earlier 45 s was one-time GPU init) |
| Turn latency | heard 1.1 s, **first audio 2.0–3.3 s**, search turns ≈ 6 s |
| Session end | idle close, autosave, and exit all confirmed |

## Features added after testing

**Internet search:**
- The ddgs package, with no API key. Google first, then Bing, Brave and DuckDuckGo, then Wikipedia.
- Explained why not a Google API (it needs keys) and why not Vercel (it can't reach inside Kaggle
  or run a GPU model, and a tunnel is needed anyway).

**Learning from conversation (`learn.py`):**
- After each turn, a JSON-schema LLM call extracts lasting facts from the **user's** words.
- Categories: health, diet, person, place, work, preference, routine, assistant, other.
- Health, diet and assistant-corrections, plus explicit "remember" facts, go into **every** prompt,
  so e.g. a peanut allergy triggers a warning on recipes months later.
- Changed facts replace old ones; duplicates are skipped; private-looking text is never learned.
- A "🧠 Learned" note shows on the page.

**Links:**
- `find_video` tool; links collected per turn and sent as a `links` event.
- URLs stripped from speech.

**Restarting after idle close:**
- One-tap **Start Jarvis** GitHub Action (`kernels push`) that attaches the latest code and cache,
  does nothing if Jarvis is already running, and retries on `ConcurrencyViolation`.
- **Auto-upload** of `src/` to Kaggle on every `git push`.

**Audio (earpiece bug):** phones switched to call mode because the page held the mic. Now:
- the mic is released after each recording;
- replies play through `<audio>`;
- iOS gets `navigator.audioSession` hints.

This should also fix Bluetooth call-quality audio.

**PWA:**
- Manifest, icons (drawn with plain Python), and a service worker.
- When ngrok says offline (3200), the cached page shows "Jarvis is off" with a **▶ Start Jarvis**
  button (`START_URL` secret).

**Self-knowledge:**
- The assistant claimed "we can talk without internet". `config.ABOUT_ASSISTANT` now states the
  truth (cloud GPU, internet required, 10-min idle close, no offline mode), with an "admit
  uncertainty" rule.
- The user's corrections are learned as the `assistant` category.

**Secrets on button runs:**
- Kaggle gives no Kaggle Secrets to API-started runs, which is a documented limitation.
- The Start script now encrypts all secrets with a fresh one-time key and uploads them as the
  private dataset `jarvis-secrets` (old versions deleted).
- It injects the key into the pushed notebook (`RUN_KEY`). Neither part alone reveals anything.

**Other small fixes:**
- Notebook cells got `id`s (nbformat warning).
- The code can be found whether Kaggle unpacks the zip or not.

**Incident:** the owner didn't have `MEMORY_KEY` saved outside Kaggle. It was recovered with a
throwaway notebook that printed it once, followed by clearing the output and deleting the
notebook. Lesson: always keep a copy in a password manager.

## 2026-09-28: Rich links, prices, screen sharing, UI

**Requests:**
- Screen sharing.
- Song requests that open the YouTube app (else the browser).
- Price search with the cheapest first.
- WhatsApp-style link previews.
- Shorter answers.
- A Clear chat button (UI only).
- This log and CLAUDE.md.

**Built:**
- `pages.py`:
  - Safe fetching: public addresses only (checked on every redirect), at most 400 KB per page.
  - Previews read from `og:*`, `twitter:*`, `<title>`, and prices from JSON-LD `offers`, meta
    tags, or ₹/Rs/INR text.
  - Checked live on real pages.
- **Link cards:** thumbnail, site, title, snippet, green price badge. The first video is a large
  card with **▶ Play on YouTube**:
  - Android: `intent://…;package=com.google.android.youtube;S.browser_fallback_url=…`
    (the app if installed, else the browser);
  - iPhone: `youtube://` with a 1.5 s browser fallback;
  - desktop: a new tab.

  Web pages can't list installed apps, so the phone decides.
- `find_video(latest=True)` uses the "last month" filter for "latest song".
- **`find_prices`:** searches shops, reads prices from up to 6 pages in parallel, sorts cheapest
  first, returns cards. Some shops block bots; snippet prices are the fallback.
- **Screen sharing** (laptop/desktop only; phone browsers have no `getDisplayMedia`):
  - "🖥 Share screen" button with a small preview.
  - Each question sends **one** JPEG snapshot (≤ 1280 px) to `/api/screen`.
  - It's used once as an image input for Qwen3.5 (vision) and never stored or logged.
  - It's a snapshot rather than continuous video: continuous analysis would burn GPU and ngrok quota.
- **Shorter answers:** the system prompt now asks for as few words as possible (usually one
  sentence under 20 words) unless details are requested.
- **Clear** button: empties the chat on the page only; memory untouched.
- `num_ctx` fixed at 8192 for **every** Ollama call (load, chat, learning): a differing value
  makes Ollama reload the model.

**Status:** 137 local tests pass, and the page JavaScript passes `node --check`. These features
are not yet tried on the owner's devices.

## 2026-09-28 (later): Memory honesty, back-learning, Whisper mode, faster start

**Owner reports and findings:**

1. **"Share screen" only on the laptop.** Expected: Android and iOS browsers, including
   installed PWAs, have no screen capture API.
2. **"Jarvis said it only remembers things from the chat; it didn't know about me."**
   - The log showed `[memory] loaded`, so no data was lost.
   - The facts the owner told it were said **before** automatic learning existed, so they
     sat only in the conversation history. "What do you know about me?" rarely matches that
     history closely enough, the fact list was empty, and the model then falsely claimed it
     can't remember.
   - Fixes:
     - The prompt states that it DOES remember, and to say "I haven't learned anything
       yet" when the fact list is empty.
     - **Back-learning:** `turns.learned` column; while the user is idle ≥ 15 s, the watchdog
       reads one old turn per 5 s for facts.
     - **🧠 Memory panel:** lists every fact with delete buttons.
     - Fact limit raised to 50.
3. **Saving risk found in review.** If a start ever misjudged an existing memory as "first
   run", `datasets create` would fail with "already exists", and the old code then switched to
   `version --delete-old-versions`, replacing the real memory with an empty one.
   - Now `MemoryStore.blocked` refuses all saves in that case.
   - The existence check asks twice (list + status).
4. **"I have to speak louder lately."** Caused by the loudspeaker fix: the mic opens fresh for
   every question, so the browser's automatic gain starts low. Fixes:
   - Lower speech thresholds in the page (0.008 / noise × 2.2, down from 0.015 / × 2.5).
   - `stt.normalize()` boosts soft audio up to 30×.
   - New **🤫 Whisper mode**: browser noise suppression off, lower threshold, 1.6 s pause,
     Silero VAD threshold 0.25.
5. **11-minute start** (`ollama install 275s`, speech 175 s, LLM load 320 s). `/kaggle/input` is
   sometimes very slow. `DOWNLOAD_FIRST`: Ollama from the internet (≈ 20 s measured earlier),
   and the LLM pull + Hugging Face downloads run in parallel. The cache is only a fallback.

**Status:** 145 tests pass. Waiting for the next Kaggle log to confirm the new start-up times.

## Open items / next steps
- The owner adds the remaining GitHub repo secrets, then tests the Start button end to end.
- On a real phone, confirm: loudspeaker/Bluetooth playback, PWA install and offline Start button,
  YouTube app deep link, price cards.
- On a laptop, try screen sharing.
- Optional: rebuild the model cache so it includes `ddgs`.
- Later: home PC hosting, Home Assistant tools (with spoken confirmation), Phase 5 fine-tune.
