"""brick9000's speaker volume, for the volume page (volume.brick.nozdormu.cloud).

PipeWire's default sink is the Picade's DAC, which is very loud: the page's
0-100% maps onto 0 to max_volume (the same cap as brick-retro's Home + D-pad,
BRICK_RETRO_MAX_VOLUME), and wpctl enforces it too (-l). WirePlumber
remembers the level across reboots.
"""

import re
import subprocess

SINK = "@DEFAULT_AUDIO_SINK@"
_VOLUME = re.compile(r"^Volume:\s*([0-9.]+)(\s*\[MUTED\])?\s*$")


def parse(out: str) -> tuple[float, bool]:
    """wpctl get-volume's "Volume: 0.25" or "Volume: 0.25 [MUTED]"."""
    m = _VOLUME.match(out.strip())
    if not m:
        raise ValueError(f"unexpected wpctl output: {out.strip()[:80]!r}")
    return float(m.group(1)), bool(m.group(2))


def get(wpctl: str, max_volume: float) -> dict:
    out = subprocess.run([wpctl, "get-volume", SINK], capture_output=True, text=True, timeout=5, check=True).stdout
    raw, muted = parse(out)
    return {"level": round(min(1.0, raw / max_volume), 3), "muted": muted, "max": max_volume}


def set_level(wpctl: str, max_volume: float, level: float) -> None:
    """level: 0-1 of the cap."""
    level = min(1.0, max(0.0, level))
    subprocess.run([wpctl, "set-volume", "-l", f"{max_volume}", SINK, f"{level * max_volume:.3f}"],
                   capture_output=True, timeout=5, check=True)


def set_muted(wpctl: str, muted: bool) -> None:
    subprocess.run([wpctl, "set-mute", SINK, "1" if muted else "0"], capture_output=True, timeout=5, check=True)
