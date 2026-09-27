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

    def transcribe(self, audio_bytes: bytes) -> tuple[str, str]:
        """Return (text, language). Text is "" when nothing was said."""
        from faster_whisper.audio import decode_audio
        audio = decode_audio(io.BytesIO(audio_bytes), sampling_rate=16000)
        if len(audio) < 16000 * 0.3:  # shorter than 0.3 s
            return "", "en"
        lang = self._pick_language(audio)
        segments, _ = self.model.transcribe(audio, language=lang, beam_size=1,
                                            vad_filter=True, condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segments).strip()
        return text, lang
