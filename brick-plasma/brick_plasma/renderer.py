"""Plays the winning signal on the LEDs, at once when it changes."""

import threading
import time

from . import patterns

FRAME_SECONDS = 1 / patterns.FPS
# A still pattern needs no frames, but expiries are checked and a corrupted
# LED is put right this often.
STILL_SECONDS = 0.25


class Renderer:
    def __init__(self, signals, chain, dark: tuple, stats, clock=time.monotonic):
        self.signals, self.chain, self.dark, self.stats, self.clock = signals, chain, dark, stats, clock
        self.wake = threading.Event()
        self.frames, self.since, self.index = None, 0.0, -1
        self.refresh_at = 0.0

    def step(self) -> float:
        """Show what's due now; returns the seconds to wait for the next frame."""
        now = self.clock()
        winner = self.signals.winner(now)
        frames = winner.frames if winner else self.dark
        if frames is not self.frames:  # same frames from a new sender: don't restart them
            self.frames, self.since, self.index = frames, now, -1
            self.stats.changes += 1
        self.stats.showing = winner.label if winner else "off"
        index = int((now - self.since) * patterns.FPS) % len(frames)
        if index != self.index or now >= self.refresh_at:
            started = time.perf_counter()
            self.chain.write(frames[index])
            self.stats.frame_seconds = time.perf_counter() - started
            self.stats.frames += 1
            self.index, self.refresh_at = index, now + STILL_SECONDS
        return FRAME_SECONDS if len(frames) > 1 else STILL_SECONDS

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            wait = self.step()
            self.wake.wait(wait)  # a new signal cuts the wait short
            self.wake.clear()
