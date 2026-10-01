#!/usr/bin/env python3
"""Draw the board's pixel art as SVG.

The cluster and wigle-sync tiles' art is our own, in the chunky style of the
pixel-art Jenkins and neon cyberpunk colours, with no licence to track. The
failing Jenkins is that pixel-art Jenkins (Heungsub Lee, CC BY-SA 3.0) read from
its PNG and set on fire, so it is CC BY-SA 3.0 too (see the LICENSE file next
to it). Edit the drawings below and rerun:
    python3 brick-status/tools/pixel_art.py
which rewrites brick-status/brick_status/static/art/*.svg and
static/vendor/jenkins/pixelart-fire.svg.
"""

import struct
import zlib
from pathlib import Path

SIZE = 24
STATIC = Path(__file__).resolve().parents[1] / "brick_status" / "static"
OUT = STATIC / "art"
JENKINS = STATIC / "vendor" / "jenkins"

PALETTE = {
    "K": "#0b0f1a",  # outline
    "D": "#1c2233",  # body, dark
    "M": "#2b3550",  # body
    "L": "#44547c",  # body, light
    "W": "#e8f6ff",  # shine, sparkle
    "C": "#00f0ff",  # neon cyan
    "P": "#ff2fb3",  # neon magenta
    "G": "#39ff88",  # green LED, glow
    "R": "#ff3b3b",  # red LED, spark
    "Y": "#ffe14a",  # flame core
    "O": "#ff8a1f",  # flame
    "F": "#e8361c",  # flame edge
    "S": "#6b7182",  # smoke
    "B": "#05070d",  # screen
    "A": "#ffb020",  # amber: warning
    "H": "#ff8f8f",  # horn, highlight
    "N": "#d81e1e",  # horn
    "i": "#f0d6b7",  # Jenkins' skin
}


class Canvas:
    """Pixels by position; a colour is a PALETTE letter or a "#rrggbb"."""

    def __init__(self, size=SIZE):
        self.size = size
        self.px: dict[tuple[int, int], str] = {}

    def dot(self, x, y, c):
        if 0 <= x < self.size and 0 <= y < self.size:
            self.px[(x, y)] = c

    def rect(self, x, y, w, h, c):
        for i in range(x, x + w):
            for j in range(y, y + h):
                self.dot(i, j, c)

    def draw(self, x, y, rows: str):
        """Paste an ASCII drawing (palette letters, '.' transparent) at x, y."""
        for j, row in enumerate(rows.strip("\n").splitlines()):
            for i, ch in enumerate(row):
                if ch != ".":
                    self.dot(x + i, y + j, ch)

    def svg(self) -> str:
        rects = "".join(f'<rect x="{x}" y="{y}" width="1" height="1" fill="{PALETTE.get(c, c)}"/>'
                        for (x, y), c in sorted(self.px.items(), key=lambda p: (p[0][1], p[0][0])))
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.size} {self.size}" '
                f'shape-rendering="crispEdges">{rects}</svg>\n')


FACES = {  # the server's screen, by mood
    "ok": """
........
..C...C.
.C.C.C.C
........
.C....C.
..CCCC..
""",
    "warn": """
........
.AA..AA.
..A...A.
........
..A.A.A.
.A.A.A..
""",
    "fail": """
........
.R.R.R.R
..R...R.
.R.R.R.R
........
..RRRR..
""",
}


def server(c: Canvas, mood: str):
    """A cartoon server tower with a face on its screen; mood is ok, warn or fail."""
    trim = {"ok": ("C", "P"), "warn": ("A", "A"), "fail": ("F", "F")}[mood]
    leds = {"ok": ("G", "G"), "warn": ("A", "G"), "fail": ("R", "R")}[mood]
    c.rect(5, 5, 14, 17, "K")          # outline
    c.rect(6, 6, 12, 15, "M")          # body
    c.rect(6, 6, 12, 1, "L")           # top bevel
    c.rect(6, 7, 1, 13, trim[0])       # neon trim down both sides
    c.rect(17, 7, 1, 13, trim[1])
    c.rect(8, 8, 8, 6, "B")            # screen, with a face
    c.draw(8, 8, FACES[mood])
    for y, led in zip((15, 18), leds):  # drive bays with LEDs
        c.rect(8, y, 8, 2, "D")
        c.rect(9, y + 1, 4, 1, "K")
        c.rect(14, y, 2, 1, led)
    c.rect(7, 22, 2, 1, "K")           # feet
    c.rect(15, 22, 2, 1, "K")


def healthy_server() -> Canvas:
    c = Canvas()
    server(c, "ok")
    c.draw(7, 7, "W\nW\n")             # shine
    c.dot(16, 20, "W")
    for x, y, col in ((2, 4, "W"), (21, 9, "C"), (2, 15, "P"), (20, 18, "W")):  # sparkles
        c.draw(x - 1, y - 1, f".{col}.\n{col}W{col}\n.{col}.")
    return c


def burning_server() -> Canvas:
    c = Canvas()
    server(c, "fail")
    c.draw(3, 0, """
....F.......F...
...FOF..F..FOF..
..FOYOF.OF.FYOF.
..FOYYOFOOFOYYOF
.FOYYYOOYYOOYYOF
FOOYYOOYYYOOYOOF
""")
    c.draw(1, 9, "F.\nOF\nYO\nOF")       # flames licking the sides
    c.draw(20, 12, ".F\nFO\nOY\nFO")
    c.draw(0, 0, "SS\nS.")             # smoke
    c.draw(20, 0, ".SSS\nSS.S\n.S..")
    return c


def degraded_server() -> Canvas:
    """Still standing, but sweating it: something in the cluster needs a look."""
    c = Canvas()
    server(c, "warn")
    c.draw(19, 3, ".C\nCW\nCC")       # sweat drop
    c.draw(2, 2, "A\nA\nA\n.\nA")     # a warning "!"
    return c


def plug(c: Canvas, x: int):
    """The plug: a magenta body (x .. x+6) with two metal prongs to its right."""
    c.rect(x, 6, 7, 12, "K")
    c.rect(x + 1, 7, 5, 10, "P")
    c.rect(x + 2, 8, 1, 3, "W")        # shine
    c.rect(x + 7, 8, 3, 2, "L")        # prongs
    c.rect(x + 7, 14, 3, 2, "L")
    c.rect(x + 7, 8, 3, 1, "W")
    c.rect(x + 7, 14, 3, 1, "W")


def socket(c: Canvas, x: int, holes: bool):
    """The socket: a cyan body (x .. x+6), its two holes facing left."""
    c.rect(x, 6, 7, 12, "K")
    c.rect(x + 1, 7, 5, 10, "C")
    c.rect(x + 4, 8, 1, 3, "W")        # shine
    if holes:
        c.rect(x, 8, 2, 2, "B")
        c.rect(x, 14, 2, 2, "B")


def plugs(connected: bool) -> Canvas:
    c = Canvas()
    if connected:                      # pushed together, glowing green
        c.rect(0, 11, 3, 2, "P")       # cables
        c.rect(19, 11, 5, 2, "C")
        plug(c, 2)
        socket(c, 12, holes=False)     # the prongs disappear into it
        c.rect(9, 8, 3, 2, "L"); c.rect(9, 14, 3, 2, "L")
        for x, y in ((11, 3), (11, 20), (4, 2), (18, 21)):
            c.draw(x - 1, y - 1, ".G.\nGWG\n.G.")
    else:                              # pulled apart, sparking
        c.rect(0, 11, 1, 2, "P")
        c.rect(22, 11, 2, 2, "C")
        plug(c, 0)
        socket(c, 15, holes=True)
        c.draw(11, 7, """
..R.
.R..
RRRR
..R.
.R..
""")
        c.draw(11, 14, ".R.\nR.R\n..R")
        c.dot(12, 3, "R"); c.dot(13, 20, "R")
    return c


def read_png(path: Path, block: int) -> dict[tuple[int, int], str]:
    """The pixels of a palette PNG drawn in block x block squares (the pixel-art
    Jenkins is 32x32 blown up 8x), one per square; transparent ones left out."""
    data, pos, idat, trns = path.read_bytes(), 8, b"", b""
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            w, h, depth, colour_type, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
            assert (depth, colour_type, interlace) == (8, 3, 0), "expects an 8-bit palette PNG"
        elif kind == b"PLTE":
            palette = [f"#{chunk[i]:02x}{chunk[i + 1]:02x}{chunk[i + 2]:02x}" for i in range(0, n, 3)]
        elif kind == b"tRNS":
            trns = chunk
        elif kind == b"IDAT":
            idat += chunk
    raw, prev, rows = zlib.decompress(idat), bytes(w), []
    for y in range(h):                 # undo each scanline's PNG filter
        kind, line = raw[y * (w + 1)], bytearray(raw[y * (w + 1) + 1:(y + 1) * (w + 1)])
        for x in range(w):
            a, b, c = (line[x - 1] if x else 0), prev[x], (prev[x - 1] if x else 0)
            p = a + b - c
            predict = [0, a, b, (a + b) // 2,
                       a if abs(p - a) <= abs(p - b) and abs(p - a) <= abs(p - c) else b if abs(p - b) <= abs(p - c) else c]
            line[x] = (line[x] + predict[kind]) & 255
        rows.append(bytes(line))
        prev = line
    return {(x // block, y // block): palette[rows[y][x]]
            for y in range(0, h, block) for x in range(0, w, block)
            if rows[y][x] >= len(trns) or trns[rows[y][x]]}


def jenkins_on_fire() -> Canvas:
    """The pixel-art Jenkins with devil horns and glowing eyes, in flames: the
    Jenkins tile when a job failed. Drawn on a bigger canvas, room for the horns."""
    c = Canvas(36)
    x0, y0 = 2, 4                      # where the 32x32 butler sits
    c.draw(x0, y0, """
..F.............................
.FOF.....F.............F.....F..
.FYOF...FOF...........FOF...FOF.
FOYOF..FOYF...........FYOF..FOYF
FOYYOF.FYOF...........FOYF.FOYOF
FOYYOFFOYYOF.........FOYYOFFOYYO
.FOYYOFOYYOF.........FOYYOFOYYOF
.FOYYYOOYYOF.........FOYYOOYYYOF
FOYYYYOYYYOF.........FOYYYOYYYOF
FOYYYYYYYYOF.........FOYYYYYYYOF
FOOYYYYYYOF...........FOYYYYYYOF
.FOYYYYYYOF...........FOYYYYYOF.
.FOOYYYYOF.............FOYYYYOF.
FOOYYYYOOF.............FOOYYYOF.
FOYYYYYOF...............FOYYYOOF
FOYYYYYOF...............FOYYYYOF
FOYYYYYOF...............FOYYYYOF
FOYYYYYOF...............FOYYYYOF
FOOYYYYOF...............FOYYYOOF
.FOYYYOF................FOYYYOF.
.FOOYYOF.................FOYYOF.
..FOYYOOF...............FOOYOF..
..FOOYYOF...............FOYYOF..
..FFOYYOF...............FOYOF...
...FOOYOF...............FOOF....
...FFOOF................FOF.....
""")                                   # flames behind him
    glow = {"#ef3d3a": PALETTE["O"], "#d33833": PALETTE["F"]}  # his red disc catches fire
    for (x, y), colour in read_png(JENKINS / "pixelart.png", 8).items():
        c.dot(x0 + x, y0 + y, glow.get(colour, colour))
    c.draw(x0 + 13, y0 + 5, """
.KK......KK
iiKK...KKii
iiiii.iiiii
.KRYK.KYRK.
..KK...KK..
""")                                   # dastardly brows over narrowed, glowing eyes
    horn = """
K.......
KK......
KNK.....
KNHK....
.KNHK...
.KNNHKK.
..KNNNNK
...KKNNK
"""
    c.draw(8, 0, horn)
    c.draw(20, 0, "\n".join(row[::-1] for row in horn.strip("\n").splitlines()))
    return c


def drawings() -> dict[Path, Canvas]:
    return {OUT / "server-ok.svg": healthy_server(), OUT / "server-warn.svg": degraded_server(),
            OUT / "server-fire.svg": burning_server(),
            OUT / "plugs-connected.svg": plugs(True), OUT / "plugs-disconnected.svg": plugs(False),
            JENKINS / "pixelart-fire.svg": jenkins_on_fire()}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for path, canvas in drawings().items():
        path.write_text(canvas.svg())
        print(f"wrote {path.name} ({len(canvas.px)} pixels)")


if __name__ == "__main__":
    main()
