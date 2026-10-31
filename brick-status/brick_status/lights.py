"""Drive the plasma daemon through its command FIFO."""

import logging
import os

from . import patterns
from .checks import ACTIVE, FAIL, OK, UNKNOWN, WARN

log = logging.getLogger(__name__)

# Plasma stage kinds, least to most important (checks.PLASMA_STAGES).
STAGE_ORDER = ["checkout", "install", "test", "verify", "build", "push", "deploy"]


def idle_pattern(checks: list[dict]) -> str:
    """Pick the idle light pattern for the most important current event."""
    states = {c["state"] for c in checks}
    running = {c["group"] for c in checks if c["state"] == ACTIVE}
    # The nightly test run gets the show, even over a failure: it lasts minutes.
    if any(c["group"] == "builds" and c["name"] == "test-all" and c["state"] == ACTIVE for c in checks):
        return "nightly"
    if FAIL in states:
        return "alert"
    # A build marked by the plasma library: its stage's pattern. With several,
    # the stage furthest along wins.
    staged = [c["stage"] for c in checks if c["group"] == "builds" and c["state"] == ACTIVE and c.get("stage")]
    if staged:
        return "stage-" + max(staged, key=STAGE_ORDER.index)
    if "builds" in running:
        return "building"
    if "wigle" in running:
        return "uploading"
    if WARN in states:
        return "warn"
    if UNKNOWN in states or not checks:
        return "unknown"
    return "calm"


# The daemon bit-bangs the LEDs from Python: a frame costs about 90 ms of CPU
# and 220 ms of wall time, whatever its fps says, so 30 fps is really about 4
# and a third of a core (brick-cicd-config#96). Off and the slow calm pulse
# don't need that; everything else keeps the daemon's default.
DEFAULT_FPS = 30
PATTERN_FPS = {"off": 1, "calm": 2}


class Plasma:
    def __init__(self, fifo: str, pattern_dir: str, prefix: str):
        self.fifo = fifo
        self.pattern_dir = pattern_dir
        self.prefix = prefix
        self.current = None
        self.fps = None

    def install_patterns(self) -> None:
        os.makedirs(self.pattern_dir, exist_ok=True)
        for name, make in {**patterns.IDLE, **patterns.BUILD}.items():
            self._write(name, make())

    def show(self, name: str, frames: list | None = None) -> None:
        """Switch to a pattern, writing its PNG first if frames are given."""
        if frames is not None:
            self._write(name, frames)
        elif name == self.current:
            return
        self._set_fps(PATTERN_FPS.get(name, DEFAULT_FPS))
        # Only remember what was actually sent, so a missed command is retried.
        self.current = name if self._send(f"{self.prefix}/{name}") else None

    def off(self) -> None:
        if self.current != "off":
            self._set_fps(PATTERN_FPS["off"])
            self.current = "off" if self._send("0 0 0") else None

    def _set_fps(self, fps: int) -> None:
        if fps != self.fps:
            self.fps = fps if self._send(f"fps {fps}") else None

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
