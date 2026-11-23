"""The cabinet's left side button (KEY_WAKE): a press or a hold.

- Board off: a key-down wakes it at once (Board.side_button); that press's
  hold and release do nothing more.
- Board on: a press puts it to sleep, on release; held `long_seconds` it stays
  on for `long_wake_seconds` instead (Board.stay_awake), fired while still
  held (the user, 2026-11-23: 12 hours instead of the 30-minute wake).

Gestures come from lamp.Gestures with no double press, so a release counts
on the next tick. Thread-safe: key events from the input thread, tick() from
run()'s.
"""

import threading
import time
from collections.abc import Callable

import structlog

from .board import Board
from .lamp import LONG, SINGLE, Gestures

log = structlog.get_logger(__name__)


class SideButton:
    def __init__(self, board: Board, long_wake_seconds: float, long_seconds: float = 0.8,
                 changed: Callable[[], None] = lambda: None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        self.board = board
        self.long_wake_seconds = long_wake_seconds
        self.gestures = Gestures(double_seconds=0.0, long_seconds=long_seconds)
        self.changed = changed
        self.clock = clock
        self.wall = wall
        self._waking = False  # this press woke the board: its release does nothing
        self._lock = threading.Lock()

    def press(self) -> None:
        now = self.wall()
        with self._lock:
            if self.board.asleep(now):
                self._waking = True
                did = self.board.side_button(now)
            else:
                self._waking = False
                self.gestures.press(self.clock())
                return
        if did == "awake":
            log.info("side button", did="awake", minutes=self.board.wake_seconds // 60)
        else:
            log.info("side button", did="on until the schedule turns it off")
        self.changed()

    def release(self) -> None:
        with self._lock:
            if self._waking:
                self._waking = False
                return
            self.gestures.release(self.clock())

    def tick(self) -> None:
        with self._lock:
            gesture = self.gestures.poll(self.clock())
            if gesture is None:
                return
            now = self.wall()
            if gesture == LONG:
                until = self.board.stay_awake(now, self.long_wake_seconds)
                log.info("side button", did="stay awake",
                         until=time.strftime("%a %H:%M", time.localtime(until)))
            elif gesture == SINGLE:
                did = self.board.side_button(now)
                until = self.board.asleep_until(now)
                log.info("side button", did=did,
                         until=time.strftime("%a %H:%M", time.localtime(until)) if until else None)
        self.changed()

    def run(self, every: float = 0.02) -> None:
        while True:
            time.sleep(every)
            self.tick()
