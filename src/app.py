"""The assistant's brain (one spoken turn at a time) and the web server for the page."""
import base64
import hashlib
import hmac
import json
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

import config
import learn
import llm
import tools
import websearch
from budget import (Budget, Meter, current_meters, describe_reset, minutes_until_stop,
                    usage_snapshot, usage_summary)
from intents import parse_command, parse_confirmation
from stt import AudioUnreadable

_URL = re.compile(r"(https?://|www\.)\S+")

STATIC_DIR = Path(__file__).parent / "static"

PHRASES = {
    "en": {
        "remembered": "Okay, I'll remember that.",
        "forgot": "Done, I've forgotten that.",
        "not_found": "I couldn't find that in my memory.",
        "notice": "Heads up: {pct}% of {label} is used.",
        "notice_left": " About {left} minutes left.",
        "error": "Sorry, something went wrong. Please try again.",
        "done": "Done.",
        "failed": "Sorry, that didn't work.",
        "cancelled": "Okay, I won't do it.",
        "bye_idle": "No activity for a while, so I'm closing now. Your memory is saved. Goodbye!",
        "bye_limit": "{label} is almost used up, so I'm stopping now to keep this free. "
                     "Your memory is saved. Goodbye!",
        "labels": {"day": "today's GPU time", "week": "this week's GPU time",
                   "requests": "this month's free link requests",
                   "data": "this month's free link data"},
    },
    "hi": {
        "remembered": "ठीक है, मैं यह याद रखूँगा।",
        "forgot": "हो गया, मैंने यह भुला दिया।",
        "not_found": "यह मेरी मेमोरी में नहीं मिला।",
        "notice": "ध्यान दें: {label} का {pct}% इस्तेमाल हो चुका है।",
        "notice_left": " लगभग {left} मिनट बचे हैं।",
        "error": "माफ़ कीजिए, कुछ गड़बड़ हो गई। फिर से कोशिश करें।",
        "done": "हो गया।",
        "failed": "माफ़ कीजिए, यह नहीं हो पाया।",
        "cancelled": "ठीक है, नहीं करूँगा।",
        "bye_idle": "काफ़ी देर से कोई बात नहीं हुई, इसलिए मैं बंद हो रहा हूँ। आपकी मेमोरी सेव है। अलविदा!",
        "bye_limit": "{label} लगभग ख़त्म है, इसलिए इसे मुफ़्त रखने के लिए मैं अब बंद हो रहा हूँ। "
                     "आपकी मेमोरी सेव है। अलविदा!",
        "labels": {"day": "आज का GPU समय", "week": "इस हफ़्ते का GPU समय",
                   "requests": "इस महीने की फ़्री लिंक रिक्वेस्ट",
                   "data": "इस महीने का फ़्री लिंक डेटा"},
    },
}


def notice_text(meter: Meter, lang: str, now: datetime | None = None) -> str:
    """e.g. 'Heads up: 95% of today's GPU time is used. About 11 minutes left. Resets at
    12:00 AM (midnight) India time, in 3 h 20 min.'"""
    p = PHRASES.get(lang, PHRASES["en"])
    text = p["notice"].format(pct=meter.percent, label=p["labels"][meter.name])
    left = minutes_until_stop(meter, config.STOP_AT)
    text += p["notice_left"].format(left=left) if left is not None else ""
    return f"{text} {describe_reset(meter, now or now_ist(), lang)}"


def limit_goodbye(meter: Meter, lang: str, now: datetime | None = None) -> str:
    p = PHRASES.get(lang, PHRASES["en"])
    label = p["labels"][meter.name]
    bye = p["bye_limit"].format(label=label[0].upper() + label[1:])
    return f"{bye} {describe_reset(meter, now or now_ist(), lang)}"


def size_report(nbytes: int, stats: dict) -> str:
    """e.g. '3.2 MB, 1,234 turns, 56 facts' plus a note when it is time to split the
    storage (docs/FUTURE_PLANS.md, section 2). Sizes and counts only."""
    text = f"{nbytes / 1e6:.1f} MB, {stats['turns']:,} turns, {stats['facts']:,} facts"
    if nbytes > config.SPLIT_STORAGE_AT_MB * 1e6 or stats["turns"] > config.SPLIT_STORAGE_AT_TURNS:
        text += "; memory is large: time to split storage (docs/FUTURE_PLANS.md section 2)"
    return text


def now_ist() -> datetime:
    return datetime.now(ZoneInfo(config.TIMEZONE))


def _line(**event) -> bytes:
    return (json.dumps(event, ensure_ascii=False) + "\n").encode()


class Assistant:
    def __init__(self, stt, tts, memory, store, timer, restart_ollama=None, picker=None):
        self.stt, self.tts, self.memory, self.store, self.timer = stt, tts, memory, store, timer
        self.restart_ollama = restart_ollama
        self.picker = picker or llm.ModelPicker(config.LLM_MODEL, None, {})
        self.start_url: str | None = None  # optional START_URL secret
        # Screen snapshot sent just before a question (screen sharing); used once,
        # never stored.
        self.pending_screen: tuple[bytes, float] | None = None
        self.backlearn_stopped = False
        self.last_reflect = time.time()
        self.learn_thread: threading.Thread | None = None
        self.backlearn_count = [0, 0]  # old conversations read, facts learned
        self.budget = Budget(config.NOTIFY_AT, config.STOP_AT)
        self.notices: queue.SimpleQueue = queue.SimpleQueue()  # 90 % warnings to speak
        self.server = None  # uvicorn.Server, set by main.py
        self.state = "ready"
        self.last_lang = "en"
        self.goodbye_audio: bytes | None = None
        self.save_ok = True
        self.turn_lock = threading.Lock()
        self.save_lock = threading.Lock()
        self.stopped = threading.Event()

    # --- one spoken turn -------------------------------------------------------
    def handle_turn(self, audio: bytes, quiet: bool = False, mime: str = ""):
        """Generator of NDJSON events: transcript, then sentence text + audio pairs.
        quiet = Whisper mode (soft voice): boost the audio and listen more sensitively.
        mime = the recording format the browser says it sent (for the log only)."""
        t0 = time.time()
        first_audio = None
        try:
            try:
                text, lang = self.stt.transcribe(audio, quiet=quiet)
            except AudioUnreadable as e:  # a bad recording from the phone: just ask again
                print(f"[turn] recording unreadable: {e}; browser said {mime[:40] or '?'}")
                yield _line(type="done", heard=False)
                return
            heard = time.time() - t0
            yield _line(type="transcript", text=text, lang=lang)
            if not text:
                yield _line(type="done", heard=False)
                return
            self.last_lang = lang
            phrases = PHRASES.get(lang, PHRASES["en"])
            now = now_ist()

            turn_id = None
            waiting = tools.waiting()  # an action that asked "Should I ...?" last turn
            answer = parse_confirmation(text) if waiting else None
            if waiting and answer is None:
                tools.cancel()  # the user moved on: that action is dropped
            command = None if answer else parse_command(text)
            if answer:
                if answer == "yes":
                    ok, _ = tools.confirm()
                    print(f"[tool] {waiting.name} confirmed by voice: {'done' if ok else 'failed'}")
                    reply = phrases["done"] if ok else phrases["failed"]
                else:
                    tools.cancel()
                    reply = phrases["cancelled"]
                yield from self._speak(reply, lang)
                turn_id = self.memory.add_turn(text, reply, lang, now)
            elif command:
                action, content = command
                if action == "remember":
                    self.memory.add_fact(content, now)
                    reply_parts = [phrases["remembered"]]
                else:
                    fact, turns = self.memory.forget(content, config.FORGET_MIN_SIMILARITY)
                    reply_parts = [phrases["forgot"] if (fact or turns) else phrases["not_found"]]
                for sentence in reply_parts:
                    yield from self._speak(sentence, lang)
            else:
                model = self.picker.pick()
                websearch.take_links(enrich=False)  # start this turn with no links
                screen = self._take_screen()
                reply_parts = []
                for sentence in self._llm_sentences(text, lang, now, model, screen):
                    reply_parts.append(sentence)
                    yield from self._speak(sentence, lang)
                    first_audio = first_audio or time.time() - t0
                links = websearch.take_links()
                if links:
                    yield _line(type="links", links=links)
                turn_id = self.memory.add_turn(text, " ".join(reply_parts), lang, now, model)
                # Timings only; never the words.
                print(f"[turn] {lang}{' +screen' if screen else ''}: heard in {heard:.1f}s, "
                      f"first audio at {first_audio or 0:.1f}s, done at {time.time() - t0:.1f}s "
                      f"({llm.speed_report()})")
                self._learn_later(text, now, turn_id)

            while not self.notices.empty():  # free-plan warnings, spoken once each
                yield from self._speak(notice_text(self.notices.get(), lang), lang)
            yield _line(type="done", heard=True, turn_id=turn_id)
        except Exception as e:
            print(f"[turn] error: {type(e).__name__}: {str(e)[:200]}")
            yield from self._speak(PHRASES.get(self.last_lang, PHRASES["en"])["error"],
                                   self.last_lang, ignore_errors=True)
            yield _line(type="done", heard=True, error=True)
        finally:
            self.timer.touch(now_ist())

    def _take_screen(self) -> str | None:
        """The screen snapshot sent for this question (base64), if fresh; used once."""
        pending, self.pending_screen = self.pending_screen, None
        if pending and time.time() - pending[1] <= config.SCREEN_MAX_AGE_SEC:
            return base64.b64encode(pending[0]).decode()
        return None

    def _llm_sentences(self, text: str, lang: str, now: datetime, model: str,
                       screen: str | None = None):
        history = self.memory.recent_turns(config.RECENT_TURNS)
        facts, related = self.memory.recall(text, now, exclude_recent_turns=config.RECENT_TURNS)
        usage = usage_summary(current_meters(self.memory, now), now, config.STOP_AT)
        messages = llm.build_messages(text, lang, facts, related, history, now, screen, usage)
        chunker = llm.SentenceChunker()
        for piece in llm.stream_reply(messages, model):
            yield from chunker.feed(piece)
        yield from chunker.flush()

    def _learn_later(self, text: str, now: datetime, turn_id: int | None = None) -> None:
        """Learn from what the user said in the background, so the next question is never
        blocked; memory happens quietly, without anything shown in the chat."""
        if config.LEARN_FROM_CHAT:
            self.learn_thread = threading.Thread(target=self._learn, args=(text, now, turn_id),
                                                 daemon=True)
            self.learn_thread.start()

    def _learn(self, text: str, now: datetime, turn_id: int | None = None) -> list[str]:
        """Pick up lasting facts from what the user said; never breaks anything."""
        try:
            learned = learn.learn(self.memory, text, now)
        except Exception as e:
            print(f"[learn] skipped: {type(e).__name__}: {str(e)[:120]}")
            return []
        if turn_id:
            self.memory.mark_learned(turn_id)
        if learned:
            print(f"[learn] {len(learned)} new fact(s)")  # count only, never the content
        return learned

    def backlearn_one(self) -> bool:
        """Read ONE old conversation for facts (older chats from before automatic
        learning existed). Returns False when there is nothing left to do."""
        if self.backlearn_stopped or not config.LEARN_FROM_CHAT:
            return False
        rows = self.memory.unlearned_turns(1)
        if not rows:
            if self.backlearn_count[0]:
                print(f"[learn] old conversations read: {self.backlearn_count[0]}, "
                      f"facts learned: {self.backlearn_count[1]}")
            self.backlearn_stopped = True
            return False
        turn_id, ts, user_text = rows[0]
        try:
            when = datetime.fromisoformat(ts) if ts else now_ist()
            self.backlearn_count[1] += len(learn.learn(self.memory, user_text, when))
        except Exception as e:  # Ollama busy/restarting: try again next session
            print(f"[learn] back-learning paused: {type(e).__name__}")
            self.backlearn_stopped = True
            return False
        self.memory.mark_learned(turn_id)
        self.backlearn_count[0] += 1
        return True

    def _speak(self, sentence: str, lang: str, ignore_errors: bool = False):
        spoken = _URL.sub("", sentence).strip()  # links are shown, never read out
        if not spoken:
            yield _line(type="sentence", text=sentence, audio=None)
            return
        try:
            wav = self.tts.synthesize(spoken, lang)
        except Exception:
            if not ignore_errors:
                raise
            wav = None
        yield _line(type="sentence", text=sentence,
                    audio=base64.b64encode(wav).decode() if wav else None)

    # --- status, saving, closing ---------------------------------------------
    def status(self) -> dict:
        now = now_ist()
        idle_left = max(0, config.IDLE_TIMEOUT_MIN * 60 - self.timer.idle_seconds(now))
        last = self.store.last_saved
        meters = current_meters(self.memory, now)
        return {
            "state": self.state,
            "name": config.ASSISTANT_NAME,
            "idle_left_sec": int(idle_left),
            "last_saved": datetime.fromtimestamp(last, ZoneInfo(config.TIMEZONE)).strftime("%H:%M")
                          if last else None,
            "save_ok": self.save_ok,
            "poll_sec": config.STATUS_POLL_SEC,
            "meters": [{"name": m.name, "used": m.used, "limit": m.limit, "percent": m.percent,
                        "resets": describe_reset(m, now)} for m in meters],
            "warnings": [notice_text(m, "en", now) for m in self.budget.warnings(meters)],
            "notify_pct": round(config.NOTIFY_AT * 100),
            "stop_pct": round(config.STOP_AT * 100),
            "start_url": self.start_url,  # the page's "Start Jarvis" button while offline
        }

    def count_net(self, requests: int, nbytes: int) -> None:
        """Track traffic through ngrok, to stay inside the free plan."""
        self.memory.add_net(now_ist().strftime("%Y-%m"), requests, nbytes)

    def save(self) -> bool:
        with self.save_lock:
            self.memory.dirty = False
            size = 0
            try:
                data = self.memory.to_bytes()
                size = len(data)
                ok = self.store.save(data, usage_snapshot(self.memory, now_ist()))
            except Exception as e:
                print(f"[memory] save error: {type(e).__name__}: {str(e)[:200]}")
                ok = False
            if not ok:
                self.memory.dirty = True
            self.save_ok = ok
            print(f"[memory] {'saved' if ok else 'SAVE FAILED (encrypted backup kept in output)'} "
                  f"at {now_ist():%H:%M} ({size_report(size, self.memory.stats())})")
            return ok

    def close(self, reason: str, meter: Meter | None = None) -> None:
        """Say goodbye, save memory and stop the server (which ends the notebook).
        reason is "idle" or "limit" (then `meter` is the limit that was reached)."""
        got_lock = self.turn_lock.acquire(timeout=90)  # let a reply in progress finish
        try:
            if self.state == "closing":
                return
            self.state = "closing"
        finally:
            if got_lock:
                self.turn_lock.release()
        print(f"[session] closing: {reason}" + (f" ({meter.name} at {meter.percent}%)" if meter else ""))
        if meter:
            text = limit_goodbye(meter, self.last_lang)
        else:
            text = PHRASES.get(self.last_lang, PHRASES["en"])["bye_idle"]
        self.reflect()  # last look for patterns in this session's messages
        self.memory.add_usage(*self.timer.tick(now_ist()))
        try:
            self.goodbye_audio = self.tts.synthesize(text, self.last_lang)
        except Exception:
            self.goodbye_audio = None
        self.save()
        time.sleep(config.GOODBYE_GRACE_SEC)  # the page polls, fetches and plays the goodbye
        self.stopped.set()
        if self.server is not None:
            self.server.should_exit = True

    def watchdog(self) -> None:
        """Background loop: usage clock, idle/limit checks, autosave, Ollama health."""
        last_save = time.time()
        last_health = time.time()
        while not self.stopped.is_set():
            time.sleep(5)
            now = now_ist()
            self.memory.add_usage(*self.timer.tick(now))
            if self.turn_lock.locked():
                self.timer.touch(now)  # a long reply is not "idle"

            meters = current_meters(self.memory, now)
            for meter in self.budget.new_notices(meters):
                print(f"[budget] {meter.name} at {meter.percent}% of the free limit")
                self.notices.put(meter)
            over = self.budget.over_stop(meters)
            if over:
                self.close("limit", over)  # blocks until the server is told to stop
                return
            if self.timer.idle_reached(now):
                self.close("idle")
                return
            if time.time() - last_save >= config.AUTOSAVE_EVERY_MIN * 60 and self.memory.dirty:
                last_save = time.time()
                self.save()
            if time.time() - last_health >= 30:
                last_health = time.time()
                self._check_ollama()
            # While you're quiet: learn facts from old conversations (one per tick), then
            # look for patterns across recent messages (every few minutes at most).
            learning = self.learn_thread is not None and self.learn_thread.is_alive()
            if (not self.turn_lock.locked() and not learning and self.state != "closing"
                    and self.timer.idle_seconds(now) >= config.BACKLEARN_AFTER_IDLE_SEC):
                if not self.backlearn_one() and \
                        time.time() - self.last_reflect >= config.REFLECT_EVERY_MIN * 60:
                    self.reflect()

    def reflect(self) -> None:
        """Store patterns (focus, habits, how the user likes help); never fails a session."""
        self.last_reflect = time.time()
        if not config.LEARN_FROM_CHAT:
            return
        try:
            found = learn.reflect(self.memory, now_ist())
            if found:
                print(f"[learn] {len(found)} pattern(s) noticed")  # count only
        except Exception as e:
            print(f"[learn] pattern review skipped: {type(e).__name__}")

    def _check_ollama(self) -> None:
        try:
            requests.get(f"{config.OLLAMA_URL}/api/version", timeout=5).raise_for_status()
        except requests.RequestException:
            print("[ollama] not responding, restarting")
            if self.restart_ollama:
                self.restart_ollama()


# --- optional password login (only when Google sign-in via ngrok is off) --------
class PasswordGate:
    COOKIE = "pva_session"
    MAX_FAILS = 5
    LOCK_SEC = 15 * 60
    VALID_DAYS = 30

    def __init__(self, password: str, secret: bytes):
        self.password = password
        self.secret = hashlib.sha256(b"pva-login-cookie" + secret).digest()
        self.fails = 0
        self.locked_until = 0.0

    def _sign(self, expires: int) -> str:
        sig = hmac.new(self.secret, str(expires).encode(), hashlib.sha256).hexdigest()
        return f"{expires}.{sig}"

    def valid(self, token: str | None) -> bool:
        try:
            expires, _ = token.split(".", 1)
            return int(expires) > time.time() and hmac.compare_digest(token, self._sign(int(expires)))
        except (AttributeError, ValueError):
            return False

    def try_login(self, password: str) -> str | None:
        if time.time() < self.locked_until:
            return None
        if hmac.compare_digest(password.encode(), self.password.encode()):
            self.fails = 0
            return self._sign(int(time.time()) + self.VALID_DAYS * 86400)
        self.fails += 1
        if self.fails >= self.MAX_FAILS:
            self.fails = 0
            self.locked_until = time.time() + self.LOCK_SEC
        return None


def create_app(assistant: Assistant, gate: PasswordGate | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    # App files with no personal data, readable before the password page (fallback mode).
    app_files = {"/manifest.json": "application/manifest+json",
                 "/sw.js": "text/javascript",
                 "/icon-192.png": "image/png", "/icon-512.png": "image/png",
                 "/apple-touch-icon.png": "image/png"}
    open_paths = {"/login", *app_files}

    @app.middleware("http")
    async def security(request: Request, call_next):
        if gate and request.url.path not in open_paths and not gate.valid(request.cookies.get(gate.COOKIE)):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "login required"}, status_code=401)
            return FileResponse(STATIC_DIR / "login.html")
        response = await call_next(request)
        # Streamed replies are counted inside /api/turn; everything else here.
        assistant.count_net(1, int(response.headers.get("content-length", 0)))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    for path, media_type in app_files.items():
        def serve(path=path, media_type=media_type):
            return FileResponse(STATIC_DIR / path.lstrip("/"), media_type=media_type)
        app.add_api_route(path, serve, methods=["GET"])

    @app.post("/login")
    async def login(request: Request):
        form = await request.json()
        token = gate.try_login(str(form.get("password", ""))) if gate else None
        if not token:
            return JSONResponse({"ok": False}, status_code=401)
        response = JSONResponse({"ok": True})
        response.set_cookie(gate.COOKIE, token, max_age=gate.VALID_DAYS * 86400,
                            httponly=True, secure=True, samesite="strict")
        return response

    @app.get("/api/status")
    def status():
        return assistant.status()

    @app.post("/api/ping")
    def ping():
        """The page was opened or the mic was tapped: counts as activity."""
        if assistant.state != "closing":
            assistant.timer.touch(now_ist())
        return {"ok": True}

    @app.post("/api/turn")
    async def turn(request: Request):
        audio = await request.body()
        if assistant.state == "closing":
            return JSONResponse({"error": "closing"}, status_code=409)
        if not audio or len(audio) > 10_000_000:
            return JSONResponse({"error": "bad audio"}, status_code=400)
        if not assistant.turn_lock.acquire(blocking=False):
            return JSONResponse({"error": "busy"}, status_code=409)

        # The turn runs in its own thread, so it always finishes (and saves the
        # turn and frees the lock) even if the phone disconnects midway.
        events: queue.Queue = queue.Queue()

        quiet = request.headers.get("x-quiet") == "1"  # Whisper mode on the page
        mime = request.headers.get("content-type", "")

        def worker():
            try:
                for event in assistant.handle_turn(audio, quiet, mime):
                    events.put(event)
            finally:
                events.put(None)
                assistant.turn_lock.release()

        threading.Thread(target=worker, daemon=True).start()

        def stream():
            sent = 0
            try:
                while (event := events.get()) is not None:
                    sent += len(event)
                    yield event
            finally:
                assistant.count_net(0, sent)

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.post("/api/screen")
    async def screen(request: Request):
        """A screenshot for the next question (screen sharing). Kept in memory only
        until that question, never saved."""
        image = await request.body()
        if len(image) > config.SCREEN_MAX_BYTES or not (
                image.startswith(b"\xff\xd8\xff") or image.startswith(b"\x89PNG")):
            return JSONResponse({"error": "JPEG or PNG up to 3 MB only"}, status_code=400)
        assistant.pending_screen = (image, time.time())
        return {"ok": True}

    @app.get("/api/facts")
    def facts():
        """What Jarvis knows about you (🧠 Memory panel)."""
        return {"facts": assistant.memory.list_facts()}

    @app.post("/api/facts/delete")
    async def delete_fact(request: Request):
        body = await request.json()
        try:
            return {"ok": assistant.memory.delete_fact(int(body["id"]))}
        except (KeyError, TypeError, ValueError):
            return JSONResponse({"error": "bad id"}, status_code=400)

    @app.post("/api/rate")
    async def rate(request: Request):
        """👍/👎 on a reply. 👍 replies become fine-tuning data (Phase 5)."""
        body = await request.json()
        try:
            ok = assistant.memory.rate_turn(int(body["turn_id"]), int(body["value"]))
        except (KeyError, TypeError, ValueError):
            return JSONResponse({"error": "bad rating"}, status_code=400)
        return {"ok": ok}

    @app.get("/api/goodbye")
    def goodbye():
        audio = assistant.goodbye_audio
        if not audio:
            return Response(status_code=404)
        return Response(audio, media_type="audio/wav" if audio[:4] == b"RIFF" else "audio/mpeg")

    return app
