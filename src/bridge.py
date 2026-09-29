"""The home helper link: Jarvis (on Kaggle) <-> a small helper at home that can reach your
home Wi-Fi, e.g. your Android phone running home_bridge/samsung_bridge.py in Termux.

Why: Samsung's SmartThings API costs money from October 2026, but the washer also answers
directly on the home Wi-Fi (CoAP-DTLS). Kaggle can't reach your home network, so the helper
at home does, and Jarvis talks to the helper.

How:
- The helper opens ONE WebSocket to wss://<your ngrok domain>/api/bridge/ws per session
  (one ngrok request, not one per question). Only this path skips Google sign-in at ngrok;
  it needs the BRIDGE_KEY secret instead (checked here, constant-time).
- When Jarvis starts it posts "up" to a private ntfy topic derived from BRIDGE_KEY, so the
  helper connects at once instead of knocking every few minutes.
- appliance_control ALWAYS waits for your spoken "yes" (tools.py), and the washer itself
  only obeys while its Smart Control button is on (Samsung's own safety rule).
Logs show counts and ok/failed only.
"""
import asyncio
import hashlib
import hmac
import itertools
import json
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout

import requests

import config
import tools

ACTIONS = ("start", "pause", "stop")
NOT_CONNECTED = ("The home helper isn't connected. On your phone at home, open Termux and "
                 "start it (python samsung_bridge.py); it must be on the home Wi-Fi.")


def wake_topic(key: str) -> str:
    """The private ntfy topic both sides derive from BRIDGE_KEY (no extra secret)."""
    return "jarvis-" + hashlib.sha256(b"jarvis-bridge-wake:" + key.strip().encode()).hexdigest()[:24]


class HomeBridge:
    def __init__(self, key: str):
        self.key = key.strip()
        self.ws = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.devices: list[str] = []
        self.pending: dict[int, Future] = {}
        self.ids = itertools.count(1)

    def authorized(self, header: str | None) -> bool:
        token = (header or "").removeprefix("Bearer ").strip()
        return bool(token) and hmac.compare_digest(token.encode(), self.key.encode())

    @property
    def connected(self) -> bool:
        return self.ws is not None

    async def serve(self, websocket) -> None:
        """One helper connection, for as long as it lasts (a newer one replaces it)."""
        if not self.authorized(websocket.headers.get("authorization")):
            print("[bridge] refused a connection with a wrong key")
            await websocket.close(code=1008)
            return
        await websocket.accept()
        old, self.ws, self.loop = self.ws, websocket, asyncio.get_running_loop()
        if old is not None:
            try:
                await old.close()
            except Exception:
                pass
        try:
            while True:
                message = json.loads(await websocket.receive_text())
                if isinstance(message.get("hello"), dict):
                    names = message["hello"].get("devices") or []
                    self.devices = [str(d).strip().lower()[:40] for d in names][:10]
                    print(f"[bridge] home helper connected ({len(self.devices)} device(s))")
                elif "id" in message:
                    future = self.pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        future.set_result(message)
        except Exception:  # disconnect, bad JSON: the helper reconnects by itself
            pass
        finally:
            if self.ws is websocket:
                self.ws = None
                self.devices = []
                for future in self.pending.values():
                    if not future.done():
                        future.set_result({"ok": False, "text": NOT_CONNECTED})
                self.pending.clear()
                print("[bridge] home helper disconnected")

    def request(self, op: str, device: str, timeout: float = 25) -> dict:
        """Ask the helper to do something; called from a turn's (non-async) thread."""
        ws, loop = self.ws, self.loop
        if ws is None or loop is None:
            return {"ok": False, "text": NOT_CONNECTED}
        rid = next(self.ids)
        future: Future = Future()
        self.pending[rid] = future
        asyncio.run_coroutine_threadsafe(
            ws.send_text(json.dumps({"id": rid, "op": op, "device": device})), loop)
        try:
            return future.result(timeout)
        except FutureTimeout:
            self.pending.pop(rid, None)
            return {"ok": False, "text": "The home helper did not answer in time."}

    # --- what the tools do -------------------------------------------------------------
    def _device(self, device: str) -> str | None:
        name = str(device or "").strip().lower()
        if self.devices and name not in self.devices:
            return None
        return name

    def status(self, device: str) -> str:
        name = self._device(device)
        if name is None:
            return f"Unknown appliance. The home helper has: {', '.join(self.devices)}."
        return str(self.request("status", name).get("text", ""))[:300]

    def control(self, device: str, action: str) -> str:
        name = self._device(device)
        if name is None:
            return f"Unknown appliance. The home helper has: {', '.join(self.devices)}."
        if action not in ACTIONS:
            return f"'{action}' is not possible. Possible: {', '.join(ACTIONS)}."
        result = self.request(action, name)
        print(f"[bridge] {action}: {'ok' if result.get('ok') else 'failed'}")
        return str(result.get("text", ""))[:300]

    def announce(self) -> bool:
        """Tell the helper that Jarvis is up (one small ntfy message, content "up")."""
        try:
            return requests.post(f"{config.NTFY_SERVER.rstrip('/')}/{wake_topic(self.key)}",
                                 data=b"up", timeout=10).ok
        except requests.RequestException:
            return False


def register(bridge: HomeBridge) -> None:
    tools.register(tools.Tool(
        name="appliance_status",
        description=("Check a home appliance through the home helper, e.g. 'is the washing "
                     "machine done?', 'how long is left on the wash?'."),
        parameters={"type": "object", "properties": {
            "device": {"type": "string", "description": "e.g. 'washing machine'"}},
            "required": ["device"]},
        func=bridge.status, requires_confirmation=False))
    tools.register(tools.Tool(
        name="appliance_control",
        description=("Start, pause or stop a home appliance through the home helper (e.g. the "
                     "washing machine). Always needs the user's spoken yes first."),
        parameters={"type": "object", "properties": {
            "device": {"type": "string", "description": "e.g. 'washing machine'"},
            "action": {"type": "string", "enum": list(ACTIONS)}},
            "required": ["device", "action"]},
        func=bridge.control, confirm_text="{action} the {device}"))


def setup() -> HomeBridge | None:
    """Only with the BRIDGE_KEY secret."""
    key = config.get_secret("BRIDGE_KEY", required=False)
    if not key or len(key.strip()) < 20:
        return None
    bridge = HomeBridge(key)
    register(bridge)
    return bridge
