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
        (0, "ready"),                                                            # secrets exist?
        (0, "Dataset version is being created."),                               # upload
        (0, "ready"),                                                            # processed
        (0, "An error occurred while committing kernel: ConcurrencyViolation"),  # push 1
        (0, "Kernel version 12 successfully pushed."),                          # push 2
    ])
    start_jarvis.main()
    assert [c[:2] for c in calls] == [("kernels", "status"), ("datasets", "status"),
                                      ("datasets", "version"), ("datasets", "status"),
                                      ("kernels", "push"), ("kernels", "push")]
    assert "--delete-old-versions" in calls[2]


def test_already_running_does_nothing(env):
    calls = fake_kaggle_calls(env, [(0, 'has status "KernelWorkerStatus.RUNNING"')])
    start_jarvis.main()
    assert len(calls) == 1


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
