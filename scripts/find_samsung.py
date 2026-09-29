"""Find Samsung appliances on your home Wi-Fi and tell which free local method they speak.

Run it on a laptop that is connected to the SAME Wi-Fi as the washer/fridge (at home):

    python scripts/find_samsung.py

Nothing is installed and nothing is sent to the internet: it only knocks on the devices
in your own network. It looks for the two community methods that work without the paid
SmartThings API:

  Method 1 (roughly 2018-2022 models): an HTTPS service on TCP port 8888
            (project: github.com/perseus177/ha-samsung-washer-local)
  Method 2 (newer models): CoAP over DTLS on a UDP port between 49152 and 49160
            (projects: github.com/QuiteYellow/SmartThings-Local, github.com/mbillow/localthings)

Copy the result lines and send them to Claude, with the model number from the sticker
(inside the washer door or on the back, e.g. "WW80T504DAN").
"""
import ipaddress
import os
import socket
import struct
import sys
from concurrent.futures import ThreadPoolExecutor

REST_PORT = 8888
DTLS_PORTS = range(49152, 49161)


def local_network() -> ipaddress.IPv4Network:
    """This computer's home network (assumes the usual /24, e.g. 192.168.1.x)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.168.1.1", 9))  # no packet is sent: this only picks the Wi-Fi address
        ip = s.getsockname()[0]
    return ipaddress.ip_network(f"{ip}/24", strict=False)


def tcp_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def dtls_client_hello() -> bytes:
    """A minimal DTLS ClientHello. A DTLS server answers it (HelloVerifyRequest,
    ServerHello or an alert); anything else stays silent."""
    ciphers = [0xC0AE, 0xC0AC, 0xC02B, 0xC023, 0xC00A, 0xC0A8, 0x00AE]
    body = b"\xfe\xfd" + os.urandom(32) + b"\x00" + b"\x00"  # no session id, no cookie
    body += struct.pack(">H", len(ciphers) * 2) + b"".join(struct.pack(">H", c) for c in ciphers)
    body += b"\x01\x00"  # compression: none
    groups = struct.pack(">HH", 2, 23)                          # secp256r1
    formats = b"\x01\x00"                                       # uncompressed points
    algorithms = struct.pack(">HHH", 4, 0x0403, 0x0401)         # ecdsa/rsa + sha256
    ext = (struct.pack(">HH", 10, len(groups)) + groups
           + struct.pack(">HH", 11, len(formats)) + formats
           + struct.pack(">HH", 13, len(algorithms)) + algorithms)
    body += struct.pack(">H", len(ext)) + ext
    length = len(body).to_bytes(3, "big")
    handshake = b"\x01" + length + b"\x00\x00" + b"\x00\x00\x00" + length + body
    return b"\x16\xfe\xff" + b"\x00\x00" + b"\x00" * 6 + struct.pack(">H", len(handshake)) + handshake


def dtls_answers(host: str, port: int, timeout: float = 0.6) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        try:
            s.sendto(dtls_client_hello(), (host, port))
            data, _ = s.recvfrom(2048)
        except OSError:  # silence, or "port closed" (Windows reports it as an error)
            return False
    return len(data) >= 3 and data[0] in (0x15, 0x16) and data[1] == 0xFE


def probe(host: str) -> list[str]:
    found = []
    if tcp_open(host, REST_PORT):
        found.append(f"{host}: TCP port {REST_PORT} open -> probably METHOD 1 (older model, "
                     "HTTPS on port 8888)")
    for port in DTLS_PORTS:
        if dtls_answers(host, port):
            found.append(f"{host}: DTLS answers on UDP {port} -> probably METHOD 2 (newer "
                         "model, CoAP-DTLS)")
            break
    return found


def main() -> None:
    net = local_network() if len(sys.argv) < 2 else ipaddress.ip_network(sys.argv[1], strict=False)
    hosts = [str(h) for h in net.hosts()]
    print(f"Looking at {len(hosts)} addresses in {net} (about a minute)...")
    with ThreadPoolExecutor(max_workers=64) as pool:
        results = [line for lines in pool.map(probe, hosts) for line in lines]
    if results:
        print("\n".join(results))
        print("\nSend these lines and the appliance's model number to Claude. If several devices "
              "show up, open your router's device list to see which address is the washer.")
    else:
        print("Nothing found. Check: the appliance is switched on and connected to Wi-Fi (it "
              "shows in the SmartThings app), and this laptop is on the same Wi-Fi (not "
              "mobile data, not office VPN). Wi-Fi 'guest' networks often block this.")


if __name__ == "__main__":
    main()
