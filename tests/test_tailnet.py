"""Tailscale access: same device every run, HTTPS help, identity check on its own port."""
import io
import json
import subprocess
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import config
import crypto
import tailnet
from memory import Memory
from persistence import MemoryStore
from test_app import FakeStore, FakeTTS
from timer import SessionTimer

IST = timezone(timedelta(hours=5, minutes=30))


def done(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


# --- the node's identity survives between runs --------------------------------------
def test_state_round_trip(tmp_path):
    a = tailnet.Tailnet(tmp_path / "a")
    a.state_dir.mkdir(parents=True)
    (a.state_dir / "tailscaled.state").write_text('{"node": "key"}')
    (a.state_dir / "certs").mkdir()
    (a.state_dir / "certs" / "jarvis.crt").write_text("CERT")
    blob = a.state_blob()
    b = tailnet.Tailnet(tmp_path / "b")
    b.restore_state(blob)
    assert (b.state_dir / "tailscaled.state").read_text() == '{"node": "key"}'
    assert (b.state_dir / "certs" / "jarvis.crt").read_text() == "CERT"


def test_restore_ignores_paths_outside_the_state_folder(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"evil"
        info = tarfile.TarInfo("../../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    net = tailnet.Tailnet(tmp_path / "w")
    net.restore_state(buf.getvalue())
    assert not (tmp_path / "escape.txt").exists()


def test_no_state_yet(tmp_path):
    assert tailnet.Tailnet(tmp_path).state_blob() is None


# --- start-up steps -------------------------------------------------------------------
def scripted(net, answers):
    calls = []

    def cli(*args, timeout=30):
        calls.append(args)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    net.cli = cli
    return calls


def test_first_login_uses_the_key_and_is_marked_fresh(tmp_path):
    net = tailnet.Tailnet(tmp_path)
    calls = scripted(net, [done('{"BackendState": "NeedsLogin"}'), done()])
    net.up("tskey-auth-abc", "jarvis")
    assert calls[1][0] == "up" and "--hostname=jarvis" in calls[1]
    assert "--auth-key=tskey-auth-abc" in calls[1] and net.fresh_login


def test_saved_identity_needs_no_login(tmp_path):
    net = tailnet.Tailnet(tmp_path)
    calls = scripted(net, [done('{"BackendState": "Running"}')])
    net.up("tskey-auth-abc", "jarvis")
    assert len(calls) == 1 and not net.fresh_login


def test_bad_key_is_explained(tmp_path):
    net = tailnet.Tailnet(tmp_path)
    scripted(net, [done("{}"), done(code=1, stderr="invalid key: unable to validate API key")])
    with pytest.raises(tailnet.TailscaleError, match="TS_AUTHKEY"):
        net.up("tskey-auth-old", "jarvis")


@pytest.mark.parametrize("answer", [
    done("To enable, visit: https://login.tailscale.com/f/serve?node=abc"),
    subprocess.TimeoutExpired("tailscale", 45),
])
def test_https_not_enabled_is_explained(tmp_path, answer):
    net = tailnet.Tailnet(tmp_path)
    scripted(net, [answer])
    with pytest.raises(tailnet.TailscaleError, match="HTTPS Certificates"):
        net.serve(8001)


def test_serve_and_name(tmp_path):
    net = tailnet.Tailnet(tmp_path)
    calls = scripted(net, [done("Available within your tailnet"),
                           done(json.dumps({"Self": {"DNSName": "jarvis.tail1234.ts.net."}}))])
    net.serve(8001)
    assert calls[0] == ("serve", "--bg", "--https=443", "http://127.0.0.1:8001")
    assert net.dns_name() == "jarvis.tail1234.ts.net"


def test_setup_is_optional_and_never_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_bundle_cache", {})
    monkeypatch.delenv("TS_AUTHKEY", raising=False)
    monkeypatch.setattr(config, "HOME", False)
    assert tailnet.setup(tmp_path, None) is None  # not configured
    monkeypatch.setenv("TS_AUTHKEY", "tskey-auth-abc")

    def broken(self, *a, **k):
        raise tailnet.TailscaleError("download failed")
    monkeypatch.setattr(tailnet.Tailnet, "start", broken)
    assert tailnet.setup(tmp_path, None) is None  # Jarvis still starts (ngrok)


# --- who may come in through Tailscale ----------------------------------------------------
@pytest.fixture
def jarvis(embedder):
    a = app_module.Assistant(None, FakeTTS(), Memory(embedder), FakeStore(),
                             SessionTimer(datetime.now(IST), 600))
    web = app_module.create_app(a, tailnet_users={"me@gmail.com"})
    return a, web


def test_tailnet_port_checks_the_tailscale_user(jarvis):
    a, web = jarvis
    ts = TestClient(web, base_url=f"http://127.0.0.1:{config.TAILSCALE_PORT}")
    assert ts.get("/api/status", headers={"Tailscale-User-Login": "Me@Gmail.com"}).status_code == 200
    r = ts.get("/api/status", headers={"Tailscale-User-Login": "stranger@gmail.com"})
    assert r.status_code == 403 and "stranger@gmail.com" in r.json()["error"]
    assert ts.get("/api/status").status_code == 403  # no identity (e.g. a tagged device)


def test_ngrok_port_cannot_pretend_to_be_tailscale(embedder):
    a = app_module.Assistant(None, FakeTTS(), Memory(embedder), FakeStore(),
                             SessionTimer(datetime.now(IST), 600))
    gate = app_module.PasswordGate("secret-password", b"k" * 32)
    web = app_module.create_app(a, gate, tailnet_users={"me@gmail.com"})
    via_ngrok = TestClient(web, base_url=f"http://127.0.0.1:{config.APP_PORT}")
    r = via_ngrok.get("/api/status", headers={"Tailscale-User-Login": "me@gmail.com"})
    assert r.status_code == 401  # the header means nothing on the ngrok port


def test_only_ngrok_traffic_counts_against_ngrok_limits(jarvis, monkeypatch):
    a, web = jarvis
    monkeypatch.setattr(config, "NGROK_IN_USE", True)
    month = datetime.now(IST).strftime("%Y-%m")
    TestClient(web, base_url=f"http://127.0.0.1:{config.TAILSCALE_PORT}").get(
        "/api/status", headers={"Tailscale-User-Login": "me@gmail.com"})
    assert a.memory.get_net(month)[0] == 0
    TestClient(web, base_url=f"http://127.0.0.1:{config.APP_PORT}").get("/api/status")
    assert a.memory.get_net(month)[0] == 1


# --- saved next to the memory, encrypted ------------------------------------------------
def test_state_is_saved_encrypted_with_the_memory_and_loaded_back(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_PATH", tmp_path / "backup.enc")
    key = crypto.load_key(crypto.generate_key())
    remote = tmp_path / "dataset"
    remote.mkdir()

    def fake_kaggle(*args):
        folder = Path(args[args.index("-p") + 1])
        if args[1] in ("version", "create"):
            for f in folder.iterdir():
                (remote / f.name).write_bytes(f.read_bytes())
        else:  # download
            for f in remote.iterdir():
                (folder / f.name).write_bytes(f.read_bytes())
        return done("ok")

    store = MemoryStore("me", key, {})
    store._kaggle = fake_kaggle
    store.exists = True
    assert store.save(b"memory", extras={config.TAILSCALE_STATE_FILE: b"node identity"})
    assert b"node identity" not in (remote / config.TAILSCALE_STATE_FILE).read_bytes()
    again = MemoryStore("me", key, {})
    again._kaggle = fake_kaggle
    assert again.load() == b"memory"
    assert again.extras == {config.TAILSCALE_STATE_FILE: b"node identity"}
    assert again.save(b"memory v2")  # not given again: the loaded state is kept
    assert (remote / config.TAILSCALE_STATE_FILE).exists()


# --- Tailscale only (the owner's choice), ngrok only as a fallback ------------------------
def test_link_plan(monkeypatch):
    import main
    ngrok = ("token", "x.ngrok-free.dev")
    monkeypatch.setattr(config, "HOME", False)
    monkeypatch.setattr(config, "USE_NGROK", False)
    monkeypatch.setattr(config, "NGROK_FALLBACK", True)
    assert main.link_plan("tskey", ngrok) == {"tailscale_only": True, "ngrok": None, "spare": ngrok}
    assert main.link_plan(None, ngrok) == {"tailscale_only": False, "ngrok": ngrok, "spare": None}
    monkeypatch.setattr(config, "NGROK_FALLBACK", False)
    assert main.link_plan("tskey", ngrok)["spare"] is None
    monkeypatch.setattr(config, "USE_NGROK", True)  # both links open ("public" later)
    assert main.link_plan("tskey", ngrok) == {"tailscale_only": False, "ngrok": ngrok, "spare": None}


def test_start_button_does_not_call_a_tailscale_only_jarvis_offline(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "start_jarvis", Path(__file__).resolve().parent.parent / "scripts" / "start_jarvis.py")
    sj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sj)
    for name, value in {"KAGGLE_USERNAME": "me", "KAGGLE_API_TOKEN": "KGAT_x",
                        "MEMORY_KEY": "a" * 43 + "=", "TS_AUTHKEY": "tskey-auth-x",
                        "ALLOWED_EMAIL": "me@gmail.com", "NGROK_AUTHTOKEN": "t",
                        "NGROK_DOMAIN": "x.ngrok-free.dev"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(config, "USE_NGROK", False)
    checked = []
    monkeypatch.setattr(sj, "link_is_online", lambda domain: checked.append(domain) or None)
    monkeypatch.setattr(sj, "kaggle", lambda *a: done('has status "KernelWorkerStatus.RUNNING"'))
    sj.main()  # returns normally: "Kaggle says Jarvis is running. Open your Tailscale link"
    assert checked == [""]  # the unused ngrok address was not checked
