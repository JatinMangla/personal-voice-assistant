"""All settings in one place. Change values here, not in the other files."""
import os
from pathlib import Path

# --- Assistant personality ---------------------------------------------------
ASSISTANT_NAME = "Jarvis"
# True facts about how the assistant works, so it never guesses about itself.
# Update this if you move Jarvis to your own PC (then it can work on home Wi-Fi only).
ABOUT_ASSISTANT = (
    "How you work (true facts; never claim otherwise): You run on a cloud GPU server "
    "(Kaggle), not on the user's phone or laptop. The user's device talks to you over the "
    "internet through a private, secured link, so it needs Wi-Fi or mobile data; without "
    "internet you cannot hear or answer, and there is no offline mode. You run only after the "
    "user starts you, and you switch off after 10 minutes without use. You remember things "
    "between sessions in encrypted memory. You can search the internet only when a search "
    "tool is available. You cannot make calls, send messages, set alarms or reminders, or "
    "control devices unless a tool for it is available."
)
TIMEZONE = "Asia/Kolkata"

# --- Models ------------------------------------------------------------------
# Check https://ollama.com/library for newer tags before changing.
LLM_MODEL = os.environ.get("PVA_LLM_MODEL", "qwen3.5:9b")
LLM_FALLBACK_MODEL = "qwen3:8b"
LLM_CONTEXT_TOKENS = 4096
LLM_MAX_REPLY_TOKENS = 300

# Phase 5 fine-tuning (notebooks/finetune.ipynb). Qwen3-8B, not Qwen3.5: a tuned
# Qwen3.5 cannot currently be loaded into Ollama (its vision part is a separate file).
FINETUNE_BASE = "unsloth/Qwen3-8B-unsloth-bnb-4bit"
FINETUNE_MIN_GOOD_TURNS = 500  # 👍-rated replies needed before training is worth it
FINETUNE_EST_MIN = 90          # GPU minutes a training run may take (budget check)
TUNED_MARKER = "PVA_TUNED_MODEL.json"
TUNED_MODE = "auto"            # "auto" = blind A/B test then keep the winner; "base"; "tuned"
AB_MIN_RATINGS = 30            # ratings per model before "auto" picks the winner

WHISPER_MODEL = "large-v3-turbo"  # "small" or "medium" if you need more speed
WHISPER_LANGUAGES = ("en", "hi")  # only these are allowed; Urdu is mapped to Hindi

EMBED_MODEL = "intfloat/multilingual-e5-small"

# Internet search tool, free and without API keys (ddgs package). Google first; if it
# blocks the request, the next engines answer; Wikipedia is the last resort.
WEB_SEARCH = True  # False = the assistant has no internet access
SEARCH_BACKENDS = "google,bing,brave,duckduckgo"
SEARCH_REGION = "in-en"
SEARCH_RESULTS = 5

KOKORO_REPO = "hexgrad/Kokoro-82M"
# language code -> (Kokoro lang_code, voice)
TTS_VOICES = {
    "en": ("a", "af_heart"),
    "hi": ("h", "hf_alpha"),
}
TTS_SAMPLE_RATE = 24000
MP3_COMPRESSION = 0.5  # 0 = best quality/biggest, 1 = smallest

# --- GPUs (Kaggle "GPU T4 x2") -----------------------------------------------
# Ollama gets GPU 0; speech + embeddings get GPU 1 (or GPU 0 if only one exists).
OLLAMA_GPU = "0"
SPEECH_GPU_INDEX = 1

# --- Memory --------------------------------------------------------------
MEMORY_DATASET_SLUG = "assistant-memory"
MEMORY_FILE_NAME = "memory.db.enc"
MEMORY_TOP_K = 5
MEMORY_MIN_SIMILARITY = 0.80  # e5 scores are high overall; below this = unrelated
ALL_FACTS_LIMIT = 30          # facts always included in the prompt
RECENT_TURNS = 6              # last N turns of this session sent as chat history
FORGET_MIN_SIMILARITY = 0.85
# Learning facts from normal conversation (learn.py)
LEARN_FROM_CHAT = True
# In every prompt: allergies/diet, and your corrections of the assistant ("assistant").
ALWAYS_SHOWN_CATEGORIES = ("health", "diet", "assistant")
LEARN_KNOWN_FACTS = 5                # similar known facts shown, so changes replace them
LEARN_DUPLICATE_SIMILARITY = 0.93    # at or above this, a new fact counts as known
DELETE_OLD_DATASET_VERSIONS = True  # so "forget" also removes old copies

# --- Session ---------------------------------------------------------------------
IDLE_TIMEOUT_MIN = 10
AUTOSAVE_EVERY_MIN = 10
STATUS_POLL_SEC = 60    # how often the page checks status (each check = 1 ngrok request)
GOODBYE_GRACE_SEC = 70  # > STATUS_POLL_SEC, so the page hears the goodbye before shutdown

# --- Free-plan limits: warn at 90 %, stop at 99 %, never pay -----------------------
NOTIFY_AT = 0.90
STOP_AT = 0.99
# Kaggle: 30 GPU hours per week, shared out evenly over 7 days (~257 min/day).
# If you also run other GPU notebooks on Kaggle, lower GPU_WEEKLY_HOURS to leave room.
GPU_WEEKLY_HOURS = 30
DAILY_LIMIT_MIN = GPU_WEEKLY_HOURS * 60 / 7
# Kaggle also counts machine start-up before this code runs; add it to every session.
SESSION_OVERHEAD_MIN = 2
# Don't start if less than this is left before the auto-stop (setup alone takes ~2 min).
MIN_START_MIN = 5
# ngrok free plan, per month
NGROK_MONTHLY_REQUESTS = 20_000
NGROK_MONTHLY_BYTES = 1_000_000_000

# --- Paths / network -----------------------------------------------------------
WORK_DIR = Path("/tmp/pva")
BACKUP_PATH = Path("/kaggle/working/memory-backup.db.enc")  # ciphertext only
APP_HOST = "127.0.0.1"  # never 0.0.0.0: only the ngrok agent may reach the app
APP_PORT = 8000
OLLAMA_URL = "http://127.0.0.1:11434"

# Marker file used to find the attached model cache under /kaggle/input
MODELS_MARKER = "PVA_MODELS_CACHE.json"


def kaggle_credentials() -> tuple[str, dict[str, str]]:
    """(username, env for the kaggle CLI). Works with the new API token
    (KAGGLE_API_TOKEN, one string copied from Kaggle settings) or the legacy
    kaggle.json values (KAGGLE_KEY)."""
    username = get_secret("KAGGLE_USERNAME")
    env = {"KAGGLE_USERNAME": username}
    token = get_secret("KAGGLE_API_TOKEN", required=False)
    key = get_secret("KAGGLE_KEY", required=False)
    if token:
        env["KAGGLE_API_TOKEN"] = token.strip()
    elif key:
        env["KAGGLE_KEY"] = key.strip()
    else:
        raise RuntimeError("Missing secret 'KAGGLE_API_TOKEN' (your Kaggle API token). "
                           "Add it in Kaggle: Add-ons -> Secrets.")
    return username.strip(), env


def get_secret(name: str, required: bool = True) -> str | None:
    """Read a Kaggle Secret (or an environment variable when running locally)."""
    value = os.environ.get(name)
    if not value:
        try:
            from kaggle_secrets import UserSecretsClient
            value = UserSecretsClient().get_secret(name)
        except Exception:
            value = None
    if required and not value:
        raise RuntimeError(
            f"Missing secret '{name}'. Add it in Kaggle: Add-ons -> Secrets, "
            "and tick it for this notebook."
        )
    return value
