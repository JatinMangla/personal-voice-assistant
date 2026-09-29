"""Find Samsung appliances on your home Wi-Fi and tell which free local method they speak.

Run it on a laptop or phone (Termux) that is on the SAME Wi-Fi as the washer, with the
washer switched ON (many washers leave Wi-Fi when they are off):

    python scripts/find_samsung.py

Nothing is installed and nothing is sent to the internet: it only asks the devices in
your own network. It looks for the two community methods that work without the paid
SmartThings API:

  Method 2 (newer models, e.g. 2023+ "DG" washers): OCF / CoAP. Plain discovery on UDP
            5683, the secure service (DTLS) on UDP 5684 or 49152-49160.
            (github.com/QuiteYellow/SmartThings-Local, github.com/mbillow/localthings)
  Method 1 (roughly 2018-2022 models): HTTPS on TCP port 8888
            (github.com/perseus177/ha-samsung-washer-local)

Copy the result lines and send them to Claude.
"""
import ipaddress
import os
import socket
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor

REST_PORT = 8888
COAP_PORT = 5683
DTLS_PORTS = (5684, *range(49152, 49161))
COAP_MULTICAST = "224.0.1.187"  # "all CoAP nodes"; OCF devices answer discovery here


def local_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.168.1.1", 9))  # no packet is sent: this only picks the Wi-Fi address
        return s.getsockname()[0]


def local_network() -> ipaddress.IPv4Network:
    """This device's home network (assumes the usual /24, e.g. 192.168.1.x)."""
    return ipaddress.ip_network(f"{local_ip()}/24", strict=False)


def coap_get_oic_res(message_id: int = 0x1234) -> bytes:
    """CoAP GET /oic/res: the OCF "which resources do you have?" question."""
    header = bytes([0x50, 0x01]) + struct.pack(">H", message_id)  # v1, NON, no token, GET
    return header + b"\xb3oic" + b"\x03res"                        # Uri-Path "oic", "res"


def is_coap_reply(data: bytes) -> bool:
    return len(data) >= 4 and data[0] >> 6 == 1 and data[1] >> 5 == 2  # version 1, 2.xx


def multicast_discovery(timeout: float = 3.0) -> list[str]:
    """Ask every OCF device at once; returns the addresses that answered."""
    found = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip()))
        except OSError:
            pass
        s.settimeout(0.5)
        s.sendto(coap_get_oic_res(), (COAP_MULTICAST, COAP_PORT))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, (addr, _) = s.recvfrom(4096)
            except OSError:
                continue
            if is_coap_reply(data) and addr not in found:
                found.append(addr)
    return found


def coap_answers(host: str, timeout: float = 0.6) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        try:
            s.sendto(coap_get_oic_res(), (host, COAP_PORT))
            data, _ = s.recvfrom(4096)
        except OSError:
            return False
    return is_coap_reply(data)


def tcp_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def dtls_client_hello() -> bytes:
    """A minimal DTLS ClientHello. A DTLS server answers it (HelloVerifyRequest,
    ServerHello or an alert); anything else stays silent."""
    ciphers = [0xC02B, 0xC0AE, 0xC0AC, 0xC023, 0xC00A, 0xC0A8, 0x00AE]
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


def dtls_port(host: str) -> int | None:
    return next((p for p in DTLS_PORTS if dtls_answers(host, p)), None)


def probe(host: str, known_ocf: bool = False) -> list[str]:
    found = []
    if known_ocf or coap_answers(host):
        found.append(f"{host}: answers OCF discovery (UDP {COAP_PORT}) -> METHOD 2 (newer model)")
    port = dtls_port(host)
    if port:
        found.append(f"{host}: secure DTLS service on UDP {port} -> METHOD 2 (newer model)")
    if tcp_open(host, REST_PORT):
        found.append(f"{host}: TCP port {REST_PORT} open -> METHOD 1 (older model)")
    return found


def main() -> None:
    net = local_network() if len(sys.argv) < 2 else ipaddress.ip_network(sys.argv[1], strict=False)
    print(f"This device: {local_ip()}. Asking the network {net} (about a minute)...")
    try:
        ocf = multicast_discovery()
    except OSError:
        ocf = []
    hosts = [str(h) for h in net.hosts()]
    with ThreadPoolExecutor(max_workers=64) as pool:
        results = [line for lines in pool.map(lambda h: probe(h, h in ocf), hosts) for line in lines]
    results += [f"{h}: answers OCF discovery (outside {net})" for h in ocf
                if ipaddress.ip_address(h) not in net]
    if results:
        print("\n".join(results))
        print("\nSend these lines to Claude. Several devices? Your router's device list shows "
              "which address is the washer (often named after its model).")
    else:
        print("Nothing found. Check: the washer is switched ON and shows as connected in the "
              "SmartThings app, and this device is on the SAME home Wi-Fi (not mobile data, not "
              "an office VPN, not a 'guest' Wi-Fi). Then run it again.")


if __name__ == "__main__":
    main()
