"""Board mode: which view is shown, and whether the board is idle, active or quiet.

- idle: nobody has pressed a button lately. The screen shows the overview
  and the lights show the most important current event.
- active: a button was pressed in the last `active_seconds`. The screen shows
  the chosen view and the lights show which button is which view.
- quiet: inside the quiet schedule (see QuietSchedule), or put to sleep by the
  side button. Screen and lights are off and the cabinet keys do nothing,
  except the side button.

The side button toggles the board:
- on: sleep until the schedule next turns it on (the end of the next quiet
  window), or the next `morning`, whichever is first. The morning cap keeps a
  Friday-evening sleep from lasting all weekend, when nothing is quiet.
- off, put to sleep during the schedule's on hours: on again, until the
  schedule turns it off.
- off otherwise (quiet hours): on for `wake_seconds`.
A sleep is saved to `state_path`, so a restart (a deploy) doesn't end it.
"""

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

IDLE, ACTIVE, QUIET = "idle", "active", "quiet"

log = logging.getLogger(__name__)


DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEK = 7 * 24 * 60


def next_clock(now: float, at: time) -> float:
    """The next time the local clock reads `at`, after `now` (a timestamp)."""
    current = datetime.fromtimestamp(now)
    target = datetime.combine(current.date(), at)
    if target <= current:
        target = datetime.combine(current.date() + timedelta(days=1), at)
    return target.timestamp()


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
                 wake_seconds: float = 1800.0, morning: time = time(8, 0),
                 state_path: str | None = None):
        self.views = views
        self.active_seconds = active_seconds
        self.quiet = quiet
        self.wake_seconds = wake_seconds
        self.morning = morning
        self.state_path = state_path
        self._view = 0
        self._last_press: float | None = None
        self._awake_until: float | None = None
        self._asleep_until: float | None = self._load_sleep()
        self._lock = threading.Lock()

    def _load_sleep(self) -> float | None:
        if not self.state_path:
            return None
        try:
            with open(self.state_path) as f:
                value = json.load(f).get("asleep_until")
        except FileNotFoundError:
            return None
        except (OSError, ValueError, AttributeError) as e:
            log.warning("board state %s unreadable, ignored: %s", self.state_path, e)
            return None
        return float(value) if isinstance(value, (int, float)) else None

    def _save_sleep(self) -> None:
        """Best effort: a lost sleep only means the board comes back on."""
        if not self.state_path:
            return
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"asleep_until": self._asleep_until}, f)
            os.replace(tmp, self.state_path)
        except OSError as e:
            log.warning("couldn't save board state to %s: %s", self.state_path, e)

    def _quiet_at(self, t: float) -> bool:
        return self.quiet.is_quiet(datetime.fromtimestamp(t))

    def next_on(self, now: float) -> float:
        """When the schedule next turns the board on after a quiet window that
        starts from `now`, capped at the next `morning`."""
        cap = next_clock(now, self.morning)
        t = now - now % 60 + 60  # minute steps: every window is in whole minutes
        while t < cap and not self._quiet_at(t):
            t += 60
        while t < cap and self._quiet_at(t):
            t += 60
        return min(t, cap)

    def _asleep(self, now: float) -> bool:
        """Put to sleep by the side button, or in the quiet schedule and not woken
        by it (lock held)."""
        if self._asleep_until is not None and now < self._asleep_until:
            return True
        if self._awake_until is not None and now < self._awake_until:
            return False
        return self._quiet_at(now)

    def side_button(self, now: float) -> str:
        """Toggle the board (see the module's docstring). Returns what it did:
        "asleep", "on" (a sleep ended in the on hours) or "awake" (on for
        wake_seconds in quiet hours)."""
        with self._lock:
            if self._asleep(now):
                slept = self._asleep_until is not None and now < self._asleep_until
                self._asleep_until = None
                self._save_sleep()
                if slept and not self._quiet_at(now):
                    return "on"
                self._awake_until = now + self.wake_seconds
                return "awake"
            self._asleep_until = self.next_on(now)
            self._save_sleep()
            self._awake_until = None
            self._last_press = None  # a recent press would keep it active
            return "asleep"

    def asleep_until(self, now: float) -> float | None:
        """When a side-button sleep ends, while one is keeping the board off."""
        with self._lock:
            if self._asleep_until is not None and now < self._asleep_until:
                return self._asleep_until
            return None

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

    def keep_awake(self, now: float, seconds: float) -> None:
        """Stay on for at least `seconds` more, whatever the schedule (a game is open)."""
        with self._lock:
            self._awake_until = max(self._awake_until or 0.0, now + seconds)

    def awake_until(self, now: float) -> float | None:
        """When a side-button wake ends, while one is keeping the board on."""
        with self._lock:
            if self._asleep_until is not None and now < self._asleep_until:
                return None  # put to sleep since
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
