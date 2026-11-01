"""What every sender wants on the lights, and which of them wins.

A sender holds one signal under its own name until it expires, is replaced or
is deleted. The highest priority shows; among equals, the newest. Every signal
has a time limit, so a sender that dies can't leave the lights stuck.
"""

import threading
from dataclasses import dataclass

DEFAULT_PRIORITY = 50
DEFAULT_TTL = 30.0
MAX_TTL = 3600.0
MAX_SIGNALS = 64


class Full(Exception):
    """Too many senders at once."""


@dataclass
class Signal:
    source: str
    label: str  # a pattern's name, or "custom"
    frames: tuple
    priority: int
    expires: float
    order: int


class Signals:
    def __init__(self, on_change=lambda: None):
        self._signals: dict[str, Signal] = {}
        self._lock = threading.Lock()
        self._order = 0
        self._on_change = on_change

    def put(self, source: str, label: str, frames: tuple, priority: int, ttl: float, now: float) -> None:
        expires = now + ttl
        with self._lock:
            self._expire(now)
            held = self._signals.get(source)
            if held and held.frames is frames and held.priority == priority:
                held.expires = expires  # a heartbeat: not news, so it can't jump the queue
                return
            if not held and len(self._signals) >= MAX_SIGNALS:
                raise Full
            self._order += 1
            self._signals[source] = Signal(source, label, frames, priority, expires, self._order)
        self._on_change()

    def remove(self, source: str) -> bool:
        with self._lock:
            found = self._signals.pop(source, None) is not None
        if found:
            self._on_change()
        return found

    def winner(self, now: float) -> Signal | None:
        with self._lock:
            self._expire(now)
            return max(self._signals.values(), key=lambda s: (s.priority, s.order), default=None)

    def active(self, now: float) -> list[Signal]:
        """Every live signal, the one showing first."""
        with self._lock:
            self._expire(now)
            return sorted(self._signals.values(), key=lambda s: (s.priority, s.order), reverse=True)

    def _expire(self, now: float) -> None:
        for source in [s for s, v in self._signals.items() if v.expires <= now]:
            del self._signals[source]
