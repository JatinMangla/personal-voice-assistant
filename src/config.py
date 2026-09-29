"""All settings in one place. Change values here, not in the other files."""
import os
from pathlib import Path

# --- Where Jarvis runs ---------------------------------------------------------------
# "kaggle": the free cloud GPU (default). "home": your own PC (roadmap stage A), started
# with scripts/run_home.py: memory in an encrypted file, no GPU limits, always on.
HOSTING = os.environ.get("PVA_HOSTING", "kaggle").strip().lower()
HOME = HOSTING == "home"

# --- Assistant personality ---------------------------------------------------
ASSISTANT_NAME = "Jarvis"
# True facts about how the assistant works, so it never guesses about itself.
# Update this if you move Jarvis to your own PC (then it can work on home Wi-Fi only).
ABOUT_ASSISTANT = (
    "How you work (true facts; never claim otherwise): You run on a cloud GPU server "
    "(Kaggle), not on the user's phone or laptop. The user's device talks to you over the "
    "internet through a private, secured link, so it needs Wi-Fi or mobile data; without "
    "internet you cannot hear or answer, and there is no offline mode. You run only after the "
    "user starts you, and you switch off after 10 minutes without use. You DO remember the "
    "user across sessions: facts they told you (listed below, when you know any) and related "
    "earlier conversations, kept in encrypted memory. You can search the internet only when a search "
    "tool is available. You can keep reminders, to-dos and bills with your reminder tools, but "
    "you cannot ring an alarm while switched off. You cannot make calls, send messages, or "
    "control devices unless a tool for it is available."
)
TIMEZONE = "Asia/Kolkata"

# --- Models ------------------------------------------------------------------
# Check https://ollama.com/library for newer tags before changing.
LLM_MODEL = os.environ.get("PVA_LLM_MODEL", "qwen3.5:9b")
LLM_FALLBACK_MODEL = "qwen3:8b"
# Same value in EVERY Ollama request: a different num_ctx makes Ollama reload the whole
# model (~40 s). 8192 leaves room for a screen snapshot.
LLM_CONTEXT_TOKENS = 8192
LLM_MAX_REPLY_TOKENS = 300
# Experiment switch (default off): flash attention on the T4. Compare the "prompt ... in"
# and "tokens/s" numbers of the [turn] lines with it on and off before keeping it.
OLLAMA_FLASH_ATTENTION = False

# Phase 5 fine-tuning (notebooks/finetune.ipynb). Qwen3-8B, not Qwen3.5: a tuned
# Qwen3.5 cannot currently be loaded into Ollama (its vision part is a separate file).
FINETUNE_BASE = "unsloth/Qwen3-8B-unsloth-bnb-4bit"
FINETUNE_MIN_GOOD_TURNS = 500  # 👍-rated replies needed before training is worth it
FINETUNE_EST_MIN = 90          # GPU minutes a training run may take (budget check)
TUNED_MARKER = "PVA_TUNED_MODEL.json"
TUNED_MODE = "auto"            # "auto" = blind A/B test then keep the winner; "base"; "tuned"
AB_MIN_RATINGS = 30            # ratings per model before "auto" picks the winner

WHISPER_MODEL = "large-v3-turbo"  # "small" or "medium" if you need more speed
WHISPER_REPO = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
WHISPER_REPO_CPU = "Systran/faster-whisper-small"  # home PC without an NVIDIA GPU
WHISPER_LANGUAGES = ("en", "hi")  # only these are allowed; Urdu is mapped to Hindi

EMBED_MODEL = "intfloat/multilingual-e5-small"

# Internet search tool, free and without API keys (ddgs package). Google first; if it
# blocks the request, the next engines answer; Wikipedia is the last resort.
WEB_SEARCH = True  # False = the assistant has no internet access
SEARCH_BACKENDS = "google,bing,brave,duckduckgo"
SEARCH_REGION = "in-en"
SEARCH_RESULTS = 5

# Screen sharing (laptop/desktop browsers): one snapshot per question, never stored.
SCREEN_MAX_BYTES = 3_000_000
SCREEN_MAX_AGE_SEC = 90  # a snapshot older than this is not used

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
USAGE_FILE_NAME = "usage.json.enc"  # usage numbers only, for the Start button's check
MEMORY_TOP_K = 5
MEMORY_MIN_SIMILARITY = 0.80  # e5 scores are high overall; below this = unrelated
ALL_FACTS_LIMIT = 50          # max facts in the prompt (health/diet/corrections always)
RECENT_TURNS = 6              # last N turns of this session sent as chat history
FORGET_MIN_SIMILARITY = 0.85
# Learning facts from normal conversation (learn.py)
LEARN_FROM_CHAT = True
# In every prompt, however many there are: allergies/diet and your corrections of the
# assistant ("assistant") - safety first - plus everything you asked it to remember.
ALWAYS_SHOWN_CATEGORIES = ("health", "diet", "assistant")
# Shown next, within ALL_FACTS_LIMIT: your preferences and the patterns it noticed (what
# you focus on, how you like help). Capped, so hundreds of them can't bloat every prompt.
PREFERRED_CATEGORIES = ("preference", "pattern")
LEARN_KNOWN_FACTS = 5                # similar known facts shown, so changes replace them
LEARN_DUPLICATE_SIMILARITY = 0.93    # at or above this, a new fact counts as known
BACKLEARN_AFTER_IDLE_SEC = 15        # read old conversations for facts while you're quiet
# Patterns (what you focus on, habits, how you like help): looked for over your recent
# messages while you're quiet and when a session ends.
REFLECT_MIN_MESSAGES = 6
REFLECT_MAX_MESSAGES = 40
REFLECT_EVERY_MIN = 5
DELETE_OLD_DATASET_VERSIONS = True  # so "forget" also removes old copies
# The whole memory is uploaded at every save. Past these sizes, split it into a small
# "hot" file plus monthly archives (docs/FUTURE_PLANS.md, section 2); the log says when.
SPLIT_STORAGE_AT_MB = 50
SPLIT_STORAGE_AT_TURNS = 20_000

# --- Reminders (reminders.py) ---------------------------------------------------------
# Optional phone notifications at the due time via ntfy (free): set the NTFY_TOPIC secret
# to a long random name and subscribe to it in the ntfy web app (ntfy.sh/app) or app.
NTFY_SERVER = "https://ntfy.sh"
NTFY_INCLUDE_TEXT = False  # False: the notification says only "You have a reminder"

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
# Tailscale (tailnet.py): a second local port only `tailscale serve` talks to, so the app
# knows a request came through Tailscale (and can trust its identity header).
TAILSCALE_PORT = 8001
TAILSCALE_HOSTNAME = "jarvis"          # -> https://jarvis.<your-tailnet>.ts.net
TAILSCALE_STATE_FILE = "tailscale.tar.enc"  # saved (encrypted) next to the memory
OLLAMA_URL = "http://127.0.0.1:11434"

# Marker file used to find the attached model cache under /kaggle/input
MODELS_MARKER = "PVA_MODELS_CACHE.json"
# Download Ollama and the models from the internet first, and use the model cache only
# if that fails. Measured on Kaggle: the internet is fast and steady, while reading the
# cache (/kaggle/input) varied from 48 s to 320 s for the LLM alone.
DOWNLOAD_FIRST = True
# If Jarvis isn't ready this long after the run starts, it stops itself (saves GPU hours).
STARTUP_TIMEOUT_MIN = 20


# --- Home PC (HOSTING = "home") ---------------------------------------------------------
HOME_DIR = Path(os.environ.get("PVA_HOME_DIR") or Path.home() / ".jarvis")
# "Hey Jarvis" on the home PC's own microphone and speakers (local_voice.py)
WAKE_WORD = True
WAKE_WORD_MODEL = "hey_jarvis"
WAKE_THRESHOLD = 0.5     # openWakeWord's recommended value; higher = fewer false wakes
WAKE_PAUSE_MS = 1200     # a pause this long ends the question
HOME_CPU_LLM = "qwen3:4b"  # used when the PC has no NVIDIA GPU (and PVA_LLM_MODEL is unset)
NGROK_IN_USE = True        # main.py sets False at home without a tunnel (local use only)
ABOUT_ASSISTANT_HOME = (
    "How you work (true facts; never claim otherwise): You run on the user's own computer at "
    "home, not in the cloud. The user's phone reaches you through a private, secured link, or "
    "they use you on that computer itself. You are on while that computer runs Jarvis. You DO "
    "remember the user across sessions: facts they told you (listed below, when you know any) "
    "and related earlier conversations, kept in encrypted memory on that computer. You can "
    "search the internet only when a search tool is available. You can keep reminders, to-dos "
    "and bills with your reminder tools. You cannot make calls, send messages, or control "
    "devices unless a tool for it is available."
)
if HOME:
    WORK_DIR = HOME_DIR / "work"
    BACKUP_PATH = HOME_DIR / "memory.db.enc.prev"
    IDLE_TIMEOUT_MIN = 0       # always on: no GPU hours to save
    SESSION_OVERHEAD_MIN = 0
    ABOUT_ASSISTANT = ABOUT_ASSISTANT_HOME


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


# Runs started by the GitHub "Start Jarvis" button get no Kaggle Secrets (a Kaggle
# limitation). The button uploads them encrypted with a one-time key instead: the
# encrypted file comes as the private "jarvis-secrets" dataset, the key inside the
# notebook it starts (PVA_RUN_KEY). Either part alone reveals nothing.
INPUT_ROOT = Path("/kaggle/input")
SECRETS_BUNDLE_FILE = "jarvis-secrets.enc"
_bundle_cache: dict | None = None


def _secrets_bundle() -> dict:
    global _bundle_cache
    if _bundle_cache is None:
        _bundle_cache = {}
        run_key = os.environ.get("PVA_RUN_KEY")
        files = list(INPUT_ROOT.glob(f"**/{SECRETS_BUNDLE_FILE}")) if run_key else []
        if files:
            import json
            import crypto
            try:
                _bundle_cache = json.loads(crypto.decrypt(files[0].read_bytes(),
                                                          crypto.load_key(run_key)))
            except Exception:
                print("[secrets] the Start button's encrypted secrets don't match this run "
                      "(an older upload); using Kaggle Secrets instead.")
    return _bundle_cache


def get_secret(name: str, required: bool = True) -> str | None:
    """Read a secret: environment variable, the Start button's encrypted bundle, or
    Kaggle Secrets (when started from the Kaggle editor)."""
    value = os.environ.get(name) or _secrets_bundle().get(name)
    if not value:
        try:
            from kaggle_secrets import UserSecretsClient
            value = UserSecretsClient().get_secret(name)
        except Exception:
            value = None
    if required and not value:
        raise RuntimeError(
            f"Missing secret '{name}'. Starting from the Kaggle editor: Add-ons -> Secrets, "
            "and tick it for this notebook. Starting with the GitHub button: add it as a "
            "GitHub repository secret (Settings -> Secrets and variables -> Actions)."
        )
    return value
