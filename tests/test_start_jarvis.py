import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "start_jarvis", Path(__file__).resolve().parent.parent / "scripts" / "start_jarvis.py")
start_jarvis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(start_jarvis)


def test_metadata_runs_private_with_gpu_internet_and_inputs(monkeypatch):
    monkeypatch.setenv("KAGGLE_USERNAME", "jatinmangla123")
    monkeypatch.setenv("KAGGLE_API_TOKEN", "KGAT_x")
    monkeypatch.delenv("JARVIS_NOTEBOOK", raising=False)
    monkeypatch.setenv("JARVIS_EXTRA", "jarvis-finetune")
    meta = start_jarvis.kernel_metadata(start_jarvis.settings())
    assert meta["id"] == "jatinmangla123/jarvis"
    assert meta["is_private"] and meta["enable_gpu"] and meta["enable_internet"]
    assert meta["machine_shape"] == "NvidiaTeslaT4"
    assert meta["dataset_sources"] == ["jatinmangla123/assistant-code"]
    assert meta["kernel_sources"] == ["jatinmangla123/build-model-cache",
                                      "jatinmangla123/jarvis-finetune"]
    assert (start_jarvis.ROOT / "notebooks" / meta["code_file"]).exists()


def test_missing_username_stops(monkeypatch):
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    with pytest.raises(SystemExit):
        start_jarvis.settings()


def test_missing_token_stops_with_help(monkeypatch):
    # What GitHub does when the repository secret isn't set: an empty value.
    monkeypatch.setenv("KAGGLE_USERNAME", "jatinmangla123")
    monkeypatch.setenv("KAGGLE_API_TOKEN", "")
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    with pytest.raises(SystemExit, match="New repository secret"):
        start_jarvis.settings()


def test_push_retries_after_concurrency_violation(monkeypatch, tmp_path):
    monkeypatch.setenv("KAGGLE_USERNAME", "jatinmangla123")
    monkeypatch.setenv("KAGGLE_API_TOKEN", "KGAT_x")
    monkeypatch.setattr(start_jarvis.time, "sleep", lambda s: None)
    answers = [
        (0, 'jatinmangla123/jarvis has status "KernelWorkerStatus.COMPLETE"'),  # status
        (0, "An error occurred while committing kernel: ConcurrencyViolation Sequence "
            "number must match Draft record"),                                  # push 1
        (0, "Kernel version 12 successfully pushed."),                          # push 2
    ]
    calls = []

    def fake_kaggle(*args):
        calls.append(args)
        code, out = answers.pop(0)
        return start_jarvis.subprocess.CompletedProcess(args, code, stdout=out, stderr="")

    monkeypatch.setattr(start_jarvis, "kaggle", fake_kaggle)
    start_jarvis.main()  # no SystemExit: the retry worked
    assert [c[:2] for c in calls] == [("kernels", "status"), ("kernels", "push"),
                                      ("kernels", "push")]


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
