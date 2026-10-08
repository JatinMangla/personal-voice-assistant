"""The Start Coder button (scripts/start_coder.py) and the matching check in start_jarvis.py."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


start_coder = load("start_coder")
sj = start_coder.sj          # the same start_jarvis module the script imported

SECRETS = {"KAGGLE_USERNAME": "jatinmangla123", "KAGGLE_API_TOKEN": "KGAT_x",
           "MEMORY_KEY": "a" * 43 + "=", "NGROK_AUTHTOKEN": "ngrok_x",
           "NGROK_DOMAIN": "volley-gnarly-constable.ngrok-free.dev", "CODER_KEY": "c" * 43 + "="}


@pytest.fixture
def env(monkeypatch):
    for name in start_coder.REQUIRED + start_coder.OPTIONAL + sj.OPTIONAL + [
            "CODER_NOTEBOOK", "JARVIS_NOTEBOOK", "KAGGLE_KEY", "CODER_FORCE_START", "JARVIS_FORCE"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sj.time, "sleep", lambda s: None)
    monkeypatch.setattr(start_coder.time, "sleep", lambda s: None)
    return monkeypatch


def result(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_metadata_is_private_gpu_internet_with_code_and_secrets_only(env):
    meta = start_coder.kernel_metadata(start_coder.settings())
    assert meta["id"] == "jatinmangla123/coder" and meta["code_file"] == "coder.ipynb"
    assert meta["is_private"] and meta["enable_gpu"] and meta["enable_internet"]
    assert meta["machine_shape"] == "NvidiaTeslaT4"
    assert meta["dataset_sources"] == ["jatinmangla123/assistant-code", "jatinmangla123/jarvis-secrets"]
    assert meta["kernel_sources"] == []


def test_missing_secrets_are_listed_with_help_for_the_key(env):
    env.setenv("CODER_KEY", "")
    env.setenv("NGROK_DOMAIN", "")
    with pytest.raises(SystemExit, match="NGROK_DOMAIN, CODER_KEY") as e:
        start_coder.collect_secrets()
    assert "make_key.py" in str(e.value)


def test_a_short_key_is_refused(env):
    env.setenv("CODER_KEY", "short")
    with pytest.raises(SystemExit, match="too short"):
        start_coder.collect_secrets()


def test_force_is_passed_on_to_the_notebook(env):
    assert "CODER_FORCE" not in start_coder.collect_secrets()
    assert start_coder.collect_secrets(force=True)["CODER_FORCE"] == "1"


def test_run_key_is_put_into_a_valid_notebook():
    nb = json.loads(start_coder.notebook_with_key("KEY123"))
    code = "".join(nb["cells"][1]["source"])
    assert 'RUN_KEY = "KEY123"' in code and 'RUN_KEY = ""' not in code
    assert "coder_main.run()" in code and all("id" in c for c in nb["cells"])


def fake_kaggle(statuses, calls):
    def kaggle(*args):
        calls.append(args)
        if args[:2] == ("kernels", "status"):
            return result(statuses.get(args[2], "", ) or "", 0 if args[2] in statuses else 1)
        if args[:2] == ("kernels", "push"):
            return result("Kernel version 1 successfully pushed.")
        return result()
    return kaggle


def run_main(env, statuses, extra=None):
    calls = []
    env.setattr(sj, "kaggle", fake_kaggle(statuses, calls))
    env.setattr(sj, "limit_blocker", lambda user, key: None)
    env.setattr(sj, "upload_secrets", lambda user, secrets: "RUNKEY")
    for name, value in (extra or {}).items():
        env.setenv(name, value)
    start_coder.main()
    return calls


def test_starts_when_nothing_else_runs(env, capsys):
    calls = run_main(env, {"jatinmangla123/coder": "COMPLETE", "jatinmangla123/jarvis": "COMPLETE"})
    assert ("kernels", "push") == next(c for c in calls if c[1] == "push")[:2]
    assert "Started" in capsys.readouterr().out


def test_refuses_while_jarvis_runs(env):
    with pytest.raises(SystemExit, match="Jarvis is running"):
        run_main(env, {"jatinmangla123/coder": "COMPLETE", "jatinmangla123/jarvis": 'status "RUNNING"'})


def test_force_starts_even_while_jarvis_runs(env):
    calls = run_main(env, {"jatinmangla123/jarvis": 'status "RUNNING"'}, {"CODER_FORCE_START": "true"})
    assert any(c[:2] == ("kernels", "push") for c in calls)


def test_refuses_when_the_coder_is_already_running(env):
    with pytest.raises(SystemExit, match="already running"):
        run_main(env, {"jatinmangla123/coder": 'status "QUEUED"'})


def test_a_used_up_free_limit_stops_before_anything_is_uploaded(env):
    env.setattr(sj, "kaggle", fake_kaggle({}, []))
    env.setattr(sj, "limit_blocker", lambda user, key: "NOT started: today's GPU time is at 99%")
    uploaded = []
    env.setattr(sj, "upload_secrets", lambda user, secrets: uploaded.append(1))
    with pytest.raises(SystemExit, match="NOT started"):
        start_coder.main()
    assert uploaded == []


# --- the matching check in the Jarvis button -------------------------------------------------
def test_start_jarvis_refuses_while_the_coder_runs(env):
    env.setenv("ALLOWED_EMAIL", "me@gmail.com")
    calls = []
    statuses = {"jatinmangla123/jarvis": "COMPLETE", "jatinmangla123/coder": 'status "RUNNING"'}
    env.setattr(sj, "kaggle", fake_kaggle(statuses, calls))
    env.setattr(sj, "limit_blocker", lambda user, key: None)
    with pytest.raises(SystemExit, match="Coder helper is running"):
        sj.main()
    assert not any(c[:2] == ("kernels", "push") for c in calls)


def test_start_jarvis_ignores_a_coder_that_does_not_exist_yet(env):
    env.setenv("ALLOWED_EMAIL", "me@gmail.com")
    calls = []
    env.setattr(sj, "kaggle", fake_kaggle({"jatinmangla123/jarvis": "COMPLETE"}, calls))
    env.setattr(sj, "limit_blocker", lambda user, key: None)
    env.setattr(sj, "upload_secrets", lambda user, secrets: "RUNKEY")
    sj.main()
    assert any(c[:2] == ("kernels", "push") for c in calls)
