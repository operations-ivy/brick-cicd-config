"""The party button's sound (party.py): twenty seconds of chiptune, made here.

Synthesized at start, so there's no audio file to license or fetch, and played
through PipeWire's pw-play at the speaker's own volume (the dial sets it).

The show's length is played as ten-second rounds, each a whole step higher
than the last, and only the last ends on the chord (with the lights' white
flash). A round:

  0-2 s     a square-wave arpeggio climbing
  2-4 s     bubbles: short blips at random pitches
  4-5.5 s   a two-tone siren
  5.5-8.5 s fireworks: whistles rising, then a pop of noise
  8.5-10 s  a fanfare up the scale (and the closing chord, last round only)

Seeded, so it's the same every time.
"""

import array
import math
import random
import subprocess
import threading
import wave
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

RATE = 22050
SECONDS = 20.0  # party.SECONDS
ROUND = 10.0
PEAK = 0.45  # of full scale: loud enough on the cabinet's speaker, never clipping

# C major, two octaves from middle C.
SCALE = [261.63, 293.66, 329.63, 349.23, 392.0, 440.0, 493.88, 523.25, 587.33, 659.25, 698.46, 783.99, 880.0, 987.77]


def _square(freq: float, t: float) -> float:
    return 1.0 if (t * freq) % 1.0 < 0.5 else -1.0


def _env(i: int, length: int, attack: int = 60, release: int = 300) -> float:
    """A note's loudness at sample i: quick in, quick out, so notes don't click."""
    return min(1.0, i / attack, (length - i) / release) if length > 0 else 0.0


def synth(seconds: float = SECONDS, rate: int = RATE, seed: int = 2017) -> list[float]:
    """The show's sound as samples from -1 to 1."""
    n = int(seconds * rate)
    out = [0.0] * n
    rounds = max(1, round(seconds / ROUND))
    for r in range(rounds):
        _round(out, rate, r * ROUND, 2 ** (2 * r / 12), random.Random(seed + r), last=r == rounds - 1,
               end=seconds)
    top = max(1e-9, max(abs(v) for v in out))
    return [v / top * PEAK for v in out]


def _round(out: list[float], rate: int, at: float, up: float, rng: random.Random, last: bool,
           end: float) -> None:
    """One ten-second round into `out`, from `at` seconds, every pitch times `up`."""
    n = len(out)

    def note(start: float, length: float, freq: float, shape: str = "square", loud: float = 0.6) -> None:
        a, count = int((at + start) * rate), int(length * rate)
        for i in range(count):
            if a + i >= n:
                break
            t = i / rate
            wave_ = _square(freq * up, t) if shape == "square" else math.sin(2 * math.pi * freq * up * t)
            out[a + i] += loud * _env(i, count) * wave_

    # 0-2 s: the arpeggio, sixteenth notes climbing and climbing again.
    for k in range(16):
        note(k * 0.125, 0.11, SCALE[(k * 2) % len(SCALE)] * (1 if k < 8 else 2))
    # 2-4 s: bubbles.
    for k in range(20):
        note(2.0 + k * 0.1, 0.07, rng.uniform(400, 1400), "sine", 0.7)
    # 4-5.5 s: the siren.
    for k in range(6):
        note(4.0 + k * 0.25, 0.24, 988 if k % 2 == 0 else 740, "square", 0.45)
    # 5.5-8.5 s: fireworks, a whistle up then a pop.
    for k in range(5):
        whistle = int(0.35 * rate)
        a = int((at + 5.5 + k * 0.6) * rate)
        phase = 0.0
        for i in range(whistle):
            if a + i >= n:
                break
            freq = (600 + 1800 * i / whistle) * up
            phase += freq / rate
            out[a + i] += 0.35 * _env(i, whistle) * math.sin(2 * math.pi * phase)
        pop = int(0.25 * rate)
        b = a + whistle
        for i in range(pop):
            if b + i >= n:
                break
            out[b + i] += 0.8 * (1 - i / pop) ** 3 * rng.uniform(-1, 1)
    # 8.5-10 s: the fanfare up the scale; the last round ends on a C major chord.
    for k, degree in enumerate([0, 2, 4, 7, 9, 11]):
        note(8.5 + k * 0.1, 0.09, SCALE[degree])
    if last:
        for freq in (SCALE[7], SCALE[9], SCALE[11]):
            note(9.15, end - at - 9.15, freq, "square", 0.3)


def write_wav(path: Path, samples: list[float], rate: int = RATE) -> None:
    data = array.array("h", (round(v * 32767) for v in samples))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())


class Player:
    """Makes the sound once (in the background, at start) and plays it on
    each show. A press during a show starts the sound again, as it does the
    show."""

    def __init__(self, path: Path, command: str = "pw-play"):
        self.path, self.command = path, command
        self._ready = threading.Event()
        self._playing: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def prepare(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_wav(self.path, synth())
            self._ready.set()
        except OSError as e:
            log.warning("sound not made; the show stays silent", error=str(e))

    def play(self) -> None:
        if not self._ready.is_set():
            log.info("sound not made yet; silent this time")
            return
        with self._lock:
            if self._playing is not None and self._playing.poll() is None:
                self._playing.terminate()
                try:
                    self._playing.wait(timeout=1)  # reap it, no zombie
                except subprocess.TimeoutExpired:
                    self._playing.kill()
            try:
                self._playing = subprocess.Popen([self.command, str(self.path)],
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                log.warning("sound not played", error=str(e))
