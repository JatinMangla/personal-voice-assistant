# Personal Voice Assistant: Review and Improved Plan (v2)

## Context

The user shared `personal-voice-assistant-plan.md`: a Kaggle-hosted, Alexa-style voice assistant (Whisper → Qwen3-8B via Ollama → Kokoro TTS, Gradio UI, SQLite memory). They asked for its weak points and a better plan.

Priorities: **security (personal data must not be hackable) and speed**. Constraints confirmed with the user:
- **Kaggle only** for now.
- **No Tailscale**, and nothing to install on devices.
- Use it from the laptop and phone browser, "just like Google".
- **No passphrase to start a session.**
- **Tap a button → it listens.**
- **Close automatically after 10 min of no interaction.**
- The user will store personal data in it.

The workspace `d:\git\personal voice assistant` is empty, so this is a new build.

---

## Part A: Weak points in the original plan

### Security
| # | Problem | Why it matters |
|---|---|---|
| S1 | **Gradio `share=True` makes a random public URL** through Gradio's relay servers. Login is a plain username and password with no limit on attempts. | Anyone who finds the link can keep guessing passwords. The link also changes every session, so you must copy it from Kaggle each day. |
| S2 | **Memory DB is stored in plain text** in a Kaggle Dataset. | If the dataset is ever made public or shared by mistake, or leaks, every personal fact is readable. |
| S3 | **Kaggle keeps every old dataset version.** | "Forget that…" deletes the fact only from the newest copy. |
| S4 | **Notebook output logs are saved with each run.** | If transcripts are printed, your personal data ends up in Kaggle logs. |
| S5 | **No account hardening** (2FA). | Account takeover is the easiest attack and gives access to everything. |
| S6 | **"Keep data inside the LLM model" (fine-tuning) is not a security measure.** | Weights are just a file. Anyone who has the file can pull facts out by asking questions, and you cannot delete a fact from weights. Encrypted memory is safer and more accurate. |
| S7 | **Future device control has no safety rules.** | A misheard sentence could trigger a real action. |
| S8 | **Unavoidable limits on Kaggle:** (a) any public-link service (Gradio, ngrok, Cloudflare) decrypts traffic at its own edge server; (b) while running, data is in memory on Google's machine. | Only the future move to a home PC removes both. |

### Speed
| # | Problem | Fix |
|---|---|---|
| P1 | Models are downloaded again every session (~5 GB, 3–5 min of GPU quota). | Cache them in a **private Kaggle Dataset** from day 1. |
| P2 | Speech starts only after the full LLM reply is ready. | Stream the reply sentence by sentence into TTS. The first audio plays after the first sentence. |
| P3 | Whisper `small` is weak on Hinglish. | Use `large-v3-turbo` float16. |
| P4 | Only one of the two T4 GPUs is used. | Ollama on GPU0; Whisper, Kokoro and embeddings on GPU1. |

### Reliability and cost
| # | Problem | Fix |
|---|---|---|
| R1 | There is no idle shutdown, so a forgotten session burns GPU quota. | **10 min with no interaction → save memory → shut down**, which frees the GPU. |
| R2 | The link changes every day. | A **fixed URL** you bookmark once (see B1). |
| R3 | Autosave can fail silently. | Retry, keep a local backup, and show "last saved HH:MM". |
| R4 | Fine-tuning on ~500 turns can't teach facts reliably. | Use it for tone and style only. |

---

## Part B: Improved plan

### B1. Access: fixed link + Google sign-in (nothing to install)
- Expose the Gradio app through **ngrok** (free account). It provides **one free static domain**, e.g. `https://jarvis-xyz.ngrok-free.app`. Bookmark it on your laptop and phone, or add it to the phone home screen so it opens like an app.
- **Google sign-in restricted to your email only**, enforced by ngrok before any request reaches the notebook (ngrok OAuth, `allow_emails: [you@gmail.com]`).
  - You sign in once per device and the browser remembers it.
  - This is not a passphrase. It is the same Google login you already use.
  - Everyone else is blocked at ngrok's edge server and never reaches the assistant.
- HTTPS is automatic, so the mic works on phones.
- The ngrok authtoken is stored in Kaggle Secrets.
- **Fallback** if ngrok OAuth isn't available on the free plan at build time: Gradio's own login page with a long password the browser saves, a 5-attempt lockout, and a remember-me cookie.
- **Honest note:** ngrok (like any tunnel) can technically see the traffic passing through its servers. This goes away when the assistant moves to a home PC.

### B2. Encrypted memory (no passphrase needed)
- The SQLite memory file is encrypted as a single blob with **AES-256-GCM** before upload. The key is a random 256-bit `MEMORY_KEY` generated once and stored in **Kaggle Secrets**. It is loaded automatically, so nothing needs to be typed.
- The Kaggle Dataset and all its old versions contain only ciphertext. A leaked or accidentally public dataset reveals nothing.
- "Forget that…" deletes the row and its embedding. An optional monthly **key rotation** makes old versions permanently unreadable.
- **What protects the key:** your Kaggle account. So **2FA on Google + Kaggle is mandatory** (B3).
- Keep a copy of `MEMORY_KEY` in your password manager. Without it, the memory cannot be recovered.

### B3. Hardening checklist
- Turn on 2FA on your Google account and Kaggle account.
- Keep the notebook and datasets **Private**.
- Code rule: never print transcripts, replies or secrets. Logs contain only timings and errors.
- Secrets: `KAGGLE_USERNAME`, `KAGGLE_KEY`, `NGROK_AUTHTOKEN`, `NGROK_DOMAIN`, `ALLOWED_EMAIL`, `MEMORY_KEY`.

### B4. How it feels to use
1. **Start:** open the notebook on Kaggle (laptop or phone browser) and click **Save & Run All**. It is ready in about 1 minute.
2. **Talk:** open the bookmark. It is already signed in. **Tap the big mic button → speak → it stops listening automatically when you pause** (voice-activity detection) → the reply plays and the text shows below. Tap again for the next question.
3. **Page shows:** a "Listening / Thinking / Speaking" status, minutes used today, and last saved time.
4. **Auto close:** after **10 min with no taps**, it says "Closing now, memory saved", then saves, and the notebook process exits. The GPU is freed and you don't need to stop Kaggle yourself. The daily 60-min cap from the original plan is kept, with spoken warnings at 45 and 55 min.

### B5. Speed stack
| Part | Choice |
|---|---|
| LLM | `qwen3:8b` Q4 via Ollama, `think: false`, streamed, 1–3 sentence replies. Verify the tag and check for newer models at build time. |
| STT | faster-whisper `large-v3-turbo` float16 on GPU1, auto language, VAD |
| TTS | Kokoro (Hindi and English voices), sentence streaming; Piper fallback |
| Embeddings | `intfloat/multilingual-e5-small` on GPU1 |
| Model cache | Private Kaggle Dataset `assistant-models`, mounted read-only |
| Target | First audio in ≤ 2–3 s; setup ≤ 1 min |

### B6. Repository layout
```
personal-voice-assistant/
├── README.md                 # one-time setup + daily steps
├── notebooks/assistant.ipynb # Save & Run All entry point
├── src/
│   ├── config.py      # models, limits (idle 10 min, daily 60 min), language, paths
│   ├── stt.py         # faster-whisper + VAD end-of-speech detection
│   ├── llm.py         # Ollama streaming client, system prompt, memory injection
│   ├── tools.py       # tool registry (empty now; allowlist + requires_confirmation)
│   ├── tts.py         # Kokoro/Piper, sentence streaming
│   ├── memory.py      # SQLite + embeddings: save, search, remember/forget
│   ├── crypto.py      # AES-GCM encrypt/decrypt of DB blob with MEMORY_KEY
│   ├── persistence.py # download/upload encrypted blob via Kaggle API, retry
│   ├── tunnel.py      # ngrok static domain + Google OAuth email allowlist
│   ├── timer.py       # idle 10-min shutdown, daily cap, daily total
│   └── app.py         # Gradio UI: one mic button, status, transcript
├── scripts/
│   ├── setup_kaggle.sh           # install Ollama, link cached models, pip install
│   ├── build_model_cache.ipynb   # one-time: create assistant-models dataset
│   └── make_key.py               # one-time: generate MEMORY_KEY locally
└── kernel-metadata.json          # GPU + internet, private
```
The code is uploaded to Kaggle as a private dataset (`assistant-code`) with `kaggle datasets version`.

### B7. Build phases
- **Phase 0: Bootstrap and model cache.** *Done when:* Ollama answers a test prompt and setup takes under 90 s.
- **Phase 1: Voice loop behind a fixed, login-protected link.** ngrok static domain + Google OAuth, one-tap mic, STT → streamed LLM → streamed TTS. *Done when:* on your phone, the bookmark opens after Google sign-in, you tap and speak, and you hear a reply. A different Google account is blocked.
- **Phase 2: Encrypted memory and persistence.** Encrypted blob, top-k recall, "remember that… / forget that…", autosave every 10 min and on shutdown. *Done when:* a fact told in session 1 is recalled in session 2, and the dataset file is unreadable ciphertext.
- **Phase 3: Idle close and daily cap.** 10-min idle → save → goodbye → exit. 60-min cap with warnings. *Done when:* a Save & Run All session left idle frees the GPU by itself with memory saved.
- **Phase 4: Polish.** Error handling (empty audio, Ollama restart, upload retry), latency tuning, README.
- **Phase 5 (later): Style fine-tune.** Unsloth QLoRA on good turns, encrypted adapter, A/B test against the base model.

### B8. Roadmap changes
- **Stage A (home PC) = the security goal.** The same code runs locally, and the tunnel and cloud limits (S8) disappear.
- **Home devices (Stage C):** every tool has an allowlist entry and a `requires_confirmation` flag. Door and camera actions always need a spoken "yes".

### B9. One-time setup for the user
1. **Kaggle:** create an account, verify your phone, turn on 2FA, and create an API token.
2. **ngrok:** create a free account with Google sign-in, copy the authtoken, and claim the free static domain. No app to install.
3. Run `make_key.py` on your laptop to generate `MEMORY_KEY`. Save it in your password manager.
4. Add all the secrets from B3 in Kaggle → Add-ons → Secrets.
5. Run `build_model_cache` once.

### Defaults for the open decisions
- Language: mixed Hindi and English; replies in the language you spoke.
- Name: "Jarvis", friendly, concise.

---

## Verification
- **Local (laptop, no GPU):** `pytest` unit tests:
  - `crypto.py`: round trip; a wrong key fails.
  - `memory.py`: remember, forget and search on CPU.
  - `timer.py`: idle and cap logic with a fake clock.
- **Kaggle Phase 0:** the notebook prints setup time and Ollama latency.
- **Kaggle Phase 1:** the bookmark works on phone and laptop, and a second Google account gets blocked.
- **Kaggle Phase 2:** the downloaded dataset file is unreadable, and a fact survives a restart.
- **Kaggle Phase 3:** set idle to 2 min, leave it alone, and confirm that it saves and the run ends on its own.

## Verify at build time
- The ngrok free plan still includes a static domain and Google OAuth with an email allowlist. If not, use the B1 fallback.
- Kaggle Secrets are available in Save & Run All runs, and the ngrok tunnel keeps working in them.
- Current Ollama tag for Qwen3 (or a newer model), plus current Kokoro, faster-whisper and Gradio versions.
