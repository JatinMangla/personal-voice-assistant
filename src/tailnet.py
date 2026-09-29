"""Private access over Tailscale (next to, or instead of, ngrok).

Why: end-to-end encrypted (WireGuard) from your phone to Jarvis; there is no public
address at all, only devices in your tailnet can reach it, no ngrok monthly limits, and
often a direct (shorter) path.

How, on Kaggle:
- tailscaled runs in userspace mode (containers have no network device for a VPN),
  joins your tailnet as "jarvis" with TS_AUTHKEY, and `tailscale serve` publishes
  https://jarvis.<your-tailnet>.ts.net (a real certificate: phone browsers only allow the
  microphone on https) -> 127.0.0.1:TAILSCALE_PORT.
- Its state (the node's identity and the certificate) is saved encrypted next to the
  memory, so every run is the SAME device with the SAME address. Otherwise each run would
  become jarvis-1, jarvis-2 ... and need a new certificate (Let's Encrypt allows ~5 a week).
- The app gets its own local port for Tailscale traffic and checks the Tailscale-User-Login
  header (set by `tailscale serve`, which removes spoofed copies) against ALLOWED_EMAIL /
  TAILSCALE_USERS. ngrok traffic uses the other port, so it can't pretend to be Tailscale.

On a home PC with the Tailscale app installed, only `tailscale serve` is run.
"""
import io
import json
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import config

DOWNLOAD_URL = "https://pkgs.tailscale.com/stable/tailscale_latest_amd64.tgz"


class TailscaleError(Exception):
    pass


HTTPS_HELP = ("Tailscale HTTPS certificates are off in your tailnet. Turn them on once: "
              "login.tailscale.com/admin/dns -> 'HTTPS Certificates' -> Enable. Then start again.")


class Tailnet:
    def __init__(self, work_dir: Path, system: bool = False):
        """system=True: use the Tailscale already installed and running on this PC."""
        self.system = system
        self.state_dir = Path(work_dir) / "tailscale-state"
        self.socket = Path(work_dir) / "tailscaled.sock"
        self.log = Path(work_dir) / "tailscaled.log"
        self.bin_dir = Path(work_dir) / "tailscale-bin"
        self.daemon: subprocess.Popen | None = None
        self.fresh_login = False
        self.url: str | None = None

    # --- programs ------------------------------------------------------------------------
    def _binary(self, name: str) -> str:
        local = self.bin_dir / name
        if local.exists():
            return str(local)
        return shutil.which(name) or str(local)

    def install(self) -> None:
        """The two static programs (tailscale, tailscaled), ~30 MB, unless already there."""
        if self.system or ((self.bin_dir / "tailscaled").exists()):
            return
        import requests
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        data = requests.get(DOWNLOAD_URL, timeout=120).content
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                name = Path(member.name).name
                if name in ("tailscale", "tailscaled") and member.isfile():
                    (self.bin_dir / name).write_bytes(tar.extractfile(member).read())
                    (self.bin_dir / name).chmod(0o755)
        if not (self.bin_dir / "tailscaled").exists():
            raise TailscaleError("the Tailscale download had no tailscaled program")

    def cli(self, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
        base = [self._binary("tailscale")]
        if not self.system:
            base.append(f"--socket={self.socket}")
        return subprocess.run(base + list(args), capture_output=True, text=True, timeout=timeout)

    # --- state kept between Kaggle runs ------------------------------------------------------
    def restore_state(self, blob: bytes | None) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if not blob:
            return
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for member in tar.getmembers():  # only plain files inside the state folder
                target = (self.state_dir / member.name).resolve()
                if member.isfile() and self.state_dir.resolve() in target.parents:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(tar.extractfile(member).read())

    def state_blob(self) -> bytes | None:
        """The node identity + certificate as one small tar.gz (encrypted by the caller)."""
        if self.system or not self.state_dir.exists() or not any(self.state_dir.iterdir()):
            return None
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path in sorted(self.state_dir.rglob("*")):
                if path.is_file():
                    tar.add(path, arcname=str(path.relative_to(self.state_dir)))
        return buf.getvalue()

    # --- start / stop ------------------------------------------------------------------------
    def _logged_in(self) -> bool:
        result = self.cli("status", "--json", timeout=10)
        try:
            return json.loads(result.stdout).get("BackendState") == "Running"
        except ValueError:
            return False

    def start_daemon(self) -> None:
        if self.system:
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.daemon = subprocess.Popen(
            [self._binary("tailscaled"), "--tun=userspace-networking",
             f"--statedir={self.state_dir}", f"--socket={self.socket}", "--port=41641"],
            stdout=open(self.log, "a"), stderr=subprocess.STDOUT)
        for _ in range(40):
            if self.socket.exists() and self.cli("version", timeout=5).returncode == 0:
                return
            time.sleep(0.5)
        raise TailscaleError("tailscaled did not start")

    def up(self, authkey: str | None, hostname: str) -> None:
        if self.system or self._logged_in():
            return
        args = ["up", f"--hostname={hostname}", "--accept-dns=false"]
        if authkey:
            args.append(f"--auth-key={authkey.strip()}")
        result = self.cli(*args, timeout=90)
        if result.returncode != 0:
            text = (result.stderr or result.stdout).strip()
            if any(word in text.lower() for word in ("login.tailscale.com", "auth", "key", "expired")):
                raise TailscaleError("Tailscale login failed: the TS_AUTHKEY secret is missing, "
                                     "used up or expired. Make a new one (README, Tailscale).")
            raise TailscaleError(f"tailscale up failed: {text[-200:]}")
        self.fresh_login = True  # new identity: save it soon, so the next run reuses it

    def dns_name(self) -> str:
        status = json.loads(self.cli("status", "--json", timeout=10).stdout or "{}")
        name = str((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
        if not name:
            raise TailscaleError("Tailscale gave no machine name (is MagicDNS on?)")
        return name

    def serve(self, port: int) -> None:
        try:
            result = self.cli("serve", "--bg", "--https=443", f"http://127.0.0.1:{port}", timeout=45)
        except subprocess.TimeoutExpired as e:  # waiting on the "enable HTTPS" prompt
            raise TailscaleError(HTTPS_HELP) from e
        text = result.stdout + result.stderr
        if result.returncode != 0 or "login.tailscale.com" in text:
            raise TailscaleError(HTTPS_HELP if "https" in text.lower() or "login.tailscale.com" in text
                                 else f"tailscale serve failed: {text.strip()[-200:]}")

    def start(self, authkey: str | None, hostname: str, port: int,
              state: bytes | None = None) -> str:
        """Join the tailnet and publish the app. Returns the https address."""
        if not self.system:
            self.restore_state(state)
            self.install()
        self.start_daemon()
        self.up(authkey, hostname)
        self.serve(port)
        self.url = f"https://{self.dns_name()}"
        return self.url

    def stop(self) -> None:
        """The node just goes offline (no logout: the next run reuses the identity)."""
        if self.daemon is not None:
            self.daemon.terminate()


def setup(work_dir: Path, state: bytes | None) -> Tailnet | None:
    """Start Tailscale if configured (TS_AUTHKEY on Kaggle; the Tailscale app at home).
    Never blocks the start: without it, Jarvis still runs over ngrok."""
    authkey = config.get_secret("TS_AUTHKEY", required=False)
    system = config.HOME and shutil.which("tailscale") is not None
    if not authkey and not system:
        return None
    net = Tailnet(work_dir, system=system)
    t = time.time()
    try:
        url = net.start(authkey, config.TAILSCALE_HOSTNAME, config.TAILSCALE_PORT, state)
    except (TailscaleError, OSError, subprocess.SubprocessError, ValueError) as e:
        print(f"[tailscale] not available: {e}")
        net.stop()
        return None
    print(f"[tailscale] private link ready in {time.time() - t:.0f}s -> {url}")
    return net


def allowed_logins() -> set[str]:
    """Tailscale users who may use Jarvis: ALLOWED_EMAIL plus TAILSCALE_USERS."""
    names = set()
    for secret in ("ALLOWED_EMAIL", "TAILSCALE_USERS"):
        value = config.get_secret(secret, required=False) or ""
        names.update(v.strip().lower() for v in value.split(",") if v.strip())
    return names


def is_tailnet_request(scope: dict) -> bool:
    """Requests that came in through `tailscale serve` (its own local port)."""
    server = scope.get("server") or ("", 0)
    return server[1] == config.TAILSCALE_PORT
