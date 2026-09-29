import config
import setup_env

CPU_LINE = ('time=2026-09-27T13:29:52.637Z level=INFO source=types.go:50 msg="inference compute" '
            'id=cpu library=cpu compute="" name=cpu description=cpu libdirs=ollama driver="" '
            'total="30.0 GiB" available="18.7 GiB"')
GPU_LINE = ('time=2026-09-27T13:29:52.637Z level=INFO source=types.go:50 msg="inference compute" '
            'id=GPU-1 library=CUDA compute=7.5 name=CUDA0 description="Tesla T4" '
            'libdirs=ollama,cuda_v12 driver=12.4 total="15.0 GiB"')


def test_ollama_devices_reads_startup_log(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK_DIR", tmp_path)
    (tmp_path / "ollama.log").write_text(f"other line\n{CPU_LINE}\n{GPU_LINE}\n")
    assert setup_env.ollama_devices() == ["cpu: cpu", "cuda: Tesla T4"]


def test_ollama_devices_without_log(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK_DIR", tmp_path)
    assert setup_env.ollama_devices() == []


import pytest


@pytest.mark.parametrize("bridge_key,expected", [
    (None, ["ddgs"]),
    ("k" * 43, ["ddgs", "wsproto"]),  # never "websockets": Kaggle's runner uses it
])
def test_only_missing_packages_are_downloaded(tmp_path, monkeypatch, bridge_key, expected):
    monkeypatch.setattr(config, "_bundle_cache", {})
    if bridge_key:
        monkeypatch.setenv("BRIDGE_KEY", bridge_key)
    else:
        monkeypatch.delenv("BRIDGE_KEY", raising=False)
    (tmp_path / "wheels").mkdir()
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        offline = "--no-index" in cmd
        # the old cache has only the first packages: installing everything offline fails
        ok = not offline or all(p in setup_env.FIRST_CACHE_PACKAGES for p in cmd[cmd.index(str(tmp_path / "wheels")) + 1:])
        return setup_env.subprocess.CompletedProcess(cmd, 0 if ok else 1)
    monkeypatch.setattr(setup_env.subprocess, "run", run)
    setup_env.install_packages(tmp_path)
    online = [c for c in calls if "--no-index" not in c]
    assert len(online) == 1
    assert online[0][len(online[0]) - len(expected):] == expected  # not everything again
    assert "websockets" not in online[0]
