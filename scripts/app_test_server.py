"""Local test server for the Android app: the real src/app.py with fake speech/LLM parts.

    .venv/Scripts/python -m pip install uvicorn
    .venv/Scripts/python scripts/app_test_server.py token.txt
    then in android/: JARVIS_IT_URL=http://127.0.0.1:8765 JARVIS_IT_TOKEN=<token.txt contents>
    ./gradlew testDebugUnitTest --tests '*ServerContractTest*'

The token is written to the file given (a paired test device); nothing real is used.
"""
import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from datetime import datetime, timedelta, timezone
import uvicorn
import app as app_module
from conftest import FakeEmbedder
from memory import Memory
from timer import SessionTimer
import reminders as rem
IST = timezone(timedelta(hours=5, minutes=30))

class STT:
    def transcribe(self, audio, quiet=False):
        assert audio[:4] == b"OggS", audio[:4]
        return ("hello jarvis quiet" if quiet else "hello jarvis"), "en"
class TTS:
    def synthesize(self, text, lang): return b"ID3fake-mp3-" + text.encode()
class Store:
    last_saved = None
    def save(self, *a, **k): return True
app_module.llm.stream_reply = lambda messages, model=None: iter(["Hello there.", " Here you go."])
app_module.websearch.take_links = lambda enrich=True: [{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "title": "Song", "kind": "video", "video_id": "dQw4w9WgXcQ", "site": "youtube.com", "note": ""}] if enrich else []
app_module.Assistant._learn_later = lambda *a, **k: None
memory = Memory(FakeEmbedder())
reminders = rem.Reminders(memory)
now = datetime.now(IST)
reminders.add("call mom", now, (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"))
a = app_module.Assistant(STT(), TTS(), memory, Store(), SessionTimer(now, 600), reminders=reminders)
a.start_url = "https://github.com/JatinMangla/personal-voice-assistant/actions"
token = a.devices.mint("IT phone", now)
open(sys.argv[1], "w").write(token)
uvicorn.run(app_module.create_app(a), host="127.0.0.1", port=8765, log_level="warning")
