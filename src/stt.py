"""Speech-to-text with faster-whisper. Only English and Hindi are allowed."""
import io

import config


class SpeechToText:
    def __init__(self, model_path: str, device_index: int):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(model_path, device="cuda", device_index=device_index,
                                  compute_type="float16")

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
        from faster_whisper.audio import decode_audio
        audio = normalize(decode_audio(io.BytesIO(audio_bytes), sampling_rate=16000))
        if len(audio) < 16000 * 0.3:  # shorter than 0.3 s
            return "", "en"
        lang = self._pick_language(audio)
        vad = {"threshold": 0.25, "min_speech_duration_ms": 150} if quiet else None
        segments, _ = self.model.transcribe(audio, language=lang, beam_size=1, vad_filter=True,
                                            vad_parameters=vad,
                                            condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segments).strip()
        return text, lang


def normalize(audio, target_peak: float = 0.7, max_gain: float = 30.0):
    """Make soft recordings louder (up to 30x), so quiet or whispered speech is heard.
    Loud recordings are left as they are."""
    import numpy as np
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak < 1e-4 or peak >= target_peak:
        return audio
    return (audio * min(max_gain, target_peak / peak)).astype(np.float32)
