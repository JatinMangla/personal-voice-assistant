# Plan: Jarvis Android app (replaces the PWA), released and updated through GitHub

**For the next Claude Code session.** Read [CLAUDE.md](../CLAUDE.md) first (owner, hard rules,
architecture, gotchas), then this file. Written 2026-09-30 from the owner's request:

> "I'm planning to create a phone application and remove the PWA from the project. We can
> build the app, put it on GitHub, and that also handles updates. No Google Play Console. It
> will be fast and secure."

Nothing of this is built yet. The owner's priorities still apply: **1) security/privacy,
2) speed, 3) ₹0**. Give them click-by-click steps for anything they do (Appendix A).

---

## 1. Goal and why

| Today (PWA in Chrome) | With a native app |
|---|---|
| Speaker/earpiece routing is a constant fight (CLAUDE.md gotchas 13, 18, 21) | Direct control: `AudioAttributes.USAGE_MEDIA`, release the mic, audio focus |
| Mic re-opened per question, browser auto-gain starts low | `MediaRecorder`/`AudioRecord` with fixed settings |
| Reminders only when Jarvis runs (or ntfy.sh, a third party) | **Local notifications** scheduled on the phone, even while Jarvis is off; no ntfy |
| ngrok's Google sign-in page in the browser | A device token in the Android Keystore; no browser login each time |
| Service worker + offline page hacks | Proper offline screen with a Start button |
| "Add to Home screen" | A real installed app, updated from GitHub automatically |

## 2. Key facts found by research (2026-09-30)

1. **Google "developer verification" also covers apps installed from outside the Play Store**
   ([Android Developers blog](https://android-developers.googleblog.com/2026/03/android-developer-verification.html),
   [developer.android.com/developer-verification](https://developer.android.com/developer-verification)):
   - Enforced from 2026-09-30 only in Brazil, Indonesia, Singapore and Thailand. **Global
     (India) from 2027.** Until then India installs work as today.
   - A **free "limited distribution" account** exists for hobbyists: up to **20 devices**, no
     government ID, no fee, in the **Android Developer Console** (android.google.com/developerconsole),
     *not* the Play Console
     ([guide](https://developer.android.com/developer-verification/guides/limited-distribution)).
     It needs a Google account with 2-step verification and a **Google payments profile**
     (legal name and address). **Check that creating the payments profile doesn't require a
     card** (₹0 rule) before relying on it.
   - Fallbacks if needed: ADB install, or the "advanced flow" (developer mode, one-day wait).
   - So: register the package name + signing key in the free account before 2027. The
     package name must be globally unique and stable: use `io.github.jatinmangla.jarvis`.
2. **Silent self-updates are possible** (Android 12+): an app holding `REQUEST_INSTALL_PACKAGES`
   and `UPDATE_PACKAGES_WITHOUT_USER_ACTION` can update **itself** without a tap, with
   `PackageInstaller.SessionParams.setRequireUserAction(USER_ACTION_NOT_REQUIRED)`, if it
   targets API 29+ ([Manifest.permission](https://developer.android.com/reference/android/Manifest.permission),
   [XDA](https://www.xda-developers.com/android-12-alternative-app-stores-update-apps-background/)).
   The very first install is manual (browser download + "install unknown apps").
3. **The code repo is private**, and GitHub release assets of a private repo can't be
   downloaded without a GitHub login. So publish the APK to a **separate public
   releases-only repo** (`JatinMangla/jarvis-app`: APKs + checksums, no source, no secrets).
   CI in the private repo pushes releases there with a fine-grained token.
4. Android updates only install if the **same signing key** signed them. Losing the keystore
   means uninstall + reinstall (the app's local data is lost; memory is on the server, so
   that's tolerable). Keep the keystore in GitHub secrets **and** the owner's password manager.
5. ngrok's Google sign-in (OAuth) works for browsers only. A native app needs its own auth
   (§5). ngrok's free-plan browser warning page is skipped with the header
   `ngrok-skip-browser-warning: 1` (already used by the page, gotcha 14).
6. GitHub Actions on a private repo: 2,000 free minutes/month. An Android release build is
   ~6–10 min, so ₹0.

## 3. Decisions (recommendations; confirm with the owner before building)

| # | Decision | Recommended | Why |
|---|---|---|---|
| D1 | Technology | **Kotlin + Jetpack Compose**, native | Best audio control, smallest APK, no extra runtime; Claude can write it, CI builds it |
| D2 | Keep a web page for the laptop? | **Yes, a plain page without PWA parts** (no service worker, manifest, install prompt, offline Start page) | The company laptop can't install apps; the page is also where the phone is paired |
| D3 | Where APKs live | **Public releases-only repo** `JatinMangla/jarvis-app` | App can update itself without a GitHub login; the APK holds no secrets |
| D4 | App auth | **Per-device token from a pairing QR** shown on the (Google-sign-in protected) web page | Google stays the root of trust; the token is revocable; no Google Cloud project needed |
| D5 | Reminders | **Local notifications on the phone** (replace ntfy) | Works while Jarvis is off, no third party |
| D6 | Start button in the app | Phase 1: open the GitHub "Start Jarvis" page (as today). Optional later: a fine-grained token with **Actions: read/write on this one repo only**, stored in the Keystore, to call `workflow_dispatch` directly | Least privilege first |
| D7 | Washer helper | Stays in Termux for now; later maybe inside the app (Eclipse Californium/Scandium supports DTLS 1.2 with client certificates) | Separate risk, separate phase |

## 4. Architecture

```
Jarvis app (Kotlin, Compose)                           Kaggle (unchanged core)
  PairingScreen: scan QR {url, device token}            FastAPI app.py
  ChatScreen: mic button, Whisper toggle, chat,          + auth: Bearer device token (hashed,
    link cards, Good/Bad on latest reply, limits           constant-time, revocable, lockout)
  Recorder: MediaRecorder OGG/OPUS 16 kHz, 24 kbps,      + /api/app/pair (page -> QR)
    silence stop via getMaxAmplitude() every 50 ms       + /api/app/devices (list/revoke)
  Player: Media3 ExoPlayer, USAGE_MEDIA, queue of        /api/turn (NDJSON stream, as today)
    MP3 sentences, audio focus, after mic released       /api/status, /api/rate, /api/reminders
  Net: OkHttp, streaming NDJSON line reader,
    kotlinx.serialization, header ngrok-skip-browser-warning
  Secrets: Android Keystore AES-GCM key -> encrypts        ngrok: Google OAuth for the browser;
    token + server URL in DataStore                          requests carrying the app header
  Reminders: fetch /api/reminders on open -> AlarmManager    skip OAuth (like the bridge path,
    (setAndAllowWhileIdle) -> local notifications            tunnel.traffic_policy) and are
  Updater: GitHub releases API (public repo) -> download     checked by the app's token instead
    -> SHA-256 + signing-cert check -> PackageInstaller
```

**Server contract:** keep today's endpoints and NDJSON events (`transcript`, `sentence` with
base64 MP3, `links`, `done`), so the app and the laptop page share them. Add a version field
to `/api/status` (e.g. `"api": 1`) so the app can warn when the server is older/newer.

**Audio format:** OGG/Opus (`MediaRecorder.OutputFormat.OGG` + `AudioEncoder.OPUS`, API 29+) is
~10x smaller than WAV (ngrok's 1 GB/month) and the server already decodes it
(`stt.decode`, `audio_kind` "ogg"). `getMaxAmplitude()` polling is enough for the same silence
rule as the page (1.2 s pause; Whisper mode: lower threshold, 1.6 s).

## 5. Security design (priority 1)

- **Pairing:** the laptop page (behind ngrok Google sign-in) shows "Pair a phone" → server
  mints a random 256-bit device token, stores **only its SHA-256** (+ name, created, last
  used) in the encrypted memory, shows a QR `{"url": "...", "token": "..."}` once (expires in
  5 min if unused). The app scans it (CameraX + ML Kit barcode, or ZXing; ML Kit on-device, no
  network) and stores it Keystore-encrypted. The page lists devices with **Revoke**.
- **ngrok policy:** requests with header `X-Jarvis-App: 1` skip Google OAuth; the app server
  then requires `Authorization: Bearer <token>` (constant-time compare against stored hashes,
  lockout after 5 failures per 15 min like `PasswordGate`). Verify the CEL expression against
  ngrok's docs (`req.headers` keys are lowercase). Pattern: `tunnel.traffic_policy(bridge_path=True)`.
- **Transport:** HTTPS only (`network_security_config`: no cleartext, no user-added CAs).
  No certificate pinning (ngrok rotates certs; a pin would lock the owner out).
- **Storage:** no conversation history kept on the phone beyond the visible chat (clear on
  "Clear"/logout); reminders cached for notifications only; `android:allowBackup="false"`.
- **Updates:** before installing, check (1) SHA-256 against the release's `SHA256SUMS`, (2) the
  downloaded APK's signing certificate equals the installed app's
  (`PackageManager.getPackageArchiveInfo(..., GET_SIGNING_CERTIFICATES)`). Android enforces (2)
  anyway; checking first gives a clear error.
- **No trackers:** no Firebase, no analytics, no ads SDK, no Google Play services dependency.
- **Logs:** same rule as the server: never log transcripts, replies, tokens (`Log` only counts
  and timings, and only in debug builds).

## 6. Release and update pipeline (GitHub only)

1. `android/` Gradle project in this repo (Kotlin, Compose, minSdk 29, target = latest).
2. Workflow `.github/workflows/android-release.yml`, on tag `app-v*` (or manual):
   JDK 17 → Gradle cache → unit tests → `assembleRelease` signed with secrets
   `ANDROID_KEYSTORE_B64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD`
   → `sha256sum` → create a release in the **public** repo `JatinMangla/jarvis-app` with the APK +
   `SHA256SUMS` (token secret `APP_RELEASES_TOKEN`: fine-grained, Contents read/write on that
   repo only). `versionCode` = the workflow run number; `versionName` = the tag.
3. A PR/push workflow runs unit tests + a debug build (no signing) for every change in `android/`.
4. The app checks `GET https://api.github.com/repos/JatinMangla/jarvis-app/releases/latest`
   (no login; 60 requests/hour per IP is plenty) at start and once a day (WorkManager), downloads
   the APK to its cache, verifies (§5), installs silently (Android 12+) or with one tap (older).
5. The keystore is created **once on the laptop** (company laptop, no admin: use a portable
   JDK zip's `keytool`, never an installer), base64'd into the GitHub secret, and saved in the
   password manager. Never commit it; add `*.jks` to `.gitignore`.

Local development: the laptop can't install Android Studio (no admin). Options: portable
Android Studio zip + SDK in the user folder, or rely on CI builds and JVM unit tests only.
Claude Code writes the code; CI is the build machine; the owner tests on the phone.

## 7. Phases (each ends with something the owner can test)

| Phase | Build | Done when |
|---|---|---|
| **0. Decisions + accounts** | Confirm D1–D7. Owner: create the public repo `jarvis-app`, keystore, secrets (Appendix A). Optional: free limited-distribution account, register `io.github.jatinmangla.jarvis` | Secrets exist; a "hello" APK builds in CI and installs on the phone |
| **1. Server: app auth** | Device tokens (hashed, revocable, lockout), `/api/app/pair` + QR on the page, `/api/app/devices`, ngrok policy header rule, `api` version in status; tests | Pairing QR appears on the laptop page; curl with the token passes, without it 401; revoke works |
| **2. App skeleton + pairing** | Compose app, Keystore storage, QR scan, status screen (limits, saved time, online/offline + Start link) | App shows live status from Kaggle after scanning the QR |
| **3. Voice loop** | Recorder (OGG/Opus, silence stop, Whisper mode), NDJSON streaming, sentence playback via ExoPlayer on media audio, interrupt by tap | Speak → reply on the loudspeaker / Bluetooth, first audio as fast as the PWA (≤ 3 s) |
| **4. Feature parity** | Link cards (Coil), YouTube app intent, price cards, Good/Bad on latest reply, Clear, notices, goodbye, 409 busy handling, no emoji | Everything in README "Things you can say" works in the app |
| **5. Reminders on the phone** | Fetch reminders → AlarmManager → notifications (channel, tap opens app); remove the ntfy push path or keep it optional | "Remind me in 10 minutes", close Jarvis → phone notification arrives |
| **6. Releases + self-update** | `android-release.yml`, public repo releases, in-app updater with SHA-256 + signer check | Tag `app-v0.2.0` → phone updates itself |
| **7. Remove the PWA** | Delete `sw.js`, `manifest.json`, icons/apple-touch, install/offline code in `index.html`; keep the plain laptop page (D2); update tests (`test_pwa.py`), README, CLAUDE.md | No service worker registered; laptop page still works |
| **8. Later** | Start Jarvis from the app (D6), home-screen widget/quick tile for tap-to-talk, washer helper inside the app (D7), wake word on the phone (battery!) | Owner's choice |

## 8. Risks and how they're handled

| Risk | Handling |
|---|---|
| Developer verification reaches India (2027) | Register the package + key in the free limited-distribution account (≤ 20 devices is plenty); fallback ADB/advanced flow |
| Payments profile might ask for a card | Check first (₹0 rule); if it does, stay unregistered until 2027 and decide then |
| Keystore lost | Keystore in GitHub secrets + password manager; losing it = reinstall, no server data lost |
| Token stolen from a lost phone | Revoke on the laptop page; tokens are per device, hashed on the server |
| ngrok header rule too open | The header only skips Google OAuth; the app server still requires a valid token; failures are rate-limited and logged as counts only |
| ngrok request budget (20k/month) | Status polling only while the app is visible (every 60 s, like the page); one request per turn |
| Kaggle can't run Tailscale (gotcha 27) | Not needed: the app uses the ngrok link on Kaggle; on a future home PC, the app can use the Tailscale address |
| GitHub API rate limit for update checks | 60/hour unauthenticated is plenty for one check per start + daily |

## 9. Files to create or change (expected)

- New: `android/` (Gradle project), `.github/workflows/android-release.yml`, `.github/workflows/android-ci.yml`,
  `src/devices.py` (device tokens), tests `tests/test_devices.py`.
- Change: `src/app.py` (token auth, pairing endpoints, `api` version), `src/tunnel.py`
  (header rule), `src/static/index.html` (pairing QR, then PWA removal), `.gitignore`
  (`*.jks`, `android/local.properties`, build outputs), README, CLAUDE.md, PROJECT_LOG.
- Remove in phase 7: `src/static/sw.js`, `src/static/manifest.json`, PWA icons, `tests/test_pwa.py`
  (replace with tests for the plain page).

---

## Appendix A: owner steps (click-by-click, when the phase needs them)

**A1. Public releases repo (phase 0).** github.com → **+** (top right) → **New repository** →
name `jarvis-app` → **Public** → tick "Add a README" → **Create repository**. (It will only ever
hold APK files and checksums, never code or secrets.)

**A2. Token for publishing releases (phase 0).** github.com → your photo → **Settings** →
**Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new
token** → name `jarvis app releases`, expiration 1 year → **Only select repositories** →
`jarvis-app` → Permissions → **Contents: Read and write** → **Generate** → copy it → in the
private repo: **Settings → Secrets and variables → Actions → New repository secret** →
name `APP_RELEASES_TOKEN`.

**A3. Signing key (phase 0).** Claude gives the exact commands for a portable JDK's `keytool`
on the laptop (no admin needed). Then add the secrets `ANDROID_KEYSTORE_B64`,
`ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD` the same way as A2,
and save the keystore file + passwords in your password manager. **Without them, future
updates can't be installed.**

**A4. First install on the phone (phase 0/2).** On the phone, open
`github.com/JatinMangla/jarvis-app/releases` → newest release → tap the `.apk` → Chrome asks
to allow "install unknown apps" → **Settings → Allow from this source** → back → **Install**.
Later versions install themselves.

**A5. Optional, before 2027: free limited-distribution account.** android.google.com/developerconsole
→ sign in (2-step verification must be on) → choose the **limited distribution** (free) account →
fill in the payments profile (name + address; **stop and tell Claude if it asks for a card**) →
register the app `io.github.jatinmangla.jarvis` with the signing-key fingerprint Claude gives you.

**A6. Pair the phone (phase 2).** On the laptop, open Jarvis → **Pair a phone** → in the app,
tap **Scan** and point the phone at the QR code.
