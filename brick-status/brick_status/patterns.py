"""Plasma LED patterns, written as the PNGs the plasma daemon animates.

Each PNG is 40 pixels wide (10 button slots x 4 LEDs) and one row per frame;
the daemon plays rows at 60 frames per second and loops.
"""

import colorsys
import math
import struct
import zlib

SLOTS = 10
LEDS_PER_SLOT = 4
WIDTH = SLOTS * LEDS_PER_SLOT
FPS = 60

GREEN = (0, 255, 0)
AMBER = (255, 140, 0)
RED = (255, 0, 0)
GREY = (90, 90, 110)
OFF = (0, 0, 0)

STATE_COLOURS = {"ok": GREEN, "warn": AMBER, "fail": RED, "active": GREEN, "unknown": GREY}

Rgb = tuple[int, int, int]
Frame = list[Rgb]


def _scale(c: Rgb, k: float) -> Rgb:
    return tuple(max(0, min(255, round(v * k))) for v in c)


def _slots(colours: list[Rgb]) -> Frame:
    """Expand one colour per slot into a full row of LEDs."""
    return [colours[i // LEDS_PER_SLOT] for i in range(WIDTH)]


def _breathe(t: float, low: float, high: float) -> float:
    return low + (high - low) * (0.5 - 0.5 * math.cos(2 * math.pi * t))


def pulse(colour: Rgb, seconds: float, low: float, high: float = 1.0) -> list[Frame]:
    n = int(seconds * FPS)
    return [_slots([_scale(colour, _breathe(f / n, low, high))] * SLOTS) for f in range(n)]


def chase(colour: Rgb, seconds: float, tail: int = 3, base: float = 0.05) -> list[Frame]:
    """A bright spot moving across the slots with a fading tail."""
    n = int(seconds * FPS)
    frames = []
    for f in range(n):
        head = f / n * SLOTS
        row = []
        for s in range(SLOTS):
            behind = (head - s) % SLOTS
            k = max(base, 1 - behind / tail) if behind < tail else base
            row.append(_scale(colour, k))
        frames.append(_slots(row))
    return frames


def buttons(slot_colours: dict[int, Rgb], selected: int | None, busy: set[int] = frozenset(),
            seconds: float = 2.0) -> list[Frame]:
    """Active mode: each view button lit in its colour, the selected one pulsing.

    Busy slots (a build or upload in progress) pulse fully whether selected or not.
    """
    n = int(seconds * FPS)
    frames = []
    for f in range(n):
        row = [OFF] * SLOTS
        for slot, colour in slot_colours.items():
            if slot in busy:
                k = _breathe(f / n, 0.1, 1.0)
            else:
                k = _breathe(f / n, 0.5, 1.0) if slot == selected else 0.2
            row[slot] = _scale(colour, k)
        frames.append(_slots(row))
    return frames


def rainbow(seconds: float) -> list[Frame]:
    """Every hue at once across the slots, rotating along them."""
    n = int(seconds * FPS)
    frames = []
    for f in range(n):
        row = []
        for s in range(SLOTS):
            r, g, b = colorsys.hsv_to_rgb((s / SLOTS + f / n) % 1.0, 1.0, 1.0)
            row.append((round(r * 255), round(g * 255), round(b * 255)))
        frames.append(_slots(row))
    return frames


def flash(colour: Rgb, seconds: float) -> list[Frame]:
    """Hard on/off blink: on for the first half of each period, off for the second."""
    n = int(seconds * FPS)
    return [_slots([colour if f < n // 2 else OFF] * SLOTS) for f in range(n)]


# Idle patterns, one per board event, highest priority first.
IDLE = {
    "alert": lambda: pulse(RED, 1.0, 0.15),
    "building": lambda: chase(AMBER, 1.5),
    "uploading": lambda: pulse(GREEN, 2.0, 0.1),
    "warn": lambda: pulse(AMBER, 3.0, 0.05, 0.6),
    "unknown": lambda: pulse(GREY, 4.0, 0.05, 0.4),
    "calm": lambda: pulse(GREEN, 6.0, 0.03),
}

# Image builds on brick9000 (see build.py). These take over the lights in
# idle and active mode alike.
BUILD = {
    "image-building": lambda: rainbow(1.5),
    "image-pushed": lambda: flash(GREEN, 0.5),
    "image-failed": lambda: pulse(RED, 0.8, 0.0),
}


def png(frames: list[Frame]) -> bytes:
    """Encode frames as a 24-bit RGB PNG, one row per frame."""
    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in frames)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", len(frames[0]), len(frames), 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
