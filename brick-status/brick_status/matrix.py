"""Run cmatrix in a pseudo-terminal and stream its output to the page.

The page draws the stream with xterm.js, so this is the real cmatrix, sized
to the window it appears in.
"""

import fcntl
import logging
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading
from collections.abc import Iterator

log = logging.getLogger(__name__)

# cmatrix -C accepts these; the page's terminal theme turns "yellow" into
# burnt orange, since cmatrix has no orange.
COLOURS = {"green", "yellow", "red", "white"}

_current: subprocess.Popen | None = None
_lock = threading.Lock()


def stream(cmatrix: str, cols: int, rows: int, colour: str) -> Iterator[bytes]:
    """Yield cmatrix's terminal output until the caller stops iterating.

    Only one cmatrix runs at a time: starting a new stream (a resize, or a
    colour change) stops the previous one.
    """
    global _current
    cols, rows = max(10, min(cols, 400)), max(5, min(rows, 200))
    colour = colour if colour in COLOURS else "green"

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": str(cols), "LINES": str(rows)}
    # -b bold, -s screensaver mode, -C colour.
    proc = subprocess.Popen([cmatrix, "-b", "-s", "-C", colour], stdin=slave, stdout=slave,
                            stderr=slave, env=env, start_new_session=True, close_fds=True)
    os.close(slave)
    with _lock:
        previous, _current = _current, proc
    if previous is not None:
        _stop(previous)
    log.info("cmatrix started (%dx%d, %s)", cols, rows, colour)

    try:
        while proc.poll() is None:
            ready, _, _ = select.select([master], [], [], 1.0)
            if not ready:
                continue
            try:
                data = os.read(master, 65536)
            except OSError:  # EIO once cmatrix exits
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
