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
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# cmatrix -C accepts these; the page's terminal theme turns "yellow" into
# burnt orange, since cmatrix has no orange.
COLOURS = {"green", "yellow", "red", "white"}
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# hollywood runs its panes in a tmux server, which outlives its process group:
# give it a private one (TMUX_TMPDIR) and kill that server when it stops.
TMUX_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "brick-status-crt-tmux")


@dataclass(frozen=True)
class Program:
    argv: tuple[str, ...]  # "{colour}" is replaced with the health colour
    tint: bool = True  # the page tints its colours; False: it colours itself
    env: dict = field(default_factory=dict)
    cleanup: tuple[str, ...] = ()  # run after it stops

    def available(self, cmatrix: str = "cmatrix") -> bool:
        exe = cmatrix if self.argv[0] == "cmatrix" else self.argv[0]
        return shutil.which(exe) is not None


PROGRAMS = {
    # -b bold, -s screensaver mode, -C colour.
    "cmatrix": Program(("cmatrix", "-b", "-s", "-C", "{colour}"), tint=False),
    # 20 frames a second, not 75: every character drawn is the page's work too.
    "pipes": Program(("/usr/games/pipes", "-p", "3", "-f", "20", "-r", "3000", "-R")),
    "bonsai": Program(("cbonsai", "-S", "-w", "10", "-L", "40")),
    "clock": Program(("tty-clock", "-s", "-c", "-b", "-n")),
    "nyancat": Program(("nyancat", "-n", "-s")),
    "rain": Program(("/usr/games/rain", "-d", "150")),
    "train": Program(("sh", "-c", "while :; do sl; sleep 20; done")),
    "gallery": Program((sys.executable, "-m", "brick_status.crt_gallery", "45")),
    "hollywood": Program(("hollywood",), env={"TMUX_TMPDIR": TMUX_DIR},
                         cleanup=("tmux", "kill-server")),
}

_current: tuple[subprocess.Popen, Program] | None = None
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
    env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": str(cols), "LINES": str(rows),
           **program.env}
    if program.env.get("TMUX_TMPDIR"):
        os.makedirs(program.env["TMUX_TMPDIR"], mode=0o700, exist_ok=True)
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=PACKAGE_ROOT,
                            start_new_session=True, close_fds=True)
    os.close(slave)
    with _lock:
        previous, _current = _current, (proc, program)
    if previous is not None:
        _stop(*previous)
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
        _stop(proc, program)
        os.close(master)
        with _lock:
            if _current is not None and _current[0] is proc:
                _current = None


def _stop(proc: subprocess.Popen, program: Program) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
    if program.cleanup:
        subprocess.run(program.cleanup, env={**os.environ, **program.env}, capture_output=True, timeout=10)
