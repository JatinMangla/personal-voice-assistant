"""Text-to-speech with Kokoro (English + Hindi voices share one model)."""
import io
from pathlib import Path

import numpy as np

import config


class TextToSpeech:
    def __init__(self, kokoro_dir: Path, device: str):
        from kokoro import KModel, KPipeline
        model = KModel(repo_id=config.KOKORO_REPO,
                       config=str(kokoro_dir / "config.json"),
                       model=str(next(kokoro_dir.glob("kokoro-*.pth")))).to(device).eval()
        self.pipelines = {}
        self.voices = {}
        for lang, (lang_code, voice) in config.TTS_VOICES.items():
            try:
                self.pipelines[lang] = KPipeline(lang_code=lang_code, repo_id=config.KOKORO_REPO,
                                                 model=model, device=device)
                self.voices[lang] = str(kokoro_dir / "voices" / f"{voice}.pt")
            except Exception as e:  # e.g. Hindi needs espeak-ng
                print(f"[tts] {lang} voice unavailable ({type(e).__name__}: {e}); using English")
        if "en" not in self.pipelines:
            raise RuntimeError("English TTS voice failed to load.")

    def synthesize(self, text: str, lang: str) -> bytes:
        """Return an MP3 file (bytes) for one sentence."""
        lang = lang if lang in self.pipelines else "en"
        pieces = []
        for _, _, audio in self.pipelines[lang](text, voice=self.voices[lang]):
            if audio is not None:
                pieces.append(audio.detach().cpu().numpy() if hasattr(audio, "detach") else audio)
        samples = np.concatenate(pieces) if pieces else np.zeros(1200, dtype=np.float32)
        return encode_audio(samples)


def encode_audio(samples: np.ndarray) -> bytes:
    """MP3 is ~6x smaller than WAV, which keeps us inside ngrok's free 1 GB/month."""
    import soundfile as sf
    buf = io.BytesIO()
    try:
        sf.write(buf, samples, config.TTS_SAMPLE_RATE, format="MP3",
                 compression_level=config.MP3_COMPRESSION, bitrate_mode="VARIABLE")
    except Exception:  # very old libsndfile without MP3
        buf = io.BytesIO()
        sf.write(buf, samples, config.TTS_SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return buf.getvalue()
