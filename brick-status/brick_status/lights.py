"""Drive the plasma daemon through its command FIFO."""

import logging
import os

from . import patterns
from .checks import ACTIVE, FAIL, OK, UNKNOWN, WARN

log = logging.getLogger(__name__)


def idle_pattern(checks: list[dict]) -> str:
    """Pick the idle light pattern for the most important current event."""
    states = {c["state"] for c in checks}
    running = {c["group"] for c in checks if c["state"] == ACTIVE}
    if FAIL in states:
        return "alert"
    if "builds" in running:
        return "building"
    if "wigle" in running:
        return "uploading"
    if WARN in states:
        return "warn"
    if UNKNOWN in states or not checks:
        return "unknown"
    return "calm"


class Plasma:
    def __init__(self, fifo: str, pattern_dir: str, prefix: str):
        self.fifo = fifo
        self.pattern_dir = pattern_dir
        self.prefix = prefix
        self.current = None

    def install_idle_patterns(self) -> None:
        os.makedirs(self.pattern_dir, exist_ok=True)
        for name, make in patterns.IDLE.items():
            self._write(name, make())

    def show(self, name: str, frames: list | None = None) -> None:
        """Switch to a pattern, writing its PNG first if frames are given."""
        if frames is not None:
            self._write(name, frames)
        elif name == self.current:
            return
        # Only remember what was actually sent, so a missed command is retried.
        self.current = name if self._send(f"{self.prefix}/{name}") else None

    def off(self) -> None:
        if self.current != "off":
            self.current = "off" if self._send("0 0 0") else None

    def _write(self, name: str, frames: list) -> None:
        path = os.path.join(self.pattern_dir, f"{name}.png")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(patterns.png(frames))
        os.replace(tmp, path)

    def _send(self, command: str) -> bool:
        # O_NONBLOCK: fail fast if the daemon isn't running instead of hanging.
        try:
            fd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            log.warning("plasma FIFO %s unavailable: %s", self.fifo, e)
            return False
        try:
            os.write(fd, (command + "\n").encode())
            return True
        except OSError as e:
            log.warning("plasma FIFO write failed: %s", e)
            return False
        finally:
            os.close(fd)


def active_frames(group_states: dict[str, str], views: list[str], slots: list[int],
                  selected_view: int) -> list:
    """Frames lighting each view's button in its group's health colour."""
    colours, busy = {}, set()
    for i, view in enumerate(views):
        if i >= len(slots):
            break
        state = group_states.get(view, OK if view == "overview" else UNKNOWN)
        colours[slots[i]] = patterns.STATE_COLOURS[state]
        if state == ACTIVE:
            busy.add(slots[i])
    selected = slots[selected_view] if selected_view < len(slots) else None
    return patterns.buttons(colours, selected, busy)
