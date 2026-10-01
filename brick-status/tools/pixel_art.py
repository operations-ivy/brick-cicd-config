#!/usr/bin/env python3
"""Draw the board's original pixel art (cluster and wigle-sync tiles) as SVG.

Same chunky style as the pixel-art Jenkins on the Jenkins tile, in neon
cyberpunk colours. Our own art, so no licence to track. Edit the drawings
below and rerun:
    python3 brick-status/tools/pixel_art.py
which rewrites brick-status/brick_status/static/art/*.svg.
"""

from pathlib import Path

SIZE = 24
OUT = Path(__file__).resolve().parents[1] / "brick_status" / "static" / "art"

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
}


class Canvas:
    def __init__(self):
        self.px: dict[tuple[int, int], str] = {}

    def dot(self, x, y, c):
        if 0 <= x < SIZE and 0 <= y < SIZE:
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
        rects = "".join(f'<rect x="{x}" y="{y}" width="1" height="1" fill="{PALETTE[c]}"/>'
                        for (x, y), c in sorted(self.px.items(), key=lambda p: (p[0][1], p[0][0])))
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" '
                f'shape-rendering="crispEdges">{rects}</svg>\n')


def server(c: Canvas, healthy: bool):
    """A cartoon server tower with a face on its screen."""
    c.rect(5, 5, 14, 17, "K")          # outline
    c.rect(6, 6, 12, 15, "M")          # body
    c.rect(6, 6, 12, 1, "L")           # top bevel
    c.rect(6, 7, 1, 13, "C" if healthy else "F")   # neon trim down both sides
    c.rect(17, 7, 1, 13, "P" if healthy else "F")
    c.rect(8, 8, 8, 6, "B")            # screen, with a face
    c.draw(8, 8, """
........
..C...C.
.C.C.C.C
........
.C....C.
..CCCC..
""" if healthy else """
........
.R.R.R.R
..R...R.
.R.R.R.R
........
..RRRR..
""")
    for y in (15, 18):                 # drive bays with LEDs
        c.rect(8, y, 8, 2, "D")
        c.rect(9, y + 1, 4, 1, "K")
        c.rect(14, y, 2, 1, "G" if healthy else "R")
    c.rect(7, 22, 2, 1, "K")           # feet
    c.rect(15, 22, 2, 1, "K")


def healthy_server() -> Canvas:
    c = Canvas()
    server(c, healthy=True)
    c.draw(7, 7, "W\nW\n")             # shine
    c.dot(16, 20, "W")
    for x, y, col in ((2, 4, "W"), (21, 9, "C"), (2, 15, "P"), (20, 18, "W")):  # sparkles
        c.draw(x - 1, y - 1, f".{col}.\n{col}W{col}\n.{col}.")
    return c


def burning_server() -> Canvas:
    c = Canvas()
    server(c, healthy=False)
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


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, canvas in {"server-ok": healthy_server(), "server-fire": burning_server(),
                         "plugs-connected": plugs(True), "plugs-disconnected": plugs(False)}.items():
        (OUT / f"{name}.svg").write_text(canvas.svg())
        print(f"wrote {name}.svg ({len(canvas.px)} pixels)")


if __name__ == "__main__":
    main()
