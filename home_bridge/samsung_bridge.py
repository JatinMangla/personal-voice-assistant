"""Jarvis home helper: lets Jarvis (running on Kaggle) check and start/pause/stop your
Samsung washing machine for free, directly over your home Wi-Fi.

Runs on your Android phone in Termux (or any laptop at home). See home_bridge/README.md.

- Talks to the washer locally with the smartthings-local library (CoAP-DTLS, the same
  protocol the SmartThings app uses on your Wi-Fi): no SmartThings API, no subscription.
- Connects to Jarvis over your ngrok link with BRIDGE_KEY, only while Jarvis is running
  (a private ntfy topic tells it when Jarvis starts).
- Bonus: a phone notification when a wash finishes, even while Jarvis is off (needs the
  free Termux:API app).
- Only these actions exist: status, start, pause, stop. Jarvis asks for your spoken
  "yes" first, and the washer obeys only while its Smart Control is on.

Settings: bridge.env next to this file (see README). Never share that file.
"""
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_PATH = ("operational", "state", "vs", "0")
REMOTE_PATHS = (("remotectrl", "0"), ("remotectrl", "vs", "0"))
DTLS_PORTS = [5684, *range(49152, 49161)]
COMMANDS = {"start": "Run", "pause": "Pause", "stop": "Ready"}


# --- settings ---------------------------------------------------------------------------
def load_settings(path: Path = HERE / "bridge.env") -> dict:
    settings = {"DEVICE_NAME": "washing machine", "CERT": "certs/client_fullchain.pem",
                "KEY": "certs/client.key", "NTFY_SERVER": "https://ntfy.sh"}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, value = (p.strip() for p in line.split("=", 1))
                settings[name] = value.strip('"').strip("'")
    for name in ("JARVIS_URL", "BRIDGE_KEY", "WASHER_IP", *settings):
        if os.environ.get(name):
            settings[name] = os.environ[name]
    return settings


def wake_topic(key: str) -> str:
    """Same as src/bridge.py: the private topic Jarvis posts "up" to when it starts."""
    return "jarvis-" + hashlib.sha256(b"jarvis-bridge-wake:" + key.strip().encode()).hexdigest()[:24]


# --- the washer -------------------------------------------------------------------------
def unwrap(rep):
    """OCF answers may wrap the values in {"rep": {...}}."""
    if isinstance(rep, dict) and isinstance(rep.get("rep"), dict):
        return rep["rep"]
    return rep if isinstance(rep, dict) else {}


def minutes_left(raw) -> int | None:
    try:
        parts = [int(p) for p in str(raw).split(":")]
    except ValueError:
        return None
    if len(parts) == 3:
        return parts[0] * 60 + parts[1] + (1 if parts[2] else 0)
    if len(parts) == 2:
        return parts[0] + (1 if parts[1] else 0)
    return None


def describe(rep: dict) -> tuple[str, str]:
    """(short state, sentence) from /operational/state/vs/0."""
    rep = unwrap(rep)
    state = str(rep.get("x.com.samsung.da.state") or "")
    progress = str(rep.get("x.com.samsung.da.progress") or "")
    percent = rep.get("x.com.samsung.da.progressPercentage")
    left = minutes_left(rep.get("x.com.samsung.da.remainingTime"))
    if progress == "Finish" or state == "End":
        return "finished", "The washing machine has finished."
    if state in ("Run", "Running"):
        text = "The washing machine is running"
        if progress and progress != "None":
            text += f" ({progress.lower()})"
        if left:
            text += f", about {left} minutes left"
        if percent not in (None, "", "None"):
            text += f", {percent}% done"
        return "running", text + "."
    if state in ("Pause", "Paused"):
        return "paused", "The washing machine is paused" + (f", {left} minutes left." if left else ".")
    return "idle", "The washing machine is not running."


def coap_ok(code) -> bool:
    """2.xx success, whether the library gives 69, '2.05' or 'Content'."""
    if isinstance(code, int):
        return 64 <= code < 96
    text = str(code)
    return text.startswith("2") or text.lower() in ("content", "changed", "valid")


class Washer:
    def __init__(self, ip: str, cert: str, key: str, session_factory=None):
        self.ip, self.cert, self.key = ip, cert, key
        self.session_factory = session_factory or self._real_session
        self.port: int | None = None
        self.lock = threading.Lock()  # one conversation with the washer at a time

    def _real_session(self):
        from smartthings_local.protocol.auth import CertificateAuth
        from smartthings_local.protocol.dtls_probe import probe_dtls_ports
        from smartthings_local.protocol.dtls_session import DtlsCoapSession
        if self.port is None:
            self.port = probe_dtls_ports(self.ip, DTLS_PORTS).selected_port
            if not self.port:
                raise ConnectionError("no secure port answered")
        session = DtlsCoapSession(self.ip, self.port, auth=CertificateAuth.from_files(self.cert, self.key))
        session.connect(timeout=10)
        session.start_reader()
        return session

    def _read(self, session, path) -> dict | None:
        import cbor2
        code, payload = session.get(path)
        return unwrap(cbor2.loads(payload)) if coap_ok(code) and payload else None

    def _remote_control_on(self, session) -> bool:
        for path in REMOTE_PATHS:
            try:
                rep = self._read(session, path)
            except Exception:
                rep = None
            if rep:
                if "value" in rep:
                    return bool(rep["value"])
                if "x.com.samsung.da.remoteControlEnabled" in rep:
                    return str(rep["x.com.samsung.da.remoteControlEnabled"]).lower() == "true"
        return True  # the washer doesn't report it: let it decide

    def run(self, op: str) -> dict:
        with self.lock:
            return self._run(op)

    def _run(self, op: str) -> dict:
        try:
            session = self.session_factory()
        except Exception as e:
            self.port = None  # the address/port may have changed: probe again next time
            return {"ok": False, "text": "I couldn't reach the washing machine. Is it switched on "
                                         f"and on Wi-Fi? ({type(e).__name__})"}
        try:
            if op == "status":
                rep = self._read(session, STATE_PATH)
                if rep is None:
                    return {"ok": False, "text": "The washing machine didn't answer the status question."}
                return {"ok": True, "text": describe(rep)[1], "state": describe(rep)[0]}
            if op not in COMMANDS:
                return {"ok": False, "text": f"'{op}' isn't possible."}
            if not self._remote_control_on(session):
                return {"ok": False, "text": "Smart Control is off on the washing machine. Press its "
                                             "Smart Control button first, then ask again."}
            import cbor2
            code, _ = session.post(STATE_PATH, cbor2.dumps({"x.com.samsung.da.state": COMMANDS[op]}))
            if not coap_ok(code):
                return {"ok": False, "text": f"The washing machine refused to {op}."}
            time.sleep(3)  # some firmware accepts, then quietly reverts: read back
            state = describe(self._read(session, STATE_PATH) or {})[0]
            expected = {"start": ("running",), "pause": ("paused",), "stop": ("idle", "finished")}[op]
            if state in expected:
                return {"ok": True, "text": f"Done: the washing machine is {state}."}
            return {"ok": False, "text": f"I asked it to {op}, but it still says {state}."}
        except Exception as e:
            return {"ok": False, "text": f"Talking to the washing machine failed ({type(e).__name__})."}
        finally:
            try:
                session.close()
            except Exception:
                pass


# --- requests from Jarvis ---------------------------------------------------------------
def handle(message: dict, devices: dict) -> dict:
    """{"id", "op", "device"} -> {"id", "ok", "text"}."""
    device = devices.get(str(message.get("device", "")).strip().lower())
    if device is None:
        result = {"ok": False, "text": f"Unknown appliance. I have: {', '.join(devices)}."}
    else:
        result = device.run(str(message.get("op", "")))
    return {"id": message.get("id"), **{k: v for k, v in result.items() if k in ("ok", "text")}}


async def serve_jarvis(settings: dict, devices: dict) -> None:
    """One WebSocket session with Jarvis, until it closes."""
    import websockets
    url = settings["JARVIS_URL"].rstrip("/").replace("https://", "wss://") + "/api/bridge/ws"
    headers = {"Authorization": f"Bearer {settings['BRIDGE_KEY']}", "ngrok-skip-browser-warning": "1"}
    try:
        connection = websockets.connect(url, additional_headers=headers, ping_interval=30)
    except TypeError:  # older websockets versions
        connection = websockets.connect(url, extra_headers=headers, ping_interval=30)
    async with connection as ws:
        await ws.send(json.dumps({"hello": {"devices": list(devices)}}))
        print(f"[{time.strftime('%H:%M')}] connected to Jarvis")
        async for raw in ws:
            reply = await asyncio.to_thread(handle, json.loads(raw), devices)
            await ws.send(json.dumps(reply))


def wait_for_jarvis(settings: dict, max_wait: float = 1800) -> None:
    """Block until Jarvis says it's up (ntfy), or at most max_wait seconds."""
    import requests
    url = f"{settings['NTFY_SERVER'].rstrip('/')}/{wake_topic(settings['BRIDGE_KEY'])}/json"
    try:
        with requests.get(url, stream=True, timeout=(10, max_wait)) as resp:
            end = time.time() + max_wait
            for line in resp.iter_lines():
                if line and json.loads(line).get("event") == "message":
                    return
                if time.time() > end:
                    return
    except Exception:
        time.sleep(60)


def notify(text: str) -> None:
    """A phone notification via Termux:API, if installed; else just print."""
    print(f"[{time.strftime('%H:%M')}] {text}")
    if shutil.which("termux-notification"):
        subprocess.run(["termux-notification", "--title", "Jarvis", "--content", text],
                       capture_output=True)


def watch_for_finish(washer: Washer, every: float = 120) -> None:
    """Local only: tells you when a wash ends, whether or not Jarvis is running."""
    last = None
    while True:
        result = washer.run("status")
        state = result.get("state")
        if last in ("running", "paused") and state == "finished":
            notify("The washing machine has finished.")
        if state:
            last = state
        time.sleep(every)


def main() -> None:
    settings = load_settings()
    missing = [k for k in ("JARVIS_URL", "BRIDGE_KEY", "WASHER_IP") if not settings.get(k)]
    if missing:
        sys.exit(f"Missing in bridge.env: {', '.join(missing)} (see README.md)")
    washer = Washer(settings["WASHER_IP"], str(HERE / settings["CERT"]), str(HERE / settings["KEY"]))
    devices = {settings["DEVICE_NAME"].strip().lower(): washer}
    print("Checking the washing machine...", washer.run("status")["text"])
    threading.Thread(target=watch_for_finish, args=(washer,), daemon=True).start()
    while True:
        try:
            asyncio.run(serve_jarvis(settings, devices))
            print(f"[{time.strftime('%H:%M')}] Jarvis closed the connection")
        except Exception as e:
            print(f"[{time.strftime('%H:%M')}] Jarvis not reachable ({type(e).__name__}); waiting")
        wait_for_jarvis(settings)


if __name__ == "__main__":
    main()
