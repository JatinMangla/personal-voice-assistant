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

## 2026-09-28 (evening): Automatic memory, speaker fix, UI layout

**Owner log** (download-first start):

| What | Result |
|---|---|
| Downloads | LLM 63 s, speech 67 s (in parallel) |
| LLM start-up | load 62 s, first answer 47 s (GPU init) |
| Ready | 309 s |
| Turns | first audio 3–4.7 s, prompts ~1,600 tokens |
| Errors | 9 × `InvalidDataError` (unreadable recordings) near the end |

**Owner requests:**
- No Memory button. The assistant itself should decide what to store: details, preferences,
  settings, decisions, actions, behaviour, what they focus on.
- The speaker still sometimes goes to the earpiece. With wired/Bluetooth headphones sound
  should go there, otherwise to the loudspeaker.
- UI: header fixed at top, mic fixed at bottom, Whisper beside the mic.

**Built:**
- Memory button and panel removed.
- `EXTRACT_PROMPT` widened: decisions, events with dates, goals, habits, work focus,
  wanted settings. Relative dates are resolved using today's date.
- New `learn.reflect()` for patterns over recent messages (`turns.reflected`), run while
  idle (every 5 min at most) and at session end.
- Always-shown categories: health, diet, assistant, preference, pattern.
- Speaker: `echoCancellation: false` (the root cause of call-mode routing), plus a 600 ms gap
  before playback.
- Robust audio decode, "tap again" for bad recordings, and a tiny-blob check on the page.
- Speed: prompt ordering for cache reuse, `OLLAMA_NUM_PARALLEL=2`, LLM
  download + load + warm-up as a background job during speech model loading.
- UI: fixed top and bottom bars, a smaller mic, Whisper to the left of the mic.

**Bug found by the new tests:** the privacy filter treated any 4+ digit number as private,
so every fact containing a year ("bought a car in September 2026") was silently dropped.
Dates (years, ISO and dd/mm/yyyy) are now allowed. PINs, cards, and phone numbers stay
blocked, even when they contain a year-like group.

**Tool note:** the command-approval service of the Claude Code session was down for a
while. The owner ran the tests and pushed themselves (163 passed, 1 skipped).

**Also this evening:**
- Start button green but nothing started: fixed. It now checks the link, turns red with
  advice, and has a `force` box.
- 20-minute start-up guard, and download timeouts.

## 2026-09-28 (night): Limits awareness, memory research

- Every warning, goodbye and start-up refusal now includes the **reset time in India time**:
  day at midnight IST, week as the oldest day drops out, link limits on the 1st of the month.
- Jarvis gets a live **usage summary** in every prompt, so it can answer "how much GPU time is left
  today?".
- The owner asked whether exceeding GPU time creates a bill. **No**: there is no payment method
  anywhere, Kaggle has no paid option, and the ngrok and GitHub free tiers just stop.
- **Research:** vectorless RAG vs ripgrep for growing memory, written up in
  [MEMORY_RESEARCH.md](MEMORY_RESEARCH.md).
  - Today's method: SQLite + multilingual vectors (brute force) + structured facts.
  - Recommendation: a hybrid (FTS5/BM25 + vectors + RRF + date/category filters). Later, a
    time-summary tree (the useful idea from vectorless RAG) and split hot/archive storage.
  - ripgrep was rejected: it needs plaintext files, exact words only, and slow multi-round search.
  - PageIndex was rejected as the main method (it suits long structured documents), but its
    tree idea is borrowed for time summaries.

## 2026-09-28 (night 2): FUTURE_PLANS worked through

The owner asked to analyse the whole project against FUTURE_PLANS.md, fix weak points,
then build every plan item. Step 0 (checks on a real device) needs the owner; it was done
as a code audit instead, and the device checks moved to the next run.

**Audit findings, fixed:**
- Risky tools could never run: `requires_confirmation` refused, but nothing ran the action
  after a "yes". Now the call is parked (`tools.pending`), the model asks "Should I …?", and
  only the user's next words, if a plain yes (fixed rules in `intents.parse_confirmation`,
  English/Hindi/Hinglish, 2-minute window), run it. The model or a web page can't confirm.
- The tool loop could end without an answer after 3 tool rounds; the last round now has no
  tools.
- `preference`/`pattern` facts were always in the prompt with no cap: hundreds of them
  would slow every reply and could overflow the 8k context. Now only health/diet/assistant
  and "remember" facts are uncapped; preferences/patterns come next within the 50-fact limit.
- "Forget" deleted only the single best fact, so an auto-learned copy survived; near-copies
  (similarity ≥ 0.93 to it) go too.
- Facts were listed twice in the prompt (facts section + related); now once.
- Back-learning could overlap the post-turn learning call.
- No size in the save log (needed for the scaling trigger); no model load time in the speed
  report (needed to explain the 47 s first answer); the page let the browser choose the
  recording format; no `nosniff` header. All added.

**Built (plan step 1, hybrid memory):** FTS5 keyword index with a Devanagari-safe tokenizer
(`categories 'L* N* Co M*'`, else vowel signs split Hindi words) in TEMP tables: rebuilt at
load (0.08 s for 15k turns), never saved, so no trace of forgotten text in the upload.
BM25 + vector rankings merged by RRF (k = 60) with a small recency boost; date ranges,
categories and "recall" intent from cheap rules (`query_hints.py`); words found in >10 % of
rows are ignored. Measured on 15k synthetic turns: ~20 ms per recall.

**Built (backlog):**
- Start button checks the limits before pushing: every save also uploads `usage.json.enc`
  (numbers only, encrypted); the script downloads just that file (`-f`), never the memory.
- Reminders, to-dos and bills (payment log, 3-month totals) as tools; due ones are spoken
  after the first reply and shown as notes; optional ntfy.sh scheduled push (generic text).
- Flash attention: research showed Ollama enables it itself only on Ampere+, and it can be
  slower on older GPUs, so it is a switch, off by default, to be A/B-tested with the new
  load-time numbers.
- Rebuilding the model cache with `ddgs`: the notebook already lists it; the owner re-runs it.

**Built (roadmap):** A home-PC mode (`PVA_HOSTING=home`), B "Hey Jarvis" (openWakeWord,
verified against the real package), C Home Assistant devices from an allowlist with spoken
confirmation, D camera snapshots to the vision model, E bills (in reminders). Plan section 2:
day/week/month digests (additive, for period questions); split storage and float16 were left
for their trigger on purpose (riskiest change to memory, not needed yet).

**Status:** 296 tests pass (1 skipped), page JS passes `node --check`. Nothing of this has
run on Kaggle or a device yet.

## 2026-09-29/30: Samsung washer helper, Tailscale-only

- Owner asked for free Samsung appliance control. SmartThings API is paid from Oct 2026;
  built the phone home helper (`home_bridge/`, CoAP-DTLS via smartthings-local, Termux) and
  `scripts/find_samsung.py`. First scan found nothing: the scanner missed UDP 5683/5684
  (fixed). Owner's washer: WW12DG6B24ASTL. Waiting for their rescan + `setup_cert.py --test`.
- Owner asked about Tailscale speed: on Kaggle ≈ same (network is 0.2–0.5 s of a 2–3 s
  reply; relay vs direct unknown until a run), clearly faster only at home; the real gains
  are privacy (end-to-end, no public address) and no ngrok limits. They already use the
  Tailscale app + Termux, so they asked for it: `src/tailnet.py` (userspace tailscaled,
  `tailscale serve`, identity saved encrypted next to the memory, second local port with a
  Tailscale-User-Login check). They are the only user, so it is Tailscale-only by default
  (`USE_NGROK = False`), with ngrok as a fallback only if Tailscale fails.
- Chat: no emoji (owner's wish), Good/Bad text buttons on the latest reply only.
- First Kaggle runs with Tailscale: it came up in 2-4 s every time, but all 4 runs were then
  killed by Kaggle (CANCEL_ACKNOWLEDGED, no stop signal) ~15-25 s later. Added a "Jarvis
  log" button (reads the Kaggle log from GitHub), a start-up heartbeat and a stop-signal
  message to find it; the websockets suspicion was wrong. The same run with Tailscale off
  started normally. Kaggle's rules forbid circumventing the service, so Tailscale stays
  off on Kaggle (`USE_TAILSCALE = HOME`) and is kept for the home PC.

## 2026-09-30: Android app planned

- The owner wants a native Android app instead of the PWA, distributed and updated via GitHub
  without the Play Console. Research: Google's developer verification is enforced in India
  from 2027 (a free limited-distribution account covers 20 devices; payments profile needed,
  check it asks for no card); Android 12+ allows silent self-updates; private-repo release
  assets need a login, so APKs go to a public releases-only repo. Plan and phases:
  [ANDROID_APP_PLAN.md](ANDROID_APP_PLAN.md).

## 2026-10-01: Android app built (phases 0-7), PWA removed

- Owner confirmed all §3 decisions as recommended (Kotlin/Compose; public releases-only repo;
  plain laptop page + QR pairing with per-device tokens; phone alarms, ntfy kept optional;
  Start button opens the GitHub page; washer helper stays in Termux).
- Phase 0: public repo `JatinMangla/jarvis-app` created; signing key (PKCS12, RSA 4096, 30 y,
  cert SHA-256 `d9c29311…f995`) made with a portable JDK on the laptop; secrets
  `ANDROID_KEYSTORE_B64/_PASSWORD`, `ANDROID_KEY_ALIAS/_PASSWORD`, `APP_DEPLOY_KEY`.
  **Change from the plan:** no fine-grained PAT and no GitHub Releases. CI publishes with an
  SSH **deploy key** that can write to `jarvis-app` only (narrower, no expiry, no owner
  clicks), as one force-pushed commit holding `apk/jarvis-<ver>.apk`, `SHA256SUMS`,
  `latest.json` (the app reads it from raw.githubusercontent.com). `app-v0.0.1` published.
- Local toolchain (no admin): JDK 17, Gradle, Android SDK in
  `%LOCALAPPDATA%\Programs\android-dev`, so the app compiles and its tests run on the laptop.
- Phase 1 (server): `devices.py` (hashed 256-bit tokens, 5-min pairing, revoke, lockout for
  wrong tokens only), app header rule in the ngrok policy, page-only pairing endpoints,
  `api` version 1, pairing QR on the page (vendored qrcode-generator, npm-integrity checked).
- Phases 2-6 (app): pairing via ZXing (no Play services / ML Kit, which has telemetry),
  Keystore-encrypted storage, OGG/Opus recorder with the page's silence rule, ExoPlayer on
  media audio, NDJSON streaming, link cards, ratings, limits, goodbye, local reminder alarms
  (server sends a reminders fingerprint so the app syncs only on change), self-update with
  checksum + signer checks and USER_ACTION_NOT_REQUIRED.
- A JVM contract test drives the app's real HTTP client against the real `app.py`
  (`scripts/app_test_server.py`): pairing, status, streamed turn, links, rating, reminders,
  403/401 all pass. Not yet run on a phone.
- Phase 7: PWA removed (manifest, icons, install tags); `sw.js` is now a kill switch that
  deletes the old cache and unregisters itself; the page unregisters old workers.

- 2026-10-01: the owner installed app 0.1.0 from jarvis-app and paired it: confirms the
  sideload install, QR scan, the ngrok `X-Jarvis-App` rule on the real ngrok, token auth and
  the token rotation on a real phone.

- 2026-10-01 (owner feedback): "play an Arijit song" -> Jarvis said it was playing, but no
  cards; "play music on YouTube" did nothing. Cause: the search tools run only if the model
  calls them, and Qwen sometimes answers without calling. Fix: `quick_search.py` detects
  song/video/price/suggestion requests by rules and searches before the model answers (cards
  arrive first); in the app a "play" request opens the video in YouTube after the reply.
  Also: screen sharing in the app (MediaProjection + bubble), app 0.2.0.
- Same day: prices showed cards, songs still didn't. Most likely cause: Whisper wrote the
  owner's English in Devanagari ("प्ले केसरिया"), which the rules didn't know. Added the
  Devanagari spellings (प्ले, सजेस्ट, सॉन्ग्स, म्यूजिक, प्राइस …) and made video search read
  YouTube's own results page first (ddgs videos as backup; DuckDuckGo often rate-limits).

- 2026-10-09: **Coder helper, step 1** (plan: a Kaggle coding model that cuts Claude Code's
  token use; step 1 needs no Kaggle). `scripts/coder/` = code index (SQLite FTS5 per
  function/class/section, identifiers split so "silence detector" finds `SilenceDetector`),
  search (keywords + symbol names, RRF, scaled by how many query words a chunk holds, tests
  pushed down), answer cache keyed by file hashes (never stale), good/bad ratings, safety
  (allowlist folders, secret files never read). `scripts/coder_mcp.py` serves it to Claude
  Code as MCP tools (`search_code`, `repo_map`, `rate`, `coder_status`, `reindex`), registered in
  `.mcp.json`. MCP is version 2.x (`mcp.server.mcpserver.MCPServer`, not `FastMCP`). This repo:
  134 files / 1.5k chunks index in ~1 s, searches take ~3 ms. 37 tests in `tests/test_coder.py`.
- 2026-10-09 (same day): **Coder helper, steps 2-3** built. Kaggle side: `src/coder_main.py` (budget
  check, refuses while Jarvis runs, Ollama on both GPUs + KV cache q8_0 + flash attention, 30B with 14B
  fallback, alias `jarvis-coder`, speed probe in the log, reranker, usage recorder, idle/3 h/limit stop),
  `coder_proxy.py` (Bearer key, 3 paths, forced model/context/reply cap, `/rerank`), `coder_rerank.py`,
  `tunnel.coder_traffic_policy`, `notebooks/coder.ipynb`, `scripts/start_coder.py`, `start-coder.yml`;
  Start Jarvis now also refuses while the Coder runs; the log button takes a `notebook` input.
  Laptop side: `client`, `tasks`, `selfcheck`, vectors in the index, reranked deep search, `ask_repo`,
  `summarize` (offline reducer), `draft_code`, idle pre-compute. 527 tests (client <-> proxy integration
  test included). Found while testing: ngrok counts every request, so the proxy now counts all of them.
  Not run on Kaggle yet.

- 2026-10-09 (first Kaggle runs of the Coder helper): **it works.** Run 1: ready in 393 s
  (`qwen3-coder:30b` download 173 s, load 120 s), reading ~1242 tok/s, writing ~66 tok/s on both
  T4s, reranker on cuda:1 in 35 s, 20-min idle stop fired, GPU time recorded correctly. Three
  real bugs the run exposed, all fixed and pushed: (1) the correct key was refused (429) because
  `gh secret set` through a PowerShell pipe prepends a BOM -> strip it, refuse keys with unexpected
  characters, print an 8-char key fingerprint on both sides, remember a refusal for 60 s; (2) the
  20-min idle clock started before the 6-minute start-up instead of at `[ready]`; (3) both first
  live answers cited README.md/tests instead of the code, because prose and tests restate code in
  the question's own words -> prose x0.6, tests x0.35, +1.6 for a chunk whose own name matches, and
  the prompt now says .md only describes the code. Also: an answer rated bad is never served from
  the cache again. After the fixes `ask_repo` answers in 7-9 s with real file:line refs. 538 tests.

## Open items / next steps
- The owner installs the app from github.com/JatinMangla/jarvis-app, pairs it, and tests
  FUTURE_PLANS §0a (the app checklist).
- Next run: the checks in FUTURE_PLANS.md section 0 (old list + the new features).
- The owner adds the remaining GitHub repo secrets, then tests the Start button end to end.
- On a real phone (app), confirm: loudspeaker/Bluetooth playback, offline Start button,
  YouTube app deep link, price cards.
- On a laptop, try screen sharing.
- Optional: rebuild the model cache so it includes `ddgs`.
- Later: home PC hosting, Home Assistant tools (with spoken confirmation), Phase 5 fine-tune.
