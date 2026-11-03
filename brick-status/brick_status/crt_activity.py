"""What's running, in the CRT window, typed out as it starts.

Run in the CRT's pseudo-terminal by matrix.py like any other program, while the
page sees activity:

    python3 -m brick_status.crt_activity

One thing running: its title typed a key at a time, then its status and details.
More than one: only their titles. Below, filling the rest of the screen, the
newest one's log scrolls by, when it has one we can read (crt_logs.py). The plasma API holds what senders say about
themselves (brick-plasma's web.py, "show"); the page tints this with the board's
health colour, like every program.
"""

import json
import os
import shutil
import sys
import time
import urllib.request
from collections import deque

from . import crt_logs

KEY_SECONDS = 0.1  # one letter: slow enough to watch
POLL_SECONDS = 0.5
TOP, LEFT = 2, 3  # the first title's row, and the left margin
PROMPT = "> "
LOG_SECONDS = 1.0  # how often the log is read
LOG_MIN_ROWS = 3  # less room than this, and the log isn't worth showing
URL = os.environ.get("BRICK_STATUS_PLASMA_URL", "http://127.0.0.1:8766").rstrip("/")


def elapsed(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


class Screen:
    """Draws what's running, typing only what hasn't been typed yet."""

    def __init__(self, cols: int, write, sleep=time.sleep, clock=time.monotonic, rows: int = 24,
                 log_for=lambda item: None):
        self.cols, self.write, self.sleep, self.clock = cols, write, sleep, clock
        self.rows, self.log_for = rows, log_for
        self.typed: dict[str, str] = {}  # source to title, in the order they appeared
        self.since: dict[str, float] = {}
        self.below: list[str] | None = None
        self.log_source: str | None = None
        self.log = None
        self.log_lines: deque[str] = deque(maxlen=max(rows, 1))
        self.log_read = float("-inf")
        self.log_drawn: tuple | None = None  # (first row, lines) last drawn

    def _at(self, row: int, text: str = "") -> None:
        self.write(f"\x1b[{row};{LEFT}H\x1b[2K{text}")

    def _redraw(self) -> None:
        self.write("\x1b[2J")
        for row, title in enumerate(self.typed.values(), TOP):
            self._at(row, f"\x1b[1m{PROMPT}{self._fit(title)}\x1b[0m")
        self.below = None
        self.log_drawn = None

    def _fit(self, title: str) -> str:
        return title[:max(self.cols - LEFT - len(PROMPT) - 1, 1)]

    def _type(self, source: str, title: str) -> None:
        self._at(TOP + len(self.typed), f"\x1b[1m{PROMPT}")
        self.write("\x1b[?25h")
        for letter in self._fit(title):
            self.write(letter)
            self.sleep(KEY_SECONDS)
        self.write("\x1b[0m\x1b[?25l")
        self.typed[source] = title

    def _details(self, item: dict) -> list[str]:
        lines = [f"status   {item['status']}"] if item.get("status") else []
        lines += [f"{name:<8} {value}" for name, value in item.get("details", {}).items()]
        lines.append(f"{'running':<8} {elapsed(self.clock() - self.since[item['source']])}")
        return [line[:max(self.cols - LEFT - 1, 1)] for line in lines]

    def update(self, items: list[dict]) -> None:
        running = {i["source"]: i for i in items}
        now = self.clock()
        for source in running:
            self.since.setdefault(source, now)
        gone = [s for s, title in self.typed.items() if running.get(s, {}).get("title") != title]
        if gone:
            for source in gone:
                del self.typed[source]
                if source not in running:
                    del self.since[source]
            self._redraw()
        for source, item in running.items():
            if source not in self.typed:
                if self.below:  # a second job arrived: its first's details go
                    self._redraw()
                self._type(source, item["title"])
        below = self._details(items[0]) if len(items) == 1 else []
        if below != self.below:
            first = TOP + len(self.typed) + 1
            for row in range(len(self.below or [])):
                self._at(first + row)
            for row, line in enumerate(below):
                self._at(first + row, line)
            self.below = below
        self._follow(items)

    def _follow(self, items: list[dict]) -> None:
        """The newest running item's log, in the rows left below everything else."""
        newest = next((s for s in reversed(list(self.typed)) if any(i["source"] == s for i in items)), None)
        if newest != self.log_source:
            self.log_source = newest
            item = next((i for i in items if i["source"] == newest), None)
            self.log = self.log_for(item) if item else None
            self.log_lines.clear()
            self.log_read = float("-inf")
        now = self.clock()
        if self.log is not None and now - self.log_read >= LOG_SECONDS:
            self.log_read = now
            self.log_lines.extend(line for line in self.log.poll() if line.strip())
        self._draw_log()

    def _draw_log(self) -> None:
        start = TOP + len(self.typed) + 1 + (len(self.below) + 1 if self.below else 0)
        height = self.rows - start - 1  # a header row, then the lines; the last row stays free
        shown = tuple(list(self.log_lines)[-height:]) if self.log is not None and height >= LOG_MIN_ROWS else ()
        if (start, shown) == self.log_drawn:
            return
        drawn_start = self.log_drawn[0] if self.log_drawn else start
        for row in range(min(start, drawn_start), self.rows):  # clear what was there
            self._at(row)
        if shown:
            width = max(self.cols - LEFT - 1, 1)
            self._at(start, f"\x1b[2m{'-- log ':-<{width}}\x1b[0m")
            for row, line in enumerate(shown, start + 1):
                self._at(row, f"\x1b[2m{line[:width]}\x1b[0m")
        self.log_drawn = (start, shown)


def fetch() -> list[dict]:
    try:
        with urllib.request.urlopen(f"{URL}/activity", timeout=1) as reply:
            return json.load(reply)["activity"]
    except (OSError, ValueError, KeyError):
        return []  # the page goes back to the playlist


def main() -> None:
    def write(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    write("\x1b[2J\x1b[?25l")
    size = shutil.get_terminal_size()
    screen = Screen(size.columns, write, rows=size.lines, log_for=crt_logs.for_item)
    while True:
        screen.update(fetch())
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
