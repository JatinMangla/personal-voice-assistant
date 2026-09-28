"""Hands-free "Hey Jarvis" on the home PC (roadmap stage B).

Only for HOSTING = "home": the PC's own microphone listens for the wake word (openWakeWord,
running locally; no audio leaves the PC for it), then records your question until you
pause, answers it through the same turn logic as the web page, and plays the reply on
the PC's speakers. The mic is ignored while Jarvis speaks, so it never hears itself.

The phone/web page keeps working at the same time (one turn at a time).
Needs: pip install sounddevice openwakeword (in requirements-home.txt).
"""
import base64
import io
import json
import queue
import threading

import numpy as np

import config

RATE = 16000
FRAME = 1280  # 80 ms: openWakeWord's frame size


def rms(frame: np.ndarray) -> float:
    x = frame.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


def record_question(frames, first_noise: float = 0.01) -> np.ndarray | None:
    """Collect frames after the wake word until a pause (like the web page does).
    None if nothing was said."""
    noise, heard, speech_ms, silence_ms, elapsed = first_noise, False, 0, 0, 0
    kept = []
    for frame in frames:
        kept.append(frame)
        elapsed += 80
        level = rms(frame)
        if elapsed <= 240:  # learn the room's noise first
            noise = max(noise, level)
            continue
        if level > max(0.008, noise * 2.2):
            speech_ms, silence_ms = speech_ms + 80, 0
        else:
            silence_ms += 80
        heard = heard or speech_ms >= 200
        if (heard and silence_ms >= config.WAKE_PAUSE_MS) or (not heard and elapsed >= 6000) \
                or elapsed >= 20000:
            break
    return np.concatenate(kept) if heard and kept else None


def to_wav(samples: np.ndarray) -> bytes:
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, samples, RATE, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def chime() -> np.ndarray:
    """A short soft "I'm listening" tone (float, 24 kHz like the voice)."""
    t = np.linspace(0, 0.12, int(0.12 * 24000), endpoint=False)
    return (0.2 * np.sin(2 * np.pi * 880 * t) * np.hanning(len(t))).astype(np.float32)


class VoiceLoop:
    def __init__(self, assistant, detector, mic, speaker, threshold: float | None = None):
        self.assistant, self.detector, self.mic, self.speaker = assistant, detector, mic, speaker
        self.threshold = threshold if threshold is not None else config.WAKE_THRESHOLD
        self.stop = threading.Event()
        self.wakes = 0

    def run(self) -> None:
        frames = self.mic.frames()
        for frame in frames:
            if self.stop.is_set() or self.assistant.stopped.is_set():
                return
            if self.detector.score(frame) < self.threshold:
                continue
            self.wakes += 1
            print(f"[wake] wake word heard ({self.wakes} this session)")  # never the audio
            self.speaker.play_samples(chime(), 24000)
            self.mic.flush()
            question = record_question(frames)
            if question is not None:
                self.answer(to_wav(question))
            self.detector.reset()
            self.mic.flush()  # drop what the mic heard while Jarvis spoke

    def answer(self, wav: bytes) -> None:
        if not self.assistant.turn_lock.acquire(timeout=30):  # the phone is mid-question
            return
        try:
            for line in self.assistant.handle_turn(wav):
                event = json.loads(line)
                if event.get("type") == "sentence" and event.get("audio"):
                    self.speaker.play(base64.b64decode(event["audio"]))
        finally:
            self.assistant.turn_lock.release()


# --- real hardware ----------------------------------------------------------------------
class OpenWakeWord:
    def __init__(self, name: str):
        import openwakeword
        from openwakeword.model import Model
        try:
            openwakeword.utils.download_models(model_names=[name])  # first run only
        except Exception:
            pass
        self.model = Model(wakeword_models=[name], inference_framework="onnx")

    def score(self, frame: np.ndarray) -> float:
        return max(self.model.predict(frame).values(), default=0.0)

    def reset(self) -> None:
        self.model.reset()


class Microphone:
    def __init__(self):
        import sounddevice as sd
        self.queue: queue.Queue = queue.Queue(maxsize=500)
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                                     blocksize=FRAME, callback=self._callback)
        self.stream.start()

    def _callback(self, data, frames, time_info, status):
        try:
            self.queue.put_nowait(data[:, 0].copy())
        except queue.Full:
            pass

    def frames(self):
        while True:
            yield self.queue.get()

    def flush(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()


class Speaker:
    def __init__(self):
        import sounddevice as sd
        self.sd = sd

    def play_samples(self, samples: np.ndarray, rate: int) -> None:
        self.sd.play(samples, rate)
        self.sd.wait()

    def play(self, audio: bytes) -> None:
        import soundfile as sf
        samples, rate = sf.read(io.BytesIO(audio), dtype="float32")
        self.play_samples(samples, rate)


def start(assistant) -> VoiceLoop | None:
    """Start listening for the wake word in the background; never blocks the start."""
    if not (config.HOME and config.WAKE_WORD):
        return None
    try:
        loop = VoiceLoop(assistant, OpenWakeWord(config.WAKE_WORD_MODEL), Microphone(), Speaker())
    except Exception as e:  # no mic, or the packages are missing
        print(f"[wake] off: {type(e).__name__}: {str(e)[:120]}")
        return None
    threading.Thread(target=loop.run, daemon=True).start()
    print('[wake] say "Hey Jarvis" to talk hands-free on this computer')
    return loop
