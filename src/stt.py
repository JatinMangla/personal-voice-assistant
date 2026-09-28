"""Speech-to-text with faster-whisper. Only English and Hindi are allowed."""
import io

import config


class SpeechToText:
    def __init__(self, model_path: str, device_index: int, device: str = "cuda"):
        from faster_whisper import WhisperModel
        # int8 on a CPU (home PC without an NVIDIA GPU): several times faster there.
        self.model = WhisperModel(model_path, device=device, device_index=device_index,
                                  compute_type="float16" if device == "cuda" else "int8")

    def _pick_language(self, audio) -> str:
        """Detect the language, but only choose between the allowed ones.

        Whisper often labels spoken Hindi as Urdu, so Urdu counts as Hindi.
        """
        try:
            _, _, all_probs = self.model.detect_language(audio)
        except Exception:
            return "en"
        scores = {lang: 0.0 for lang in config.WHISPER_LANGUAGES}
        for lang, prob in all_probs:
            lang = "hi" if lang == "ur" else lang
            if lang in scores:
                scores[lang] += prob
        return max(scores, key=scores.get)

    def transcribe(self, audio_bytes: bytes, quiet: bool = False) -> tuple[str, str]:
        """Return (text, language). Text is "" when nothing was said.
        quiet = Whisper mode: a softer speech detector for low voices."""
        audio = normalize(decode(audio_bytes))
        if len(audio) < 16000 * 0.3:  # shorter than 0.3 s
            return "", "en"
        lang = self._pick_language(audio)
        vad = {"threshold": 0.25, "min_speech_duration_ms": 150} if quiet else None
        segments, _ = self.model.transcribe(audio, language=lang, beam_size=1, vad_filter=True,
                                            vad_parameters=vad,
                                            condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segments).strip()
        return text, lang


class AudioUnreadable(Exception):
    """The browser sent a recording that can't be decoded."""


def audio_kind(data: bytes) -> str:
    """File type from the first bytes (the browser decides the format)."""
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if data[:4] == b"OggS":
        return "ogg"
    if data[4:8] == b"ftyp":
        return "mp4"
    if data[:4] == b"RIFF":
        return "wav"
    return "unknown"


def decode(audio_bytes: bytes):
    """Browser recording -> 16 kHz mono samples. Retries as a named file (the decoder
    guesses formats better from a file name) before giving up."""
    import tempfile
    from faster_whisper.audio import decode_audio
    try:
        return decode_audio(io.BytesIO(audio_bytes), sampling_rate=16000)
    except Exception as first_error:
        kind = audio_kind(audio_bytes)
        suffix = {"mp4": ".m4a"}.get(kind, f".{kind}" if kind != "unknown" else ".webm")
        with tempfile.NamedTemporaryFile(suffix=suffix) as f:
            f.write(audio_bytes)
            f.flush()
            try:
                return decode_audio(f.name, sampling_rate=16000)
            except Exception:
                raise AudioUnreadable(f"{len(audio_bytes)} bytes, type {kind}, starts "
                                      f"{audio_bytes[:4].hex()} ({type(first_error).__name__})")


def normalize(audio, target_peak: float = 0.7, max_gain: float = 30.0):
    """Make soft recordings louder (up to 30x), so quiet or whispered speech is heard.
    Loud recordings are left as they are."""
    import numpy as np
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak < 1e-4 or peak >= target_peak:
        return audio
    return (audio * min(max_gain, target_peak / peak)).astype(np.float32)
