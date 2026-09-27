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
