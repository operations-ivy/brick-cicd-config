"""Dim the panel overnight through /sys/class/backlight (writable by the video
group once brick1982/90-backlight.rules is installed)."""

import logging
from datetime import datetime, time as dtime
from pathlib import Path

log = logging.getLogger(__name__)


def parse_window(text: str) -> tuple[dtime, dtime] | None:
    """"HH:MM-HH:MM" (may wrap past midnight), or empty for never."""
    text = text.strip()
    if not text:
        return None
    start, end = (dtime.fromisoformat(t.strip()) for t in text.split("-"))
    return start, end


def in_window(window: tuple[dtime, dtime] | None, at: datetime) -> bool:
    if window is None:
        return False
    start, end = window
    t = at.time()
    return start <= t < end if start <= end else t >= start or t < end


class Backlight:
    def __init__(self, root: str = "/sys/class/backlight"):
        devices = sorted(Path(root).glob("*/brightness"))
        self.path = devices[0] if devices else None
        self.max = int((self.path.parent / "max_brightness").read_text()) if self.path else 0
        self.current: int | None = None

    def set_fraction(self, fraction: float) -> None:
        if self.path is None:
            return
        level = max(1, round(self.max * fraction))
        if level == self.current:
            return
        try:
            self.path.write_text(str(level))
            self.current = level
        except OSError as e:
            log.warning("backlight %s not writable: %s", self.path, e)
