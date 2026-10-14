"""RetroArch's menu wallpaper for brick9000, in the board's colours (the
board's CSS tokens: bunker, panel edge, phosphor): its near-black green, faint
CRT scanlines, a soft vignette and a thin panel-edge frame, so the game menu
looks like the board it opens over. Rerun after changing it; a test checks the
committed PNG matches:

    python3 brick-retro/tools/wallpaper.py
"""

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "brick9000/retroarch/wallpaper.png"
WIDTH, HEIGHT = 1024, 768  # brick9000's screen
BUNKER = (0x0E, 0x14, 0x11)
CRT = (0x07, 0x10, 0x0B)
EDGE = (0x23, 0x2E, 0x27)
PHOSPHOR = (0x2E, 0xE8, 0x4A)
FRAME = 18  # px from the screen edge to the frame
NOTCH = 22  # the board's cut corner


def mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def pixel(x: int, y: int) -> tuple[int, int, int]:
    # Vignette: bunker in the middle, the CRT's darker green at the corners.
    dx, dy = (x - WIDTH / 2) / (WIDTH / 2), (y - HEIGHT / 2) / (HEIGHT / 2)
    c = mix(BUNKER, CRT, min(1.0, (dx * dx + dy * dy) * 0.6))
    if y % 3 == 2:  # scanlines
        c = mix(c, (0, 0, 0), 0.25)
    # The frame: a 2 px panel edge with one notched (cut) corner, top left,
    # like the board's plates, and a phosphor tick on the cut.
    inside = FRAME <= x < WIDTH - FRAME and FRAME <= y < HEIGHT - FRAME
    on_edge = inside and (x < FRAME + 2 or x >= WIDTH - FRAME - 2 or y < FRAME + 2 or y >= HEIGHT - FRAME - 2)
    cut = (x - FRAME) + (y - FRAME)
    if inside and cut < NOTCH:
        on_edge = NOTCH - 2 <= cut < NOTCH
        if on_edge:
            return PHOSPHOR
        return c
    return EDGE if on_edge else c


def png(width: int, height: int, rows) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(v for p in row for v in p) for row in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def image() -> bytes:
    return png(WIDTH, HEIGHT, ([pixel(x, y) for x in range(WIDTH)] for y in range(HEIGHT)))


if __name__ == "__main__":
    OUT.write_bytes(image())
    print(f"wrote {OUT}")
