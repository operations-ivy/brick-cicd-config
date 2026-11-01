"""What's running, in the CRT window, typed out as it starts.

Run in the CRT's pseudo-terminal by matrix.py like any other program, while the
page sees activity:

    python3 -m brick_status.crt_activity

One thing running: its title typed a key at a time, then its status and details.
More than one: only their titles. The plasma API holds what senders say about
themselves (brick-plasma's web.py, "show"); the page tints this with the board's
health colour, like every program.
"""

import json
import os
import shutil
import sys
import time
import urllib.request

KEY_SECONDS = 0.1  # one letter: slow enough to watch
POLL_SECONDS = 0.5
TOP, LEFT = 2, 3  # the first title's row, and the left margin
PROMPT = "> "
URL = os.environ.get("BRICK_STATUS_PLASMA_URL", "http://127.0.0.1:8766").rstrip("/")


def elapsed(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


class Screen:
    """Draws what's running, typing only what hasn't been typed yet."""

    def __init__(self, cols: int, write, sleep=time.sleep, clock=time.monotonic):
        self.cols, self.write, self.sleep, self.clock = cols, write, sleep, clock
        self.typed: dict[str, str] = {}  # source to title, in the order they appeared
        self.since: dict[str, float] = {}
        self.below: list[str] | None = None

    def _at(self, row: int, text: str = "") -> None:
        self.write(f"\x1b[{row};{LEFT}H\x1b[2K{text}")

    def _redraw(self) -> None:
        self.write("\x1b[2J")
        for row, title in enumerate(self.typed.values(), TOP):
            self._at(row, f"\x1b[1m{PROMPT}{self._fit(title)}\x1b[0m")
        self.below = None

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
    screen = Screen(shutil.get_terminal_size().columns, write)
    while True:
        screen.update(fetch())
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
