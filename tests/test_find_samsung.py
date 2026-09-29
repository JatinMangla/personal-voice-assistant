"""The home-network scanner for Samsung appliances (scripts/find_samsung.py)."""
import importlib.util
import socket
import struct
import threading
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "find_samsung", Path(__file__).resolve().parent.parent / "scripts" / "find_samsung.py")
fs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fs)


def test_client_hello_lengths_are_consistent():
    hello = fs.dtls_client_hello()
    assert hello[:3] == b"\x16\xfe\xff"                      # handshake record, DTLS
    assert struct.unpack(">H", hello[11:13])[0] == len(hello) - 13
    handshake = hello[13:]
    assert handshake[0] == 1                                 # ClientHello
    body_len = int.from_bytes(handshake[1:4], "big")
    assert handshake[9:12] == handshake[1:4]                 # fragment = whole message
    assert len(handshake) == 12 + body_len


def test_detects_a_dtls_server_and_ignores_silence():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]

    def answer():
        data, addr = server.recvfrom(2048)
        if data[:1] == b"\x16":
            server.sendto(b"\x16\xfe\xff" + b"\x00" * 20, addr)  # like a HelloVerifyRequest
    threading.Thread(target=answer, daemon=True).start()
    assert fs.dtls_answers("127.0.0.1", port)
    server.close()
    silent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    silent.bind(("127.0.0.1", 0))
    assert not fs.dtls_answers("127.0.0.1", silent.getsockname()[1], timeout=0.2)
    silent.close()


def test_tcp_probe():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    assert fs.tcp_open("127.0.0.1", listener.getsockname()[1])
    port = listener.getsockname()[1]
    listener.close()
    assert not fs.tcp_open("127.0.0.1", port, timeout=0.2)
