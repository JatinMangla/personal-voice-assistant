"""Unreadable recordings from the phone: retried, then 'tap again' (no spoken error)."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app as app_module
import stt
from memory import Memory
from timer import SessionTimer

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.mark.parametrize("data, kind", [
    (b"\x1a\x45\xdf\xa3rest", "webm"), (b"OggSrest", "ogg"),
    (b"\x00\x00\x00\x20ftypM4A ", "mp4"), (b"RIFF....WAVE", "wav"), (b"garbage!", "unknown")])
def test_audio_kind(data, kind):
    assert stt.audio_kind(data) == kind


def test_garbage_recording_is_reported_with_size_and_type_only():
    pytest.importorskip("faster_whisper")  # the speech library is only installed on Kaggle
    with pytest.raises(stt.AudioUnreadable) as info:
        stt.decode(b"\x1a\x45\xdf\xa3" + b"\x00" * 300)
    message = str(info.value)
    assert "304 bytes" in message and "type webm" in message


def test_unreadable_recording_asks_to_tap_again_without_speaking_an_error(embedder):
    class BadSTT:
        def transcribe(self, audio, quiet=False):
            raise stt.AudioUnreadable("12 bytes, type unknown")

    class TTS:
        pipelines = {"en": None}
        spoken = []

        def synthesize(self, text, lang):
            self.spoken.append(text)
            return b"RIFF"

    class Store:
        last_saved = None

    tts = TTS()
    a = app_module.Assistant(BadSTT(), tts, Memory(embedder), Store(),
                             SessionTimer(datetime.now(IST), 600))
    body = TestClient(app_module.create_app(a)).post("/api/turn", content=b"x").text
    events = [json.loads(l) for l in body.splitlines() if l.strip()]
    assert events == [{"type": "done", "heard": False}]
    assert tts.spoken == []
