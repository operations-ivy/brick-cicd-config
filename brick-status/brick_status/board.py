"""Board mode: which view is shown, and whether the board is idle, active or quiet.

- idle: nobody has pressed a button lately. The screen shows the overview
  and the lights show the most important current event.
- active: a button was pressed in the last `active_seconds`. The screen shows
  the chosen view and the lights show which button is which view.
- quiet: inside the quiet schedule (see QuietSchedule). Screen and lights are
  off and the cabinet keys do nothing, except the side button: it wakes the
  board for `wake_seconds`, and does nothing while the board is already on.
"""

import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, time

IDLE, ACTIVE, QUIET = "idle", "active", "quiet"


DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEK = 7 * 24 * 60


def in_quiet_hours(now: time, start: time, end: time) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end  # wraps past midnight


@dataclass(frozen=True)
class Window:
    """Quiet from start to end: every day (no weekday), or once a week."""
    start: time
    end: time
    start_day: int | None = None  # 0 = Monday
    end_day: int | None = None

    def contains(self, dt: datetime) -> bool:
        if self.start_day is None:
            return in_quiet_hours(dt.time(), self.start, self.end)
        minute = lambda day, t: day * 24 * 60 + t.hour * 60 + t.minute
        now, start, end = minute(dt.weekday(), dt.time()), minute(self.start_day, self.start), \
            minute(self.end_day, self.end)
        if start == end:
            return False
        if start < end:
            return start <= now < end
        return now >= start or now < end  # wraps past the end of the week


def _point(text: str) -> tuple[int | None, time]:
    m = re.fullmatch(r"(?:([A-Za-z]{3})\s+)?(\d{1,2}):(\d{2})", text.strip())
    if not m:
        raise ValueError(f"bad time {text!r}: expected HH:MM or Day HH:MM (Mon..Sun)")
    day = m.group(1)
    if day is not None and day.capitalize() not in DAYS:
        raise ValueError(f"bad day {day!r}: expected one of {', '.join(DAYS)}")
    return (None if day is None else DAYS.index(day.capitalize())), time(int(m.group(2)), int(m.group(3)))


def parse_window(text: str) -> Window | None:
    """'00:00-06:00' (daily), 'Mon 00:00-Fri 16:00' (weekly) or 'none'."""
    if text.strip().lower() == "none":
        return None
    start, _, end = text.partition("-")
    (sd, st), (ed, et) = _point(start), _point(end)
    if (sd is None) != (ed is None):
        raise ValueError(f"bad window {text!r}: give a day on both ends or neither")
    return Window(st, et, sd, ed)


class QuietSchedule:
    """When the board is quiet: windows, optionally changing on given dates.

    Entries are separated by ';', each optionally prefixed with the date it
    takes effect from (at midnight); the latest one that has started applies.
    An entry is one or more windows separated by ',', quiet inside any of them:
        00:00-06:00; 2026-10-09: Mon 00:01-Mon 16:00, Tue 00:00-Tue 16:00
    """

    def __init__(self, entries: list[tuple[date | None, tuple[Window, ...]]]):
        self.entries = sorted(entries, key=lambda e: e[0] or date.min)

    @classmethod
    def parse(cls, text: str) -> "QuietSchedule":
        entries = []
        for part in filter(None, (p.strip() for p in text.split(";"))):
            m = re.fullmatch(r"(\d{4}-\d{2}-\d{2}):\s*(.+)", part)
            when = date.fromisoformat(m.group(1)) if m else None
            windows = (parse_window(w) for w in (m.group(2) if m else part).split(","))
            entries.append((when, tuple(w for w in windows if w is not None)))
        return cls(entries)

    def windows(self, day: date) -> tuple[Window, ...]:
        current: tuple[Window, ...] = ()
        for when, windows in self.entries:
            if when is None or when <= day:
                current = windows
        return current

    def is_quiet(self, dt: datetime) -> bool:
        return any(w.contains(dt) for w in self.windows(dt.date()))


class Board:
    def __init__(self, views: list[str], active_seconds: float, quiet: QuietSchedule,
                 wake_seconds: float = 1800.0):
        self.views = views
        self.active_seconds = active_seconds
        self.quiet = quiet
        self.wake_seconds = wake_seconds
        self._view = 0
        self._last_press: float | None = None
        self._awake_until: float | None = None
        self._lock = threading.Lock()

    def _asleep(self, now: float) -> bool:
        """In the quiet schedule and not woken by the side button (lock held)."""
        if self._awake_until is not None and now < self._awake_until:
            return False
        return self.quiet.is_quiet(datetime.fromtimestamp(now))

    def wake_button(self, now: float) -> bool:
        """The side button: while asleep, turn everything on for wake_seconds.
        Does nothing while the board is already on. True if it woke the board."""
        with self._lock:
            if not self._asleep(now):
                return False
            self._awake_until = now + self.wake_seconds
            return True

    def wake(self, now: float) -> None:
        """A key that isn't a view button: stay on the current view, but count as a press."""
        with self._lock:
            if not self._asleep(now):
                self._last_press = now

    def press_view(self, index: int, now: float) -> None:
        with self._lock:
            if self._asleep(now):
                return  # keys do nothing while it's off; only the side button wakes it
            if 0 <= index < len(self.views):
                self._view = index
            self._last_press = now

    def step_view(self, delta: int, now: float) -> None:
        with self._lock:
            if self._asleep(now):
                return
            self._view = (self._view + delta) % len(self.views)
            self._last_press = now

    def awake_until(self, now: float) -> float | None:
        """When a side-button wake ends, while one is keeping the board on."""
        with self._lock:
            if self._awake_until is not None and now < self._awake_until \
                    and self.quiet.is_quiet(datetime.fromtimestamp(now)):
                return self._awake_until
            return None

    def mode(self, now: float) -> str:
        with self._lock:
            pressed = self._last_press is not None and now - self._last_press < self.active_seconds
            asleep = self._asleep(now)
        if pressed:
            return ACTIVE
        if asleep:
            return QUIET
        return IDLE

    def view(self, now: float) -> int:
        """The view on screen; idle always shows the overview."""
        if self.mode(now) != ACTIVE:
            return 0
        with self._lock:
            return self._view
