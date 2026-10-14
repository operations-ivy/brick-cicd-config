"""Public-domain paintings in the CRT window, drawn with chafa, one at a time.

Run in the CRT's pseudo-terminal by matrix.py like any other program:

    python3 -m brick_status.crt_gallery [SECONDS_EACH]

Each painting is drawn once (so it costs nothing while it hangs) in 256
colours, which the page tints with the board's health colour, with its title
and artist on the bottom row. The paintings and their credits are in
brick-status/art/paintings/.
"""

import os
import random
import shutil
import subprocess
import sys
import time

PAINTINGS = os.path.join(os.path.dirname(__file__), "..", "art", "paintings")


def credits() -> dict[str, str]:
    """File name to "Title, Artist, year", from CREDITS.md's table."""
    found = {}
    with open(os.path.join(PAINTINGS, "CREDITS.md")) as f:
        for line in f:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 4 and cells[0].startswith("`") and cells[0].endswith(".jpg`"):
                found[cells[0].strip("`")] = f"{cells[1]}, {cells[2]}, {cells[3]}"
    return found


def show(path: str, caption: str, cols: int, rows: int) -> None:
    sys.stdout.write("\x1b[2J\x1b[H\x1b[?25l")  # clear, home, hide the cursor
    sys.stdout.flush()
    # Polite: no queries to a terminal that will never answer.
    art = subprocess.run(["chafa", "--polite", "on", "-f", "symbols", "-c", "256",
                          "-s", f"{cols}x{max(rows - 1, 1)}", "--align", "center,center", path],
                         capture_output=True, text=True, timeout=30).stdout
    sys.stdout.write(art.rstrip("\n"))
    sys.stdout.write(f"\x1b[{rows};1H\x1b[2m{caption[:cols].center(cols)}\x1b[0m")
    sys.stdout.flush()


def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 45
    names = credits()
    files = sorted(n for n in os.listdir(PAINTINGS) if n.endswith(".jpg"))
    random.shuffle(files)
    while True:
        for name in files:
            cols, rows = shutil.get_terminal_size()
            show(os.path.join(PAINTINGS, name), names.get(name, name), cols, rows)
            time.sleep(seconds)


if __name__ == "__main__":
    main()
