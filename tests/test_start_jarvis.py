import importlib.util
import json
from pathlib import Path

import pytest

import config
import crypto

spec = importlib.util.spec_from_file_location(
    "start_jarvis", Path(__file__).resolve().parent.parent / "scripts" / "start_jarvis.py")
start_jarvis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(start_jarvis)

SECRETS = {"KAGGLE_USERNAME": "jatinmangla123", "KAGGLE_API_TOKEN": "KGAT_x",
           "MEMORY_KEY": "a" * 43 + "=", "NGROK_AUTHTOKEN": "ngrok_x",
           "NGROK_DOMAIN": "volley-gnarly-constable.ngrok-free.dev",
           "ALLOWED_EMAIL": "me@gmail.com"}


@pytest.fixture
def env(monkeypatch):
    for name in start_jarvis.REQUIRED + start_jarvis.OPTIONAL + ["JARVIS_NOTEBOOK", "KAGGLE_KEY"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(start_jarvis.time, "sleep", lambda s: None)
    return monkeypatch


def test_metadata_runs_private_with_gpu_internet_and_inputs(env):
    env.setenv("JARVIS_EXTRA", "jarvis-finetune")
    meta = start_jarvis.kernel_metadata(start_jarvis.settings())
    assert meta["id"] == "jatinmangla123/jarvis"
    assert meta["is_private"] and meta["enable_gpu"] and meta["enable_internet"]
    assert meta["machine_shape"] == "NvidiaTeslaT4"
    assert meta["dataset_sources"] == ["jatinmangla123/assistant-code",
                                       "jatinmangla123/jarvis-secrets"]
    assert meta["kernel_sources"] == ["jatinmangla123/build-model-cache",
                                      "jatinmangla123/jarvis-finetune"]


def test_missing_token_stops_with_help(env):
    env.setenv("KAGGLE_API_TOKEN", "")  # what GitHub passes for a missing secret
    with pytest.raises(SystemExit, match="New repository secret"):
        start_jarvis.settings()


def test_missing_notebook_secrets_are_listed(env):
    env.setenv("MEMORY_KEY", "")
    env.setenv("ALLOWED_EMAIL", "")
    with pytest.raises(SystemExit, match="MEMORY_KEY, ALLOWED_EMAIL"):
        start_jarvis.collect_secrets()


def test_run_key_is_put_into_a_valid_notebook():
    nb = json.loads(start_jarvis.notebook_with_key("KEY123"))
    code = "".join(nb["cells"][1]["source"])
    assert 'RUN_KEY = "KEY123"' in code and 'RUN_KEY = ""' not in code
    assert all("id" in c for c in nb["cells"])


def test_notebook_opens_the_bundle_with_the_run_key(tmp_path, monkeypatch):
    run_key = crypto.generate_key()
    (tmp_path / "jarvis-secrets").mkdir()
    (tmp_path / "jarvis-secrets" / config.SECRETS_BUNDLE_FILE).write_bytes(
        crypto.encrypt(json.dumps({"NGROK_DOMAIN": "x.ngrok-free.dev"}).encode(),
                       crypto.load_key(run_key)))
    monkeypatch.setattr(config, "INPUT_ROOT", tmp_path)
    monkeypatch.setattr(config, "_bundle_cache", None)
    monkeypatch.delenv("NGROK_DOMAIN", raising=False)
    monkeypatch.setenv("PVA_RUN_KEY", run_key)
    assert config.get_secret("NGROK_DOMAIN") == "x.ngrok-free.dev"

    # A different run's key can't open it: falls back (here: nothing found).
    monkeypatch.setattr(config, "_bundle_cache", None)
    monkeypatch.setenv("PVA_RUN_KEY", crypto.generate_key())
    assert config.get_secret("NGROK_DOMAIN", required=False) is None
    monkeypatch.setattr(config, "_bundle_cache", None)


def fake_kaggle_calls(env, answers):
    calls = []

    def fake(*args):
        calls.append(args)
        code, out = answers.pop(0)
        return start_jarvis.subprocess.CompletedProcess(args, code, stdout=out, stderr="")

    env.setattr(start_jarvis, "kaggle", fake)
    return calls


def test_full_start_uploads_secrets_then_pushes_and_retries(env):
    calls = fake_kaggle_calls(env, [
        (0, 'jatinmangla123/jarvis has status "KernelWorkerStatus.COMPLETE"'),  # kernel status
        (1, "404 - Not Found"),                                    # the Coder helper: none yet
        (1, "404 - Not Found"),                                    # usage numbers (none yet)
        (0, "ready"),                                                            # secrets exist?
        (0, "Dataset version is being created."),                               # upload
        (0, "ready"),                                                            # processed
        (0, "An error occurred while committing kernel: ConcurrencyViolation"),  # push 1
        (0, "Kernel version 12 successfully pushed."),                          # push 2
    ])
    start_jarvis.main()
    assert [c[:2] for c in calls] == [("kernels", "status"), ("kernels", "status"),
                                      ("datasets", "download"), ("datasets", "status"),
                                      ("datasets", "version"), ("datasets", "status"),
                                      ("kernels", "push"), ("kernels", "push")]
    assert "--delete-old-versions" in calls[4]


def test_already_running_and_reachable_does_nothing(env):
    env.setattr(start_jarvis, "link_is_online", lambda domain: True)
    calls = fake_kaggle_calls(env, [(0, 'has status "KernelWorkerStatus.RUNNING"')])
    start_jarvis.main()
    assert len(calls) == 1


def test_running_but_link_offline_turns_red_with_advice(env):
    # e.g. an open Kaggle editor tab or a stuck run: before, this looked green.
    env.setattr(start_jarvis, "link_is_online", lambda domain: False)
    calls = fake_kaggle_calls(env, [(0, 'has status "KernelWorkerStatus.RUNNING"')])
    with pytest.raises(SystemExit, match="NOTHING was started"):
        start_jarvis.main()
    assert len(calls) == 1


def test_force_starts_even_if_kaggle_says_running(env):
    env.setenv("JARVIS_FORCE", "true")
    calls = fake_kaggle_calls(env, [
        (0, 'has status "KernelWorkerStatus.RUNNING"'), (1, "404"),
        (0, "ready"), (0, "Dataset version is being created."), (0, "ready"),
        (0, "Kernel version 13 successfully pushed.")])
    start_jarvis.main()
    assert calls[-1][:2] == ("kernels", "push")


def test_push_error_text_counts_as_failure():
    assert not start_jarvis.push_ok(0, "An error occurred while committing kernel: ...")
    assert start_jarvis.push_ok(0, "Kernel version 12 successfully pushed.")
    assert not start_jarvis.push_ok(1, "")


@pytest.mark.parametrize("output, running", [
    ('jatinmangla123/jarvis has status "KernelWorkerStatus.RUNNING"', True),
    ('jatinmangla123/jarvis has status "KernelWorkerStatus.QUEUED"', True),
    ('jatinmangla123/jarvis has status "KernelWorkerStatus.COMPLETE"', False),
    ('jatinmangla123/jarvis has status "KernelWorkerStatus.ERROR"', False),
])
def test_already_running_detection(output, running):
    assert start_jarvis.is_running(output) is running


# --- free-limit check before starting (no GPU time used) ------------------------------
IST = start_jarvis.IST
NOW = start_jarvis.datetime(2026, 9, 28, 21, 0, tzinfo=IST)


def usage_download(env, snapshot, key=None):
    """kaggle datasets download -f usage.json.enc, answered with this snapshot."""
    key = crypto.load_key(key or SECRETS["MEMORY_KEY"])
    calls = []

    def fake(*args):
        calls.append(args)
        if args[:2] == ("datasets", "download"):
            folder = Path(args[args.index("-p") + 1])
            (folder / config.USAGE_FILE_NAME).write_bytes(
                crypto.encrypt(json.dumps(snapshot).encode(), key))
            return start_jarvis.subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")
        raise AssertionError(f"unexpected kaggle call {args}")

    env.setattr(start_jarvis, "kaggle", fake)
    return calls


def test_day_limit_used_up_refuses_before_any_gpu_run(env):
    daily = config.DAILY_LIMIT_MIN * 60
    calls = usage_download(env, {"days": {"2026-09-28": daily * 0.97}, "net": {}})
    why = start_jarvis.limit_blocker("me", SECRETS["MEMORY_KEY"], NOW)
    assert "today's GPU time is at 9" in why and "12:00 AM" in why and "No GPU time" in why
    assert calls[0][:3] == ("datasets", "download", "me/assistant-memory")
    assert config.USAGE_FILE_NAME in calls[0]  # only the numbers file, never the memory


def test_week_limit_counts_the_last_7_days(env):
    days = {f"2026-09-{d:02d}": config.GPU_WEEKLY_HOURS * 3600 / 6 * 0.99 for d in range(22, 28)}
    usage_download(env, {"days": days, "net": {}})
    assert "7 days" in start_jarvis.limit_blocker("me", SECRETS["MEMORY_KEY"], NOW)


def test_plenty_left_allows_start(env):
    usage_download(env, {"days": {"2026-09-28": 600, "2026-09-20": 99999}, "net": {"2026-09": [50, 10]}})
    assert start_jarvis.limit_blocker("me", SECRETS["MEMORY_KEY"], NOW) is None


def test_link_requests_used_up_refuses(env):
    usage_download(env, {"days": {}, "net": {"2026-09": [19_900, 0]}})
    assert "link requests" in start_jarvis.limit_blocker("me", SECRETS["MEMORY_KEY"], NOW)


def test_unreadable_usage_lets_the_notebook_decide(env):
    usage_download(env, {"days": {}}, key=crypto.generate_key())  # other key: can't decrypt
    assert start_jarvis.limit_blocker("me", SECRETS["MEMORY_KEY"], NOW) is None


def test_main_stops_when_limit_is_used_up(env):
    env.setattr(start_jarvis, "limit_blocker", lambda user, key: "NOT started: used up.")
    calls = fake_kaggle_calls(env, [(0, 'has status "KernelWorkerStatus.COMPLETE"'), (1, "404")])
    with pytest.raises(SystemExit, match="NOT started"):
        start_jarvis.main()
    assert len(calls) == 2  # only the two status checks: nothing uploaded, nothing pushed
