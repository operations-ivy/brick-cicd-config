"""RetroArch's menu wallpaper for brick9000, drawn like the board it opens
over (brick-status's page since the redesign, #45: its CSS tokens, grid,
raster, wordmark and panels):

- near-black void with the board's grid, a header band and a footer band;
- the BRICK//NET wordmark in the footer, cyan with "//" in magenta, and the
  logo's magenta/cyan offset and glow, in a chunky pixel font;
- the CRT panel on the right, under its magenta label, where XMB draws a
  game's box art;
- the board's vignette, and its raster (a dark line every 3 px) over it all.

XMB draws its title, clock, icons and lists over this, in VT323 and the
board's text colour (retroarch.cfg). Rerun after changing it; a test checks
the committed PNG matches:

    python3 brick-retro/tools/wallpaper.py
"""

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "brick9000/retroarch/wallpaper.png"
W, H = 1152, 864  # brick9000's screen

Color = tuple[int, int, int]

# brick-status's CSS tokens (static/index.html, :root).
VOID = (0x04, 0x05, 0x0A)
PANEL = (0x0A, 0x0E, 0x19)
EDGE = (0x1A, 0x24, 0x42)
GRID = (0x0E, 0x16, 0x30)
GLASS = (0x02, 0x03, 0x06)  # .crt .glass
CYAN = (0x00, 0xEA, 0xFF)
MAGENTA = (0xFF, 0x2B, 0xD6)

GRID_PX = 64  # the board's 2.2em grid at its font size on this screen
HEADER, FOOTER = 66, 792  # the header band's bottom, the footer band's top
LABEL_SCALE = 3  # the panel labels' pixel size

# A 5x7 pixel font, only the letters the wallpaper needs.
GLYPHS = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11100", "10010", "10001", "10001", "10001", "10010", "11100"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
}


def mix(a: Color, b: Color, t: float) -> Color:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def width(s: str, scale: int) -> int:
    return len(s) * 6 * scale - scale


def label_height(scale: int = LABEL_SCALE) -> int:
    """A label chip's height: the 7-row letters and their padding above and below."""
    return 7 * scale + 4 * scale


class Canvas:
    def __init__(self) -> None:
        self.px = [[VOID] * W for _ in range(H)]

    def rect(self, x0: int, y0: int, x1: int, y1: int, c: Color) -> None:
        for y in range(max(0, y0), min(H, y1)):
            row = self.px[y]
            for x in range(max(0, x0), min(W, x1)):
                row[x] = c

    def blend(self, x0: int, y0: int, x1: int, y1: int, c: Color, t: float) -> None:
        for y in range(max(0, y0), min(H, y1)):
            row = self.px[y]
            for x in range(max(0, x0), min(W, x1)):
                row[x] = mix(row[x], c, t)

    def frame(self, x0: int, y0: int, x1: int, y1: int, c: Color, thickness: int = 2) -> None:
        self.rect(x0, y0, x1, y0 + thickness, c)
        self.rect(x0, y1 - thickness, x1, y1, c)
        self.rect(x0, y0, x0 + thickness, y1, c)
        self.rect(x1 - thickness, y0, x1, y1, c)

    def text(self, s: str, x: int, y: int, scale: int, colour: Color,
             colours: dict[int, Color] | None = None) -> None:
        """Pixel-font text; `colours` gives some letters their own (the "//")."""
        colours = colours or {}
        for i, ch in enumerate(s):
            c = colours.get(i, colour)
            cx = x + i * 6 * scale
            for gy, row in enumerate(GLYPHS[ch]):
                for gx, on in enumerate(row):
                    if on == "1":
                        self.rect(cx + gx * scale, y + gy * scale, cx + (gx + 1) * scale, y + (gy + 1) * scale, c)

    def glow(self, x0: int, y0: int, x1: int, y1: int, c: Color, radius: int, strength: float) -> None:
        """A soft halo around a box, like the board's text-shadow glow."""
        for y in range(max(0, y0 - radius), min(H, y1 + radius)):
            row = self.px[y]
            dy = max(y0 - y, 0, y - y1 + 1)
            for x in range(max(0, x0 - radius), min(W, x1 + radius)):
                dx = max(x0 - x, 0, x - x1 + 1)
                d = math.hypot(dx, dy)
                if d < radius:
                    row[x] = mix(row[x], c, strength * (1 - d / radius) ** 2)

    def vignette(self) -> None:
        """Dark from 60% of the way to the corners out, like the board's."""
        corner = math.sqrt(2)
        for y in range(H):
            row = self.px[y]
            dy = (y - H / 2) / (H / 2)
            for x in range(W):
                r = math.hypot((x - W / 2) / (W / 2), dy) / corner
                if r > 0.6:
                    row[x] = mix(row[x], (0, 0, 0), 0.6 * (r - 0.6) / 0.4)

    def raster(self) -> None:
        """The board's scanlines: a dark line every 3 px."""
        for y in range(0, H, 3):
            row = self.px[y]
            for x in range(W):
                row[x] = mix(row[x], (0, 0, 0), 0.28)


def wordmark(c: Canvas, s: str, x: int, y: int, scale: int) -> None:
    """The board's .logo: cyan, "//" in magenta, a magenta copy offset right,
    a cyan one offset left (text-shadow 0.06em each way), and a cyan glow."""
    slashes = {i: MAGENTA for i, ch in enumerate(s) if ch == "/"}
    off = max(1, scale // 2)
    c.glow(x, y, x + width(s, scale), y + 7 * scale, CYAN, 3 * scale, 0.35)
    c.text(s, x + off, y, scale, MAGENTA)
    c.text(s, x - off, y, scale, CYAN)
    c.text(s, x, y, scale, CYAN, slashes)


def label(c: Canvas, s: str, x: int, y: int, colour: Color, scale: int = LABEL_SCALE) -> None:
    """A panel's label chip (.panel > h2): the colour as a block, the text dark on it."""
    pad = scale * 2
    c.rect(x, y, x + width(s, scale) + 2 * pad, y + label_height(scale), colour)
    c.text(s, x + pad, y + pad, scale, VOID)


def draw() -> Canvas:
    c = Canvas()

    # The board's grid on the void.
    for x in range(0, W, GRID_PX):
        c.rect(x, 0, x + 1, H, GRID)
    for y in range(0, H, GRID_PX):
        c.rect(0, y, W, y + 1, GRID)

    # Header and footer bands, as on the board: near-opaque void over the grid,
    # a 2 px edge between them and the rest.
    c.blend(0, 0, W, HEADER, VOID, 0.85)
    c.rect(0, HEADER, W, HEADER + 2, EDGE)
    c.rect(0, FOOTER, W, FOOTER + 2, EDGE)
    c.blend(0, FOOTER + 2, W, H, VOID, 0.85)

    # The CRT panel on the right, where XMB puts a game's box art: the board's
    # panel (fill, 2 px edge), its magenta label, and the dark glass inside.
    # XMB draws box art at about x 781-1138, y 240-600 on this screen; the
    # label stays below XMB's row of icons (to y 200).
    x0, y0, x1, y1 = 768, 226, 1148, 776
    c.rect(x0, y0, x1, y1, PANEL)
    c.frame(x0, y0, x1, y1, EDGE)
    label(c, "CRT", x0 + 16, y0 - 16, MAGENTA)
    c.rect(x0 + 8, y0 + 22, x1 - 8, y1 - 8, GLASS)
    c.frame(x0 + 8, y0 + 22, x1 - 8, y1 - 8, EDGE, 1)

    # The vignette, under the wordmark so it stays bright.
    c.vignette()

    # The wordmark in the footer, right-aligned, with ARCADE in a magenta chip.
    scale = 5
    mark = "BRICK//NET"
    band = H - FOOTER - 2
    mx = W - 32 - width(mark, scale)
    wordmark(c, mark, mx, FOOTER + 2 + (band - 7 * scale) // 2, scale)
    gap = 44  # between the chip and the wordmark
    label(c, "ARCADE", mx - gap - width("ARCADE", LABEL_SCALE), FOOTER + 2 + (band - label_height()) // 2, MAGENTA)

    # The raster over everything.
    c.raster()
    return c


def png(rows) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + bytes(v for p in row for v in p) for row in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def image() -> bytes:
    return png(draw().px)


if __name__ == "__main__":
    OUT.write_bytes(image())
    print(f"wrote {OUT}")
