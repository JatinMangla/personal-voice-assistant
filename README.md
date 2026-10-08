# Jarvis: personal voice assistant

A private, Alexa-style voice assistant: the **Jarvis app** on your Android phone, or the web page on your laptop.
Tap the mic, speak (English or Hindi), and it answers out loud. It remembers what you tell it.
It runs on Kaggle's free GPU, so there is nothing to buy; the phone app is free from GitHub.

- **Private link:** a fixed web address where only *your* Google account can sign in. Everyone else is blocked before they reach the assistant.
- **Encrypted memory:** everything it remembers is saved to Kaggle already encrypted (AES-256). Without your `MEMORY_KEY`, the saved file is unreadable, even to Kaggle.
- **Auto close:** after 10 minutes without use it saves, says goodbye, and shuts down, so you don't waste GPU hours.
- **Never costs anything:** every free limit is watched. It warns you at 90% and stops by itself at 99% (see below).

---

## Cost: ₹0, no credit card anywhere

| Piece | Price | Free limit | Your use |
|---|---|---|---|
| Kaggle GPU (T4 x2) | Free | 30 GPU hours/week | Up to **257 min/day** (30 h ÷ 7 days) |
| Kaggle notebook output + private datasets | Free | 20 GB output per notebook; private dataset space is far larger | ~12 GB model cache (output) + a few MB of memory (dataset) |
| ngrok (link + Google sign-in) | Free | 20,000 requests + 1 GB/month, 3 sign-in users | ~12,000 requests and ~400 MB even at the full 257 min every day; you are 1 user |
| Models: Qwen, Whisper, Kokoro, e5 | Free, open licence | none | runs on Kaggle's GPU |
| Google account | Free | none | none |

**Never add a card or click "upgrade"** on Kaggle or ngrok. Nothing here needs it.

### Automatic free-limit guard: warn at 90%, stop at 99%

| Limit | Warning (90%) | Automatic stop (99%) | Resets |
|---|---|---|---|
| GPU time today | 231 of 257 min | at 254 min | midnight (India time) |
| GPU time, last 7 days | 27 of 30 h | at 29.7 h | as old days drop out of the 7 days |
| Link requests this month | 18,000 | 19,800 | next month |
| Link data this month | 900 MB | 990 MB | next month |

- **At 90%** a red banner appears on the page, and the assistant says it once after its next reply, e.g. *"Heads up: 90% of today's GPU time is used. About 23 minutes left."*
- **At 99%** it saves your memory, says why it is stopping, and shuts down. This frees the GPU.
- **Start-up check:** each start first checks the limits *before* loading anything heavy. If a limit is at 99%, or fewer than 5 minutes are left, it refuses to start and prints the reason. Almost no GPU time is used.
- The page always shows all four meters.

How the ngrok usage stays low:
- Replies are sent as MP3 (about 6× smaller than WAV).
- The page checks status once a minute, and only while it is connected and visible.

**Good to know:**
- The GPU counter only knows about *this* assistant. If you also run other GPU notebooks on Kaggle, lower `GPU_WEEKLY_HOURS` in `src/config.py` to leave room for them.
- Each session also adds 2 minutes for Kaggle's machine start-up, so the count errs on the safe side.
- ngrok's month may start on a different day than the calendar month; the 99% stop leaves room for that.

---

## One-time setup (about 30–40 minutes)

### 1. Kaggle account
1. Sign up at <https://www.kaggle.com> (Google sign-in is fine).
2. **Settings → Phone verification**: verify your number. This is required for GPU and internet.
3. **Turn on 2-step verification** for your Google account (<https://myaccount.google.com/security>) and on Kaggle if offered. This matters most: your account protects everything else.
4. **Settings → API → Generate New Token**. Kaggle shows a token (one long text, often starting `KGAT_`). **Copy it right away**: it is shown only once. No file is downloaded; that's normal with Kaggle's new tokens.
5. Note your **username**: it's in your profile address, `kaggle.com/<username>`.

### 2. ngrok account (gives you the fixed private link)
1. Sign up at <https://ngrok.com> with the **same Google account** you'll use to open the assistant.
2. Dashboard → **Your Authtoken**: copy it.
3. Dashboard → **Domains**: copy your free domain (looks like `something.ngrok-free.app` or `something.ngrok-free.dev`). Every free account gets one; if you already have one, use it. Don't run another ngrok program with the same domain while the assistant is on.

### 3. Create your memory key (on this laptop)
```
python scripts/make_key.py
```
Copy the printed key. **Save it in your password manager.** If you lose it, the saved memory cannot be recovered.

### 4. Upload the code to Kaggle
1. In PowerShell, in this folder: `python scripts/make_code_zip.py`. This creates `assistant-code.zip`. Don't use Windows "Compress to ZIP": its folder paths break on Kaggle.
2. Kaggle → **Create → New Dataset**. Upload `assistant-code.zip`, name it `assistant-code`, and keep it **Private**. Click Create.

### 5. Build the model cache (makes daily start ~1 min instead of ~5)
1. Kaggle → **Create → New Notebook → File → Import Notebook**. Choose `notebooks/build_model_cache.ipynb`.
2. Right panel, **Settings**: Accelerator = **None**, Internet = **On**.
3. **Save Version → Save & Run All (Commit)**. It takes about 15–20 minutes, and you can close the tab.
4. Keep this notebook **Private**.

### 6. Create the assistant notebook
1. Kaggle → **Create → New Notebook → File → Import Notebook**. Choose `notebooks/assistant.ipynb`.
2. **Settings**: Accelerator = **GPU T4 x2**, Internet = **On**, keep it **Private**.
3. **Add Input**:
   - Your Work → Datasets → `assistant-code`
   - Your Work → Notebooks → the model-cache notebook from step 5
4. **Add-ons → Secrets**. Add each of these, and tick "attached" for this notebook:

| Secret | Value |
|---|---|
| `KAGGLE_USERNAME` | your Kaggle username |
| `KAGGLE_API_TOKEN` | the token from step 1.4 (if you have an old-style `kaggle.json` instead, add `KAGGLE_KEY` with its `key` value) |
| `NGROK_AUTHTOKEN` | from step 2 |
| `NGROK_DOMAIN` | from step 2, e.g. `something.ngrok-free.app` |
| `ALLOWED_EMAIL` | your Gmail address (comma-separate if you ever add more) |
| `MEMORY_KEY` | from step 3 |

### 7. First run
1. **Save Version → Save & Run All (Commit)**.
2. The first time, watch the log (open the running version → **Logs**) until you see `[ready] ... -> https://your-domain`.
3. Open that address on your phone:
   - ngrok shows a one-time warning page: tap **Visit Site**.
   - Sign in with Google.
   - Allow the microphone.
4. Bookmark the link on your laptop. On your phone, install the **Jarvis app** and pair it
   (see "Jarvis app on your phone").

---

## One-tap "Start Jarvis" button (recommended, 10 minutes once)

After 10 quiet minutes Jarvis shuts down, and the link shows **ERR_NGROK_3200** ("offline"). This button starts it again from your phone:
- No Kaggle editor is opened, so no GPU time is wasted.
- It always uses your latest uploaded code.
- If Jarvis is already running, it does nothing.

It uses GitHub Actions: free for private repos, and each start takes under a minute of the 2,000 free minutes/month.

1. **GitHub account:** sign up free at <https://github.com>, and **turn on two-factor authentication** (Settings → Password and authentication).
2. **Private repo:** <https://github.com/new> → name `personal-voice-assistant` → **Private** → don't add a README → **Create repository**.
3. **Upload this project** (PowerShell, in this folder; replace `<you>` with your GitHub username):
   ```
   git remote add origin https://github.com/<you>/personal-voice-assistant.git
   git push -u origin main
   ```
   A browser window asks you to sign in to GitHub the first time. Your secrets are **not** in the project (`.gitignore` keeps keys and zips out).
4. **Secrets on GitHub:** repo → **Settings → Secrets and variables → Actions → New repository secret**. Use the same values as your Kaggle secrets:
   - `KAGGLE_USERNAME`, `KAGGLE_API_TOKEN`
   - `MEMORY_KEY`, `NGROK_AUTHTOKEN`, `NGROK_DOMAIN`, `ALLOWED_EMAIL`
   - `START_URL` (optional)

   Why all of them: Kaggle gives **no Kaggle Secrets** to runs started through its API, which is a Kaggle limitation. So each press encrypts these with a new one-time key and uploads them as the private dataset `jarvis-secrets`, replacing the previous upload. The key goes into the notebook it starts. Neither part alone reveals anything, and an old key is useless after the next start.
5. **Check the notebook name:** open Jarvis on Kaggle and look at the address, `kaggle.com/code/<username>/<name>`. If `<name>` is not `jarvis`, go to the same GitHub page → **Variables** tab → **New repository variable** `JARVIS_NOTEBOOK` = that name.
6. **Use it:** GitHub app or github.com → your repo → **Actions → Start Jarvis → Run workflow → Run workflow**.
   - Bookmark `https://github.com/<you>/personal-voice-assistant/actions/workflows/start-jarvis.yml` on your phone.
   - About 4–5 minutes later, open your Jarvis link.

If the first start fails with `Missing secret`, open the Jarvis editor once → **Add-ons → Secrets**, check all 6 switches are on, stop the session, and press the button again.

7. **Start button inside the app:** add one more Kaggle secret to Jarvis, `START_URL` = `https://github.com/<you>/personal-voice-assistant/actions/workflows/start-jarvis.yml`. From then on, the Jarvis app shows a green **▶ Start** button (right of the mic) whenever Jarvis is off.

**Code updates are automatic too.** Once the repo is on GitHub, every `git push` that changes `src/` uploads the code to Kaggle as a new `assistant-code` version (the "Update code on Kaggle" action, which takes about a minute). The next Start uses it, so you never upload the dataset by hand again.

---

## Jarvis app on your phone (Android)

The phone app replaced the old "install the web page" (PWA) way. It is faster and safer: replies always
play on the loudspeaker or Bluetooth (never the earpiece), reminders ring on the phone even while
Jarvis is off, and there is no Google sign-in page each time. It's free and comes from GitHub (no Play
Store), and **it updates itself**.

**Install (once):**
1. On the phone, open **github.com/JatinMangla/jarvis-app** in Chrome and tap the **download** link
   (the newest `jarvis-….apk`).
2. Chrome asks to allow "install unknown apps": **Settings → Allow from this source** → back → **Install**.
3. Open **Jarvis**. It shows **Scan pairing code**.

**Pair it (once; needs Jarvis running):**
1. On the laptop, open your Jarvis link (Google sign-in as usual) → tap **Phone** (top right) →
   **Show pairing code**.
2. In the app, tap **Scan pairing code** and point the phone at the QR code. The page says **Paired**.
   The code works once and only for 5 minutes.
3. Allow notifications when asked (for reminders), and the microphone on the first tap of the mic.

**Updates:** the app checks GitHub a few times a day. A new version downloads by itself, is checked
(checksum and the same signing key), and installs when you leave the app. The very first time, tap
**Install** on the banner and allow **Install unknown apps → Jarvis** once; after that no taps are needed
(Android 12+).

**Lost phone or new phone:** on the laptop page → **Phone** → **Revoke** next to it. That phone's key stops
working at once. In the app, **Menu → Unpair this phone** deletes the key from the phone.

The app has the same controls as the page: tap the mic and speak, **Whisper**, **Clear**, **Good/Bad**,
link cards (YouTube opens in the YouTube app), and a green **Start** button while Jarvis is off (it opens
your GitHub Start page).

**Screen sharing on the phone (once):** tap **Screen** at the top. Android asks to allow
**Display over other apps** for Jarvis (for the bubble): switch it on, go back, tap **Screen** again.
Then Android asks to start recording/casting: choose **Entire screen** (or one app) → **Start**.

The laptop page stays as a plain web page (no install), for the laptop and for pairing.

**Old "Jarvis" icon from the web page:** if your phone still has the Jarvis icon you added from Chrome
earlier (it opens in Chrome and may show a small Chrome badge), remove it: long-press it →
**Uninstall** or **Remove**. The web page no longer offers installing, and it cleans up the old copy
by itself the next time it opens.

From a laptop you can do the same with `python scripts/start_jarvis.py` (needs `pip install kaggle` and the two values as environment variables).

**Security note:** your Kaggle token can start notebooks that hold your secrets, so protect the GitHub account like your Kaggle account: two-factor authentication on, and keep the repo private.

---

## Daily use

1. Tap your **Start Jarvis** bookmark → **Run workflow** (see above). Without the button: open the assistant notebook on Kaggle → **Save Version → Save & Run All**, then stop the editor session.
2. Wait about 1–2 minutes, then open the Jarvis app (or your bookmark on the laptop). If it says "Jarvis is off", it is still starting: tap the mic a bit later to reconnect.
3. **Tap the mic and speak.** It stops listening when you pause, then answers. Tap while it's speaking to interrupt.
4. When you're done, just leave it. After 10 quiet minutes it saves and shuts down, and the GPU is freed.

The app and the page show minutes used today and when memory was last saved.

### Things you can say
| Say | What happens |
|---|---|
| "Remember that my car number is DL 3C 1234" | Saved as a fact. It is always available to the assistant. |
| "याद रखना कि मेरी मीटिंग सोमवार को है" | Same, in Hindi |
| "Forget that my car number…" / "…भूल जाओ" | Deletes the closest matching fact, and past chats that closely match it |
| "Who sang Kesariya?" / "Latest India cricket score" | Searches the internet (Google first, then Bing, Brave or DuckDuckGo, then Wikipedia) and answers from the results. Free, no API key. |
| "Play Arijit Singh's latest song" / "Kesariya bajao" / "यूट्यूब पर भजन चलाओ" | Finds the video and shows a big card with a thumbnail and a red **Play on YouTube** button. In the **phone app**, after Jarvis says what it found, the video **opens in the YouTube app by itself**. On the laptop, tap Play. |
| "Suggest some romantic songs" / "मुझे कुछ अच्छे गाने सुझाओ" | Video cards with several suggestions (nothing opens by itself). |
| "Cheapest price of boAt Airdopes 141" / "iPhone 16 kitne ka hai" | Searches shops, reads their prices, and says the cheapest. Cards show thumbnail, shop and a green price badge, cheapest first. Prices are approximate, and some shops block automatic reading. |
| "Suggest a good laptop for coding" / "recommend restaurants in Noida" | Searches the web first and shows link cards with the answer. |
| Any search | Results appear as **preview cards** (thumbnail, site, title, short text), like WhatsApp link previews. The voice never reads web addresses. |
| **🖥 Share screen** (laptop page) | Share a window or your screen, then ask "what's this error?", "summarise this page", etc. Each time you ask, Jarvis looks at **one snapshot**; nothing is recorded or saved. |
| **Screen** (phone app, top) | Screen sharing on the phone. A round blue Jarvis **bubble** floats over your other apps. In any app, tap the bubble: Jarvis takes **one snapshot** of that screen, opens and listens, and you ask "what does this say?", "reply to this message for me", "which of these is cheaper?". The bubble can be dragged. Stop with **Stop screen** or the notification's **Stop sharing**. Nothing is recorded or saved; only a bubble tap takes a picture. |
| **Clear** (top right) | Clears the chat on the screen only. Jarvis's memory is kept. |
| **🤫 Whisper** (left of the mic) | For speaking softly, e.g. at night: Jarvis listens more sensitively and boosts your voice. It stays on until you tap it again. |
| Just talk | Memory is automatic. Jarvis decides what's worth keeping: your details, preferences, settings, decisions, what you did and plan, and patterns like what you focus on. Say "forget that …" to remove something. |
| Just mention things about yourself: "I'm allergic to peanuts", "my daughter Riya is 8" | Jarvis **learns lasting facts by itself**, quietly, with no "remember that" needed. Ask "what do you know about me?" to hear them. Health and diet facts are always in front of it, so months later a recipe with peanuts comes with a warning and a substitute. |
| "What did we talk about yesterday?" / "What did I decide last month?" / "पिछले हफ्ते मैंने क्या कहा?" | Jarvis understands dates (today, yesterday, last week, last month, "in March", "3 days ago", also in Hindi/Hinglish) and topics ("my decisions", "health", "goals"), and looks only there. For longer periods it uses short **day/week/month summaries** it writes by itself while you're quiet. Names and numbers ("Rohan", "invoice 48213") are found word for word. |
| "Remind me to call mom at 6" / "Add milk to my to-do list" | Saved as a reminder or to-do. **The phone app rings at the due time**, even while Jarvis is off (it fetches your reminders whenever it's open while Jarvis runs). Due reminders are also said after your first question and shown as notes. |
| "I have to pay the electricity bill of 1,200 rupees on the 5th every month" / "I paid the electricity bill" / "How much did I pay for bills?" | Bills are monthly reminders with an amount. Paying one logs it and moves it to next month; Jarvis can total the last 3 months. |
| "Turn off the living room light" / "Is the washing machine done?" / "Who's at the door?" | Only after you connect Home Assistant (see "Home devices"). Risky actions ask "Should I …?" first and happen only after you say **yes**. |
| Anything else | Normal conversation. Every chat is remembered and recalled when relevant. |
| Tap **Good** / **Bad** under the latest reply | Optional. Rates it (tap again to undo). "Good" replies are what the assistant later learns your style from (Phase 5); only the newest reply shows the buttons. Jarvis never uses emoji in the chat. |

---

## Coder helper: a coding model for Claude Code (optional, free)

A second AI on Kaggle that only does coding chores for Claude Code, so Claude reads less and uses fewer tokens: it answers questions about the code, shortens long logs and files, and drafts small pieces of code (which Claude then checks). It runs **instead of** Jarvis: same ngrok address, same free GPU hours, one at a time.

**What works without Kaggle (already, nothing to set up):** Claude Code has a local code search and a "repo map" (`search_code`, `repo_map`, `summarize` for logs). They use no GPU and no internet.

**What the Kaggle model adds:** `ask_repo` (answers with file:line references), real summaries, `draft_code`, search by meaning, and a one-line description of every file.

**Models:** `qwen3-coder:30b` (19 GB; a "mixture of experts": 30B knowledge but only 3B active per word, so it is fast; split over both T4 GPUs). If it can't start, the run uses `qwen2.5-coder:14b` (9 GB) by itself. Small helper models: `qwen3-embedding:0.6b` (meaning search) and Qwen3-Reranker-0.6B (picks the best code pieces). The log prints the real speed of the run (`[coder] speed: reading ... writing ...`): please paste it so we can compare the two models.

### One-time setup (about 15 minutes)

1. **Make a key** (on this laptop, in this folder): `.venv\Scripts\python scripts\make_key.py`. Copy the printed text. This is your `CODER_KEY`; save it in your password manager too. It is the only thing that lets anyone use the Coder.
2. **GitHub secret:** repo → **Settings → Secrets and variables → Actions → New repository secret** → name `CODER_KEY` → paste the key → **Add secret**. (The other secrets are the same ones Jarvis uses; they are already there.)
3. **Start it:** GitHub app or github.com → your repo → **Actions → Start Coder → Run workflow → Run workflow**. If Jarvis is running it refuses (close Jarvis first). About 5–8 minutes later the Kaggle log shows `[ready]`. (Read it without opening Kaggle: **Actions → Jarvis log → Run workflow**, type `coder` in the *notebook* box.)
4. **Tell Claude Code the key** (this laptop, no admin needed): press the Windows key, type **environment variables**, open **Edit environment variables for your account**, under **User variables** click **New** → name `CODER_KEY`, value = your key → **OK**. Then close and reopen VS Code. (The address is already set to your ngrok domain in `.mcp.json`; set a `CODER_URL` variable too only if the domain ever changes.)
5. **Check:** in Claude Code run `/mcp`: `coder` should be connected. Ask Claude to run `coder_status`: it says `Coder: online` while the Kaggle run is up and `offline (press Start Coder)` otherwise.

The Coder stops by itself after **20 minutes without a request** (and after 3 hours, or at 99 % of any free limit). Its GPU time is added to the same counters as Jarvis, so the Start buttons and the limit guard stay correct.

### Use it directly in VS Code (chat with the model yourself)

Install the free **Continue** extension, open its config (`config.yaml`) and add:
```yaml
models:
  - name: Jarvis Coder
    provider: openai
    model: jarvis-coder
    apiBase: https://<your ngrok domain>/v1
    apiKey: <your CODER_KEY>
    requestOptions:
      headers:
        ngrok-skip-browser-warning: "1"
```

### Safety (what to know)

- Only the folders in `CODER_ALLOWED_DIRS` (default: this repository) are ever read, and never secret files (`.env`, `*.enc`, keystores, `kaggle.json`, names containing `secret`/`password`...). **Do not add office/company folders** unless your company allows sending that code to Kaggle (Google) and ngrok.
- The model's answers are treated as untrusted. `draft_code` never writes files; it shows a draft after checking paths, syntax and lint. Running the project's tests on a draft is **off** by default (it would run model-written code on this PC); turn it on with the user variable `CODER_RUN_TESTS=1` if you want it. It runs in a temporary copy, never in your real files.
- The link needs the key: ngrok rejects requests without an `Authorization` header, and the proxy checks the key (10 wrong keys in 15 minutes lock out wrong keys). Only chat and embeddings are forwarded; nothing can pull, create or delete models through it.
- Logs show timings and sizes only, never your code or the answers.

---

## Security: what is protected and what isn't

| Threat | Protected? |
|---|---|
| Someone finds your link | **Yes.** Only your Google account can sign in (ngrok checks this before anything reaches Kaggle). The phone app skips Google sign-in, so every app request must carry a paired phone's key (256-bit, random); wrong keys are refused and slowed down. |
| Your phone is lost or stolen | **Yes, once you revoke it.** Laptop page → Phone → Revoke: its key stops working at once. The key sits in the phone's secure hardware storage (Android Keystore) and is never backed up; the app keeps no chat history beyond the screen. A phone's key can't pair new phones. |
| Screen sharing on the phone | **Only when you ask.** It runs only after you switch it on (Android shows its own sharing sign the whole time), and a picture is taken only when you tap the bubble; that one picture goes with that one question and is never saved. |
| A fake app update | **Yes.** The app installs only files from the releases repo whose checksum matches and that are signed with the same key as the installed app (Android checks the key too). |
| Your memory dataset leaks or is made public by mistake | **Yes.** It is only ciphertext; old versions are deleted on every save. |
| Someone reads the notebook logs | **Yes.** Conversations are never printed. |
| Internet search leaking personal data | **Yes.** Only the short search words the assistant picks are sent to the search engine; never your memory. Web results are treated as untrusted text and can't trigger any action. Set `WEB_SEARCH = False` in `src/config.py` to turn internet off. |
| Learning something it shouldn't | **Mostly.** It learns only from *your* words, never web text; anything that looks private (numbers, PINs, OTPs, passwords, emails) is never learned; "what do you know about me?" lists what it knows, and "forget that …" removes it. Set `LEARN_FROM_CHAT = False` in `src/config.py` to switch learning off. |
| "Forget" leaving traces | **Mostly.** Deleted rows are zeroed in the database, copies of the same fact and the summaries of those days are deleted too, the search index is never saved, and old dataset versions are deleted. |
| A web page tricking Jarvis into switching something on | **Yes.** Device actions run only after *your* spoken "yes" on the very next question, checked by fixed rules, not by the AI. Only devices and actions you listed exist. |
| The Start button reading your memory | **No, it can't.** It reads only a separate small encrypted file with usage numbers (GPU minutes, link counts), never conversations or facts. |
| Reminder notifications | **Private by default.** On the lock screen the app's notification only says "You have a reminder"; the text shows after unlocking. The reminder list is kept encrypted on the phone. (ntfy, if you use it, only ever says "You have a reminder".) |
| ngrok seeing your traffic | **On the home PC with Tailscale: protected.** On Kaggle: no (ngrok can technically see it). ngrok decrypts traffic to do the Google sign-in; the Tailscale link is encrypted from your phone to Jarvis, and Jarvis also checks your Tailscale login. |
| Your Google/Kaggle/GitHub account is hacked | **No.** The key sits in Kaggle Secrets, and the Kaggle token (also in GitHub if you use the Start button) can run notebooks that use it. That's why 2-step verification on all three is essential. |
| Kaggle/Google or ngrok looking at live traffic | **No.** While running, data is on their machines. The only fix is running on your own PC later (same code). |

Why facts are not "trained into the model": a model file can be made to repeat what it was trained on, and a fact can't be removed from it. Encrypted memory is safer, can be deleted, and is recalled more accurately.

---

## Updating the code
**With GitHub set up:** change files in `src/`, `git commit`, `git push`. That's all: the code is uploaded to Kaggle automatically, and the next Start uses it.

**Without GitHub:** run `python scripts/make_code_zip.py`, then open the `assistant-code` dataset on Kaggle → **New Version** → upload `assistant-code.zip`. In the assistant notebook's input panel, click the update icon next to `assistant-code`.

To change the model, edit `LLM_MODEL` in `src/config.py` **and** `LLM` in the model-cache notebook, then rebuild the cache (step 5).

## Settings you might change (`src/config.py`)
| Setting | Default |
|---|---|
| `ASSISTANT_NAME` | Jarvis |
| `IDLE_TIMEOUT_MIN` | 10 |
| `GPU_WEEKLY_HOURS` | 30 (daily share = 30 h ÷ 7 ≈ 257 min) |
| `NOTIFY_AT` / `STOP_AT` | 0.90 / 0.99 |
| `LLM_MODEL` | `qwen3.5:9b` (falls back to `qwen3:8b`) |
| `WHISPER_MODEL` | `large-v3-turbo` |
| `NTFY_INCLUDE_TEXT` | `False` (reminder notifications say only "You have a reminder") |
| `OLLAMA_FLASH_ATTENTION` | `False` (an experiment: try `True` and compare the `[turn]` speed lines) |
| `WAKE_WORD` | `True` (home PC only: "Hey Jarvis" on the PC's microphone) |

## Fallback: password page instead of Google sign-in
If ngrok's Google sign-in ever stops working on the free plan, delete the `ALLOWED_EMAIL` secret and add `APP_PASSWORD` (use a long one and let your browser save it). The page then asks for the password once per device for 30 days, and locks for 15 minutes after 5 wrong tries.

## Troubleshooting
| Problem | Fix |
|---|---|
| `Missing secret ...` | Add the secret and make sure it's ticked for this notebook. |
| `Could not download memory` | A Kaggle API problem. The assistant refuses to start rather than risk overwriting your memory. Run again later. |
| `Wrong MEMORY_KEY` | The secret doesn't match the key used before. Paste the one from your password manager. |
| Page says offline | The notebook isn't running (or is still starting). Tap the mic to check again. |
| Link stops working late in the month | The ngrok free monthly limit was reached. It resets next month, and you are never charged. |
| "SAVE FAILED" | Uploads are retried. An encrypted copy is also kept in the run's output as `memory-backup.db.enc`. |
| Mic doesn't work | Allow microphone for the site in the browser settings. |

## Running the tests (laptop, no GPU)
```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest tests
```

## Phase 5: teach it your style (after 500+ 👍 replies)

Fine-tuning shapes *how* Jarvis talks: tone, length, Hinglish style. Your facts stay in the encrypted memory and are not trained in. It uses **Qwen3-8B**, because a fine-tuned Qwen3.5 can't be loaded into Ollama yet.

1. Keep tapping 👍 on replies you like, over a few weeks.
2. Kaggle → **Create → New Notebook → File → Import Notebook** → `notebooks/finetune.ipynb`.
3. **Settings:** GPU T4 x2, Internet On, Private. **Add Input:** `assistant-code`. **Secrets:** tick `KAGGLE_USERNAME`, `KAGGLE_API_TOKEN`, `MEMORY_KEY`.
4. **Save Version → Save & Run All.** It takes about 45–90 minutes. Don't run the assistant at the same time.
   - If there aren't 500 usable 👍 replies yet, or not enough free GPU time, it stops at once and tells you why.
   - Training stops early rather than pass the 99% free limit, and its GPU time is added to your counters.
   - Replies that look private (long numbers, emails, "password", "OTP") are never used for training.
5. When it finishes, open the **assistant** notebook → **Add Input → Your Work → Notebooks →** the fine-tune notebook.
6. Use the assistant as usual and keep rating replies. For each reply it secretly picks the old or the new model at random. After 30 ratings each, it automatically keeps the one you rated higher. The log shows the score: `[models] base ...: 👍/👎 (..) | tuned ...: 👍/👎 (..) -> using: ...`.

The trained model is stored **encrypted** with your `MEMORY_KEY`, like the memory.

To stop using it, remove that input from the assistant notebook, or set `TUNED_MODE = "base"` in `src/config.py`.

## Reminder notifications without the phone app (optional, free)

The Jarvis phone app rings for reminders by itself; you don't need this. Only without the app (e.g.
an iPhone): Jarvis runs only when you start it, so it can't ring by itself. With this, the free service
ntfy.sh sends a notification at the due time (up to 3 days ahead; later reminders are
scheduled the next time you start Jarvis). The notification only says "You have a reminder".

1. Think of a long random topic name, e.g. `jarvis-7f3k9q2m8x4w` (anyone who knows the name
   could read the notifications, so make it unguessable).
2. On your phone: open **ntfy.sh/app** in Chrome → **Subscribe to topic** → type the name →
   allow notifications. (Or install the ntfy app, if you prefer.)
3. Add the secret `NTFY_TOPIC` = that name: in Kaggle (Add-ons → Secrets) **and** on GitHub
   (repo → Settings → Secrets and variables → Actions → New repository secret).

## Private link with Tailscale (home PC only)

**Not on Kaggle:** Kaggle stopped every run that started Tailscale (tested 2026-09-29), and
trying to get around that could put your Kaggle account at risk. On Kaggle, Jarvis uses the
ngrok link with Google sign-in (see the Security table: only your Google account gets in).

When Jarvis runs on **your own PC** (see "Run on your own PC"), Tailscale gives the best link:
end-to-end encrypted from your phone to the PC, no public address at all, no ngrok limits.

1. Install the **Tailscale app on the PC** and sign in with the same account as on your phone.
2. Tailscale admin page (login.tailscale.com) → **DNS** → **HTTPS Certificates** → **Enable**
   (phone browsers allow the microphone only on https pages).
3. In `~/.jarvis/secrets.env` keep `ALLOWED_EMAIL=<your Tailscale login>` (or add
   `TAILSCALE_USERS=<login>` if your Tailscale login isn't that Gmail).
   Leave out the `NGROK_…` lines if you want **Tailscale only** (no public link at all).
4. Start Jarvis (`scripts\run_home.py`). It runs `tailscale serve` by itself; the log shows
   `[ready]  -> https://<pc-name>.<tailnet>.ts.net (Tailscale…)`.
5. On the laptop (Tailscale connected), open that link → **Phone** → **Show pairing code**, and
   pair the phone app with it (the phone needs Tailscale connected too).

Note: https certificates are public records, so the PC's `….ts.net` name is visible on the
internet, but nobody outside your tailnet can connect to it.

## Samsung washing machine, free (home helper on your phone)

Samsung's SmartThings API becomes paid in October 2026, but the washer also answers directly
on your home Wi-Fi. A small helper on your Android phone (Termux) talks to it and passes
messages to Jarvis: "is the washing done?", "start the washing machine" (after your "yes"),
plus a phone notification when a wash ends. Step-by-step: [home_bridge/README.md](home_bridge/README.md).

## Home devices and cameras (Home Assistant, optional)

Jarvis talks to your devices through **Home Assistant** (free, runs at home, e.g. on a
Raspberry Pi or the home PC). Home Assistant connects to SmartThings (Samsung washer/fridge),
Home Connect (Bosch dishwasher), Android TV (Mi TV), Tuya/LocalTuya (Syska bulb) and smart
plugs (the air fryer).

1. In Home Assistant: your profile (bottom left) → **Security** → **Long-lived access tokens**
   → Create → copy it.
2. Write the list of what Jarvis may control (only these exist for it):
   ```json
   {"devices": [
      {"name": "living room light", "entity": "light.syska_bulb", "actions": ["turn_on", "turn_off"], "confirm": false},
      {"name": "air fryer", "entity": "switch.air_fryer_plug", "actions": ["turn_on", "turn_off"]},
      {"name": "tv", "entity": "media_player.mi_tv", "actions": ["turn_on", "turn_off", "volume_up", "volume_down"]},
      {"name": "washing machine", "entity": "sensor.washer_job_state", "actions": []}],
    "cameras": [{"name": "front door", "entity": "camera.front_door"}]}
   ```
   Entity names are in Home Assistant → Settings → Devices & services → Entities.
   `"confirm": false` means no "yes" needed: only use it for harmless things like a light.
3. Secrets (Kaggle and GitHub, as above): `HA_URL` (e.g. `https://yourhome.duckdns.org`, or
   on the home PC `http://homeassistant.local:8123`), `HA_TOKEN`, and `HOME_DEVICES` (the list
   above, pasted as one line). On the home PC you can put the list in `devices.json` instead.

From Kaggle, Home Assistant must be reachable over https from the internet; that's easiest
once Jarvis runs on the home PC (then the plain home-network address works).

## Run on your own PC (home mode)

The same code runs at home: no cloud, no GPU-hour limits, always on, and "Hey Jarvis" on the
PC's own microphone. With an NVIDIA GPU it uses the same models as Kaggle; without one it
uses smaller ones (slower, still private and free).

1. Install **Python 3.12** (python.org, tick "Add to PATH") and **Ollama** (ollama.com).
   For Hindi speech also install **espeak-ng** (github.com/espeak-ng/espeak-ng/releases).
2. With an NVIDIA GPU: install PyTorch with CUDA first (pytorch.org → "Get started").
3. In this folder:
   ```
   python -m venv .venv-home
   .venv-home\Scripts\python -m pip install -r requirements-home.txt
   ```
4. Create the folder `C:\Users\<you>\.jarvis` and in it a file `secrets.env`:
   ```
   MEMORY_KEY=<the same key as on Kaggle>
   NGROK_AUTHTOKEN=<optional: for your phone>
   NGROK_DOMAIN=<optional>
   ALLOWED_EMAIL=<your Gmail>
   ```
5. To bring your memory home: Kaggle → Datasets → `assistant-memory` → Download → put
   `memory.db.enc` into that `.jarvis` folder (it stays encrypted; same `MEMORY_KEY`).
6. Start: `.venv-home\Scripts\python scripts\run_home.py`. The first start downloads the
   models (a few GB). Without ngrok, open `http://127.0.0.1:8000` on the PC itself.
   Say **"Hey Jarvis"** near the PC to talk hands-free.

Don't run Jarvis on Kaggle and at home at the same time: each has its own copy of the memory.

## Later
- **Cameras that recognise faces** and more utilities, as tools with the same rules.
