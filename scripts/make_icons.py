"""Draw the app icons (white microphone on blue) into src/static.

    python scripts/make_icons.py

Plain Python (no extra packages). Full-bleed background with the mic inside the
middle 60 %, so the same image also works as a "maskable" Android icon.
"""
import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "static"
BLUE, WHITE = (37, 99, 235), (255, 255, 255)


def in_mic(x: float, y: float) -> bool:
    """Is point (x, y), in 0..100 icon units, on the white microphone?"""
    # capsule: rounded rectangle 40..60 x 22..58, corner radius 10
    if 40 <= x <= 60 and 32 <= y <= 48:
        return True
    for cy in (32, 48):
        if math.hypot(x - 50, y - cy) <= 10 and (y <= 32 if cy == 32 else y >= 48):
            return True
    # cup: lower half of a ring around (50, 50), radius 17..23
    if y >= 50 and 17 <= math.hypot(x - 50, y - 50) <= 23:
        return True
    if 47 <= x <= 53 and 72 <= y <= 80:  # stem
        return True
    return 38 <= x <= 62 and 78 <= y <= 84  # base


def draw(size: int, samples: int = 4) -> bytes:
    rows = []
    step = 100 / size
    for py in range(size):
        row = bytearray([0])  # PNG filter byte: none
        for px in range(size):
            hits = sum(in_mic((px + (i + 0.5) / samples) * step, (py + (j + 0.5) / samples) * step)
                       for i in range(samples) for j in range(samples))
            a = hits / samples ** 2  # smooth edges
            row += bytes(round(b + (w - b) * a) for b, w in zip(BLUE, WHITE))
        rows.append(bytes(row))
    return b"".join(rows)


def png(size: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(draw(size), 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    for name, size in [("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)]:
        (OUT / name).write_bytes(png(size))
        print("wrote", OUT / name)
