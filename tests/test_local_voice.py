"""'Hey Jarvis' on the home PC: wake word -> record until a pause -> answer aloud."""
import base64
import json
import threading

import numpy as np

import local_voice
from local_voice import FRAME, VoiceLoop, record_question

WAKE = np.full(FRAME, 7, dtype=np.int16)  # the fake detector's "Hey Jarvis"


def noise(n):
    return [np.random.default_rng(i).integers(-30, 30, FRAME).astype(np.int16) for i in range(n)]


def speech(n):
    t = np.arange(FRAME)
    return [(8000 * np.sin(t / 5 + i)).astype(np.int16) for i in range(n)]


class FakeDetector:
    def __init__(self):
        self.resets = 0

    def score(self, frame):
        return 0.9 if np.array_equal(frame, WAKE) else 0.01

    def reset(self):
        self.resets += 1


class FakeMic:
    def __init__(self, frames):
        self.list = frames
        self.flushes = 0

    def frames(self):
        yield from self.list

    def flush(self):
        self.flushes += 1


class FakeSpeaker:
    def __init__(self):
        self.played, self.chimes = [], 0

    def play(self, audio):
        self.played.append(audio)

    def play_samples(self, samples, rate):
        self.chimes += 1


class FakeAssistant:
    def __init__(self):
        self.turn_lock = threading.Lock()
        self.stopped = threading.Event()
        self.heard = []

    def handle_turn(self, wav, quiet=False, mime=""):
        assert self.turn_lock.locked()  # a turn never overlaps one from the phone
        self.heard.append(wav)
        yield (json.dumps({"type": "transcript", "text": "hi"}) + "\n").encode()
        yield (json.dumps({"type": "sentence", "text": "Hello.",
                           "audio": base64.b64encode(b"mp3-1").decode()}) + "\n").encode()
        yield (json.dumps({"type": "done", "heard": True}) + "\n").encode()


def test_wake_word_question_and_spoken_answer():
    frames = noise(20) + [WAKE] + noise(3) + speech(15) + noise(20) + noise(10)
    assistant, speaker, detector = FakeAssistant(), FakeSpeaker(), FakeDetector()
    loop = VoiceLoop(assistant, detector, FakeMic(frames), speaker, threshold=0.5)
    loop.run()
    assert loop.wakes == 1 and speaker.chimes == 1
    assert len(assistant.heard) == 1 and assistant.heard[0][:4] == b"RIFF"
    assert speaker.played == [b"mp3-1"]
    assert detector.resets == 1 and not assistant.turn_lock.locked()


def test_no_wake_word_no_turn():
    assistant, speaker = FakeAssistant(), FakeSpeaker()
    VoiceLoop(assistant, FakeDetector(), FakeMic(noise(50) + speech(20)), speaker, 0.5).run()
    assert assistant.heard == [] and speaker.played == []


def test_wake_then_silence_is_ignored():
    assistant = FakeAssistant()
    VoiceLoop(assistant, FakeDetector(), FakeMic([WAKE] + noise(100)), FakeSpeaker(), 0.5).run()
    assert assistant.heard == []


def test_recording_stops_at_the_pause():
    frames = iter(noise(3) + speech(10) + noise(40) + speech(10))
    recorded = record_question(frames)
    # 3 noise + 10 speech + 15 silent frames (1.2 s pause), then it stops listening
    assert len(recorded) == (3 + 10 + 15) * FRAME
    assert len(list(frames)) == 25 + 10  # the rest is left for the wake-word detector


def test_off_unless_home(monkeypatch):
    import config
    monkeypatch.setattr(config, "HOME", False)
    assert local_voice.start(FakeAssistant()) is None


def test_missing_microphone_does_not_break_the_start(monkeypatch):
    import config
    monkeypatch.setattr(config, "HOME", True)
    monkeypatch.setattr(config, "WAKE_WORD", True)

    def no_package(name):
        raise ImportError("No module named 'openwakeword'")
    monkeypatch.setattr(local_voice, "OpenWakeWord", no_package)
    assert local_voice.start(FakeAssistant()) is None
