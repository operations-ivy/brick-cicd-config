"""RetroArch's menu wallpaper for brick9000, drawn like the board it opens
over (brick-status's page: its CSS tokens, plates and wordmark):

- a header band and a footer band in the plates' colour, as on the board;
- the "brick_9000" wordmark in a chunky pixel font, bone with an orange
  shadow, and "ARCADE" in phosphor, in the footer;
- the CRT window on the right: a notched plate with dim phosphor rain, where
  XMB draws a game's box art;
- three status tiles on the left with the board's green, orange and red bars;
- scanlines and a vignette over the board's near-black green.

XMB draws its title, clock, icons and lists over this. Rerun after changing
it; a test checks the committed PNG matches:

    python3 brick-retro/tools/wallpaper.py
"""

import random
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "brick9000/retroarch/wallpaper.png"
W, H = 1024, 768  # brick9000's screen

# brick-status's CSS tokens.
BUNKER = (0x0E, 0x14, 0x11)
PANEL = (0x16, 0x1E, 0x19)
EDGE = (0x23, 0x2E, 0x27)
CRT = (0x07, 0x10, 0x0B)
BONE = (0xD9, 0xD3, 0xBF)
DIM = (0x7A, 0x85, 0x7C)
PHOSPHOR = (0x2E, 0xE8, 0x4A)
ORANGE = (0xD9, 0x82, 0x2B)
RUST = (0xC4, 0x44, 0x2C)

# A 5x7 pixel font, only the letters the wallpaper needs.
GLYPHS = {
    "b": ["10000", "10000", "11110", "10001", "10001", "10001", "11110"],
    "r": ["00000", "00000", "10110", "11001", "10000", "10000", "10000"],
    "i": ["00100", "00000", "01100", "00100", "00100", "00100", "01110"],
    "c": ["00000", "00000", "01110", "10000", "10000", "10001", "01110"],
    "k": ["10000", "10000", "10010", "10100", "11000", "10100", "10010"],
    "_": ["00000", "00000", "00000", "00000", "00000", "00000", "11111"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11100", "10010", "10001", "10001", "10001", "10010", "11100"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
}


def mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


class Canvas:
    def __init__(self):
        self.px = [[BUNKER] * W for _ in range(H)]

    def put(self, x, y, c):
        if 0 <= x < W and 0 <= y < H:
            self.px[y][x] = c

    def rect(self, x0, y0, x1, y1, c):
        for y in range(max(0, y0), min(H, y1)):
            row = self.px[y]
            for x in range(max(0, x0), min(W, x1)):
                row[x] = c

    def plate(self, x0, y0, x1, y1, fill, notch, edge=EDGE):
        """A board plate: filled, with a 2 px edge and its top-right corner cut."""
        for y in range(y0, y1):
            for x in range(x0, x1):
                cut = (x - (x1 - notch)) - (y - y0)  # > 0 inside the cut corner
                if cut > 0:
                    continue
                border = x < x0 + 2 or x >= x1 - 2 or y < y0 + 2 or y >= y1 - 2 or cut > -3
                self.put(x, y, edge if border else fill)

    def text(self, s, x, y, scale, c, shadow=None):
        """Pixel-font text; with a shadow, offset down and right like the board's wordmark."""
        for layer, colour, off in ((shadow, shadow, scale // 2), (c, c, 0)):
            if layer is None:
                continue
            cx = x
            for ch in s:
                for gy, row in enumerate(GLYPHS[ch]):
                    for gx, on in enumerate(row):
                        if on == "1":
                            self.rect(cx + gx * scale + off, y + gy * scale + off,
                                      cx + (gx + 1) * scale + off, y + (gy + 1) * scale + off, colour)
                cx += 6 * scale


def draw() -> Canvas:
    c = Canvas()
    # The room: a vignette from bunker to the CRT's darker green at the corners.
    for y in range(H):
        for x in range(W):
            dx, dy = (x - W / 2) / (W / 2), (y - H / 2) / (H / 2)
            c.px[y][x] = mix(BUNKER, CRT, min(1.0, (dx * dx + dy * dy) * 0.5))

    # Header and footer bands, like the board's.
    c.rect(0, 0, W, 64, PANEL)
    c.rect(0, 64, W, 66, EDGE)
    c.rect(22, 54, 132, 57, PHOSPHOR)  # under XMB's title
    c.rect(0, 702, W, 704, EDGE)
    c.rect(0, 704, W, H, PANEL)

    # The wordmark in the footer, right-aligned: bone on an orange shadow.
    c.text("brick_9000", 620, 716, 4, BONE, shadow=ORANGE)
    c.text("ARCADE", 878, 744, 2, PHOSPHOR)

    # The CRT window on the right, where XMB puts a game's box art, with dim
    # phosphor rain falling in columns (seeded, so the PNG never changes).
    x0, y0, x1, y1 = 676, 196, 1000, 680
    c.plate(x0 - 10, y0 - 10, x1 + 10, y1 + 10, PANEL, 26)
    c.plate(x0, y0, x1, y1, CRT, 18)
    rng = random.Random(9000)
    for col in range(x0 + 10, x1 - 14, 12):
        head = rng.randrange(y0 + 40, y1 - 10)
        length = rng.randrange(60, 260)
        for i, y in enumerate(range(head, max(y0 + 8, head - length), -9)):
            if y + 6 >= y1 - 4 or (x1 - 18 - col) < (y0 + 8 - y) + 18:  # clear of the edge and the cut
                continue
            bright = 0.55 if i == 0 else max(0.05, 0.32 * (1 - i * 9 / length))
            c.rect(col, y, col + 5, y + 6, mix(CRT, PHOSPHOR, bright))

    # Status tiles on the left, each with one of the board's coloured bars.
    for i, bar in enumerate((PHOSPHOR, ORANGE, RUST)):
        ty = 236 + i * 110
        c.plate(22, ty, 212, ty + 92, PANEL, 14)
        c.rect(22, ty, 30, ty + 92, bar)
        for line in range(3):  # dim "detail" lines, like a tile's text
            c.rect(46, ty + 22 + line * 20, 46 + (150 - line * 40), ty + 28 + line * 20, DIM if line == 0 else EDGE)

    # Scanlines over everything.
    for y in range(2, H, 3):
        row = c.px[y]
        for x in range(W):
            row[x] = mix(row[x], (0, 0, 0), 0.22)
    return c


def png(rows) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(v for p in row for v in p) for row in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def image() -> bytes:
    return png(draw().px)


if __name__ == "__main__":
    OUT.write_bytes(image())
    print(f"wrote {OUT}")
