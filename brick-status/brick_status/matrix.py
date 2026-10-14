"""Run the CRT window's program in a pseudo-terminal and stream its output to
the page.

The page draws the stream with xterm.js, so these are the real programs,
sized to the window they appear in. cmatrix colours itself with the health
colour; the others are tinted by the page (every terminal colour remapped to
a shade of it, by brightness), so the colour keeps its meaning whatever runs.
"""

import fcntl
import logging
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
from collections.abc import Iterator
from dataclasses import dataclass

log = logging.getLogger(__name__)

# cmatrix -C accepts these; the page's terminal theme turns "yellow" into
# burnt orange, since cmatrix has no orange.
COLOURS = {"green", "yellow", "red", "white"}
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass(frozen=True)
class Program:
    argv: tuple[str, ...]  # "{colour}" is replaced with the health colour
    tint: bool = True  # the page tints its colours; False: it colours itself
    # The terminal's font size in pixels: smaller shows more of programs that
    # draw at a fixed size (nyancat).
    font: int = 11

    def available(self, cmatrix: str = "cmatrix") -> bool:
        exe = cmatrix if self.argv[0] == "cmatrix" else self.argv[0]
        return shutil.which(exe) is not None


PROGRAMS = {
    # -b bold, -s screensaver mode, -C colour.
    "cmatrix": Program(("cmatrix", "-b", "-s", "-C", "{colour}"), tint=False),
    # 20 frames a second, not 75: every character drawn is the page's work too.
    "pipes": Program(("/usr/games/pipes", "-p", "3", "-f", "20", "-r", "3000", "-R")),
    "bonsai": Program(("cbonsai", "-S", "-w", "10", "-L", "40"), font=9),
    "clock": Program(("tty-clock", "-s", "-c", "-b", "-n"), font=8),
    # nyancat crops its fixed-size animation to the terminal: a small font shows all of it.
    "nyancat": Program(("nyancat", "-n", "-s"), font=6),
    "rain": Program(("/usr/games/rain", "-d", "150")),
    "gallery": Program((sys.executable, "-m", "brick_status.crt_gallery", "45"), font=7),
    # Not hollywood: whatever its settings, its widgets (atop, htop, tree, ...)
    # held the Pi 4 at 96% CPU (#28).
}

_current: subprocess.Popen | None = None
_lock = threading.Lock()


def stream(name: str, cols: int, rows: int, colour: str, cmatrix: str = "cmatrix") -> Iterator[bytes]:
    """Yield the program's terminal output until the caller stops iterating.

    Only one program runs at a time: starting a new stream (a resize, a colour
    change, the next program) stops the previous one.
    """
    global _current
    program = PROGRAMS.get(name, PROGRAMS["cmatrix"])
    cols, rows = max(10, min(cols, 400)), max(5, min(rows, 200))
    colour = colour if colour in COLOURS else "green"
    argv = [a.replace("{colour}", colour) for a in program.argv]
    if argv[0] == "cmatrix":
        argv[0] = cmatrix

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": str(cols), "LINES": str(rows)}
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=PACKAGE_ROOT,
                            start_new_session=True, close_fds=True)
    os.close(slave)
    with _lock:
        previous, _current = _current, proc
    if previous is not None:
        _stop(previous)
    log.info("%s started (%dx%d, %s)", name, cols, rows, colour)

    try:
        while proc.poll() is None:
            ready, _, _ = select.select([master], [], [], 1.0)
            if not ready:
                continue
            try:
                data = os.read(master, 65536)
            except OSError:  # EIO once the program exits
                break
            if not data:
                break
            yield data
    finally:
        _stop(proc)
        os.close(master)
        with _lock:
            if _current is proc:
                _current = None


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
