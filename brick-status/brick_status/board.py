"""Board mode: which view is shown, and whether the board is idle, active or quiet.

- idle: nobody has pressed a button lately. The screen shows the overview
  and the lights show the most important current event.
- active: a button was pressed in the last `active_seconds`. The screen shows
  the chosen view and the lights show which button is which view.
- quiet: overnight. Screen and lights are off; any cabinet key wakes the
  board into active mode until it times out again.
"""

import threading
from datetime import datetime, time

IDLE, ACTIVE, QUIET = "idle", "active", "quiet"


def in_quiet_hours(now: time, start: time, end: time) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end  # wraps past midnight


class Board:
    def __init__(self, views: list[str], active_seconds: float, quiet_start: time, quiet_end: time):
        self.views = views
        self.active_seconds = active_seconds
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end
        self._view = 0
        self._last_press: float | None = None
        self._lock = threading.Lock()

    def wake(self, now: float) -> None:
        """A key that isn't a view button: stay on the current view, but wake up."""
        with self._lock:
            self._last_press = now

    def press_view(self, index: int, now: float) -> None:
        with self._lock:
            if 0 <= index < len(self.views):
                self._view = index
            self._last_press = now

    def step_view(self, delta: int, now: float) -> None:
        with self._lock:
            self._view = (self._view + delta) % len(self.views)
            self._last_press = now

    def mode(self, now: float) -> str:
        with self._lock:
            pressed = self._last_press is not None and now - self._last_press < self.active_seconds
        if pressed:
            return ACTIVE
        if in_quiet_hours(datetime.fromtimestamp(now).time(), self.quiet_start, self.quiet_end):
            return QUIET
        return IDLE

    def view(self, now: float) -> int:
        """The view on screen; idle always shows the overview."""
        if self.mode(now) != ACTIVE:
            return 0
        with self._lock:
            return self._view
