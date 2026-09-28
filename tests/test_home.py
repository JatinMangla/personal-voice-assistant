"""Home-PC mode (roadmap stage A): the same code behind one switch, PVA_HOSTING=home."""
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import budget
import config
import setup_home
from memory import Memory

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=IST)


def config_in(env: dict) -> dict:
    """config values as a fresh process sees them with these environment variables."""
    code = ("import json, config; print(json.dumps({'home': config.HOME, 'idle': "
            "config.IDLE_TIMEOUT_MIN, 'work': str(config.WORK_DIR), 'about': "
            "config.ABOUT_ASSISTANT[:160], 'overhead': config.SESSION_OVERHEAD_MIN}))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT / "src", capture_output=True,
                         text=True, env={**os.environ, **env}, check=True).stdout
    return json.loads(out)


def test_kaggle_is_the_default(tmp_path):
    c = config_in({"PVA_HOSTING": ""})
    assert not c["home"] and c["idle"] == 10 and "cloud GPU" in c["about"]


def test_home_switch(tmp_path):
    c = config_in({"PVA_HOSTING": "home", "PVA_HOME_DIR": str(tmp_path)})
    assert c["home"] and c["idle"] == 0 and c["overhead"] == 0
    assert Path(c["work"]) == tmp_path / "work"
    assert "own computer" in c["about"]


def test_home_has_no_gpu_meters(embedder, monkeypatch):
    monkeypatch.setattr(config, "HOME", True)
    monkeypatch.setattr(config, "IDLE_TIMEOUT_MIN", 0)
    m = Memory(embedder)
    monkeypatch.setattr(config, "NGROK_IN_USE", True)
    assert [x.name for x in budget.current_meters(m, NOW)] == ["requests", "data"]
    monkeypatch.setattr(config, "NGROK_IN_USE", False)  # this computer only: no limits at all
    assert budget.current_meters(m, NOW) == []
    assert budget.usage_summary([], NOW, 0.99) == ("No usage limits apply: you run on the "
                                                   "user's own computer.")


def test_cpu_pc_gets_the_small_models(monkeypatch, tmp_path):
    monkeypatch.delenv("PVA_LLM_MODEL", raising=False)
    assert setup_home.choose_llm(0) == config.HOME_CPU_LLM
    assert setup_home.choose_llm(1) == config.LLM_MODEL
    monkeypatch.setenv("PVA_LLM_MODEL", "llama3.2:3b")
    assert setup_home.choose_llm(0) == config.LLM_MODEL  # your own choice wins


def test_models_downloaded_once_then_used_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HOME_DIR", tmp_path)
    downloads = []

    def fake_download(folder, repo):
        downloads.append(repo)
        return str(folder / "whisper"), folder / "kokoro", str(folder / "embed")
    monkeypatch.setattr(setup_home, "_download_speech_models", fake_download)
    first = setup_home.model_paths(0)
    second = setup_home.model_paths(0)
    assert downloads == [config.WHISPER_REPO_CPU]  # the second start needed no internet
    assert first == second and first.device == "cpu"
    assert setup_home.model_paths(2).device == "cuda" and downloads[-1] == config.WHISPER_REPO


def test_secrets_file(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("run_home", ROOT / "scripts" / "run_home.py")
    run_home = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_home)
    (tmp_path / "secrets.env").write_text(
        "# comment\nMEMORY_KEY=abc\nNGROK_DOMAIN = \"x.ngrok-free.dev\"\nEMPTY=\nALREADY=file\n")
    for name in ("MEMORY_KEY", "NGROK_DOMAIN", "EMPTY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALREADY", "env")
    assert run_home.load_secrets_file(tmp_path / "secrets.env") == 2
    assert os.environ["MEMORY_KEY"] == "abc" and os.environ["NGROK_DOMAIN"] == "x.ngrok-free.dev"
    assert os.environ["ALREADY"] == "env"  # a real environment variable wins


def test_home_start_end_to_end_with_fakes(embedder, monkeypatch, tmp_path):
    """main.run() in home mode, with the models, Ollama and the web server faked: catches
    any Kaggle-only step left in the start-up path."""
    import types
    from concurrent.futures import Future

    from fastapi.testclient import TestClient

    import crypto
    import llm
    import main
    import memory as memory_module
    import stt as stt_module
    import tools
    import tts as tts_module
    from persistence import LocalMemoryStore

    key = crypto.generate_key()
    monkeypatch.setattr(config, "HOME", True)
    monkeypatch.setattr(config, "HOME_DIR", tmp_path)
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "IDLE_TIMEOUT_MIN", 0)
    monkeypatch.setattr(config, "_bundle_cache", {})
    monkeypatch.setattr(config, "NGROK_IN_USE", True)  # main.run() changes it: restore after
    for name in ("NGROK_AUTHTOKEN", "NGROK_DOMAIN", "ALLOWED_EMAIL", "APP_PASSWORD",
                 "NTFY_TOPIC", "KAGGLE_USERNAME", "KAGGLE_API_TOKEN", "PVA_RUN_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORY_KEY", key)
    monkeypatch.setattr(tools, "REGISTRY", {})

    job = Future()
    job.set_result("qwen3:4b")
    monkeypatch.setattr(setup_home, "prepare", lambda: (
        setup_home.ModelPaths("whisper", tmp_path, "embed", 0, "cpu"), job))
    monkeypatch.setattr(memory_module, "E5Embedder", lambda path, device: embedder)

    class FakeSTT:
        def __init__(self, path, index, device):
            assert device == "cpu"
            self.model = types.SimpleNamespace(transcribe=lambda audio, language: ([], None))

        def transcribe(self, audio, quiet=False):
            return "hello jarvis", "en"

    class FakeTTS:
        pipelines = {"en": None}

        def __init__(self, path, device):
            assert device == "cpu"

        def synthesize(self, text, lang):
            return b"RIFFfake"

    monkeypatch.setattr(stt_module, "SpeechToText", FakeSTT)
    monkeypatch.setattr(tts_module, "TextToSpeech", FakeTTS)
    monkeypatch.setattr(llm, "load_model", lambda model: None)
    monkeypatch.setattr(llm, "stream_reply", lambda messages, model=None, max_tokens=None: iter(["OK."]))

    replies = []

    class FakeServer:
        def __init__(self, cfg):
            self.app, self.should_exit = cfg.app, False

        def run(self):  # one real turn through the app, then stop
            client = TestClient(self.app)
            assert client.get("/api/status").json()["meters"] == []  # no limits at home
            replies.append(client.post("/api/turn", content=b"audio").text)

    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(
        Config=lambda app, **kw: types.SimpleNamespace(app=app, **kw), Server=FakeServer))

    main.run()
    assert '"OK."' in replies[0]
    saved = Memory(embedder, LocalMemoryStore(tmp_path, crypto.load_key(key)).load())
    assert saved.recent_turns(1) == [("hello jarvis", "OK.")]
