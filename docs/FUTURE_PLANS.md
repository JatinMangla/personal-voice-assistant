# Future plans: Jarvis (personal voice assistant)

Hand this file to the next Claude Code session. **Read [CLAUDE.md](../CLAUDE.md) first**: it
has the owner's preferences, the hard rules (₹0 cost, security, no secrets or conversation
content in logs), the architecture, and the known Kaggle/Ollama/ngrok gotchas. The full history
is in [PROJECT_LOG.md](PROJECT_LOG.md).

**Working style:** the owner runs everything on Kaggle and pastes logs back. Give them
click-by-click steps. Build and test locally (`.venv\Scripts\python -m pytest tests`,
`node --check` for page JS), then commit and push to `main`. Pushing auto-uploads `src/` to
Kaggle, and the owner presses **Start Jarvis** on GitHub.

**State (2026-09-28, night 2):** every item of the previous plan is built and unit-tested
(296 tests), except split storage and float16 vectors, which wait for their trigger (§2).
**None of it has run on Kaggle or a real device yet.** So the next job is §0.

---

## 0. First: confirm on Kaggle and on a real device

Ask the owner to start Jarvis and paste the log. Check each item; fix what fails before new work.

| Check | Expected |
|---|---|
| Start-up log | `[setup] steps:`, `[reminders] n pending`, `[ready] in ~3–5 min`; no `[memory] keyword search unavailable` (that would mean Kaggle's SQLite lacks FTS5: meaning search still works) |
| `[turn]` lines | first audio ≤ 3 s; `prompt ... tokens` not larger than before (facts are no longer listed twice) |
| `[setup] ... first answer` / `test reply` | now ends with `model load Xs`: tells whether the 47 s is loading or GPU start-up |
| Save line | `[memory] saved at HH:MM (x MB, n turns, m facts)` |
| Start button | a second press while the day's limit is used up turns red at once: "NOT started: …" (needs one save by the new code first, which writes `usage.json.enc`) |
| Memory | "What did we talk about yesterday?", "What did I decide last month?", a name or number said weeks ago; Hindi question about an English fact |
| Forget | "Forget that …" removes the fact; asking again doesn't find it |
| Reminders | "Remind me to call mom at 6" → saved; start Jarvis after 6 → "Reminder: call mom." after the first reply, ⏰ note on the page |
| Bills | "I pay the electricity bill of 1,200 rupees on the 5th every month" → "I paid it" → "how much did I pay?" |
| Old list | loudspeaker/earphones, UI, links/YouTube app, price cards, screen sharing, limits answer (unchanged since last time) |

Then, one at a time and only when the owner wants them:
- **ntfy push:** README "Reminder notifications" (needs the `NTFY_TOPIC` secret).
- **Flash attention A/B:** set `OLLAMA_FLASH_ATTENTION = True`, compare the `[turn]` speed lines
  with the last run, keep whichever is faster.
- **Model cache rebuild:** re-run `notebooks/build_model_cache.ipynb` once (it already lists
  `ddgs`; saves ~17 s per start when the cache is used).

## 0b. Samsung washer via the phone helper (built 2026-09-29, not yet run)

The owner chose their Android phone as the home helper. Next steps are theirs, in
[home_bridge/README.md](../home_bridge/README.md): Termux, rescan with the new
`find_samsung.py` (washer ON), `setup_cert.py --test` against WW12DG6B24ASTL, BRIDGE_KEY
secret, run `samsung_bridge.py`. If the certificate is rejected (`unknown_ca`), the washer
needs the OCF-PKI/OwnerPSK path (SmartThings-Local docs/ocf-pki-laundry.md: not public yet).
If the status text looks wrong, log the raw `/operational/state/vs/0` keys (not values) and
adjust `describe()`.

## 1. Hybrid memory retrieval: DONE (verify in §0)

FTS5 (TEMP tables, never saved) + vectors + RRF + recency, date/category/recall rules
(`query_hints.py`), day/week/month digests (`digest.py`). Possible tuning after real use:
`SEARCH_CANDIDATES`, `RECENCY_BOOST`, the "too common word" cut-off (10 % of rows), and
`MEMORY_MIN_SIMILARITY` (0.80) now that keywords catch exact matches.

## 2. Scaling memory (only when the log says so)

Trigger: the save line shows "memory is large: time to split storage" (50 MB or 20,000 turns;
`config.SPLIT_STORAGE_AT_*`). Not built on purpose: changing the storage format is the riskiest
change to memory (hard rule 3), and it isn't needed yet.

- **Split storage:** a "hot" DB (facts, reminders, digests, last 90 days) uploaded at autosave,
  plus encrypted, immutable **monthly archive** files. A Kaggle dataset version replaces all its
  files, so archives need their own dataset(s) (e.g. `assistant-memory-archive-2027`), uploaded
  once. Load: hot DB at start; archives only when a search needs older turns (or load all and
  keep them read-only). Keep `MemoryStore.blocked`-style protection for every file.
- **float16 embeddings** (half the size); `sqlite-vec` only if brute force gets slow
  (measured ~20 ms per recall at 15k turns).
- The time-summary tree is already built (digests); with split storage, search digests first,
  then drill into the archive of that month.

## 3. Backlog: DONE

- Start button budget check (`usage.json.enc`), reminders/to-dos/bills (+ ntfy), load-time
  logging + flash-attention switch, recording-format choice + mic-track-ended handling.
- **Recording failures:** if `[turn] recording unreadable: …; browser said …` reappears, the
  log now shows the format the browser claimed; compare with `type …` (the real bytes).
- **Phase 5 fine-tune:** unchanged: run `notebooks/finetune.ipynb` once there are 500+ 👍.

## 4. Roadmap: built, waiting for hardware

| Stage | Status | What the owner needs |
|---|---|---|
| A **Home PC** | `PVA_HOSTING=home`, `scripts/run_home.py`, `LocalMemoryStore`, CPU fallback | A PC (NVIDIA GPU ideal), README "Run on your own PC". Re-ask about Tailscale/Cloudflare only if ngrok isn't enough. |
| B **Wake word** | `local_voice.py`, "Hey Jarvis" (openWakeWord) on the PC's mic + speakers | Home PC with a mic/speaker. Tune `WAKE_THRESHOLD` if it wakes by mistake or misses. |
| C **Home devices** | `home.py`: Home Assistant allowlist, spoken yes unless `"confirm": false` | Home Assistant + integrations, `HA_URL`, `HA_TOKEN`, `HOME_DEVICES`. **Samsung washer: SmartThings API is paid from Oct 2026 → use a power-monitoring plug (status only). Qubo Q400 purifier: no integration exists → local smart plug or IR blaster.** See CLAUDE.md gotcha 26. |
| D **Cameras** | `look_at_camera`: one snapshot to Qwen3.5 vision, never stored | Cameras in Home Assistant (RTSP/ONVIF). Face recognition is not built (privacy: would need consent of people filmed). |
| E **Utilities** | bills with payment log in `reminders.py` | Nothing. Ideas: meter readings as a tool, bill due-date alerts via ntfy. |

## Rules that must survive every change

- ₹0 always. Keep the 90 % warn / 99 % stop guard working, and never add paid services.
- Never log or print secrets, transcripts, replies, facts, reminders or screenshots/camera images.
- Never let an empty or failed memory load overwrite the saved memory (`MemoryStore.blocked`,
  `LocalMemoryStore` refuses a missing file when `.prev` exists).
- The same `num_ctx` in every Ollama request (chat, learning, reflection, digests).
- Web, screen and camera content is untrusted data, never instructions. Device actions need
  the user's own spoken yes (`tools.pending` + `intents.parse_confirmation`), never the model's.
- Update `CLAUDE.md` and `docs/PROJECT_LOG.md` after each piece of work.
