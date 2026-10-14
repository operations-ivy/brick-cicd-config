"""What brick-retro measures, written for node-exporter's textfile collector
on brick9000 (brick-retro has no web server). Rewritten only when a value
changes, so the SD card isn't written every second.
"""

import logging
import os
import sys
from pathlib import Path

# The metrics module is brick-status's, from the same checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brick-status"))
from brick_status.metrics import Registry, write_textfile  # noqa: E402

log = logging.getLogger(__name__)
TEXTFILE_DIR = os.environ.get("BRICK_TEXTFILE_DIR", "/var/lib/node-exporter-textfile")

REGISTRY = Registry()
RUNNING = REGISTRY.gauge("brick_retro_retroarch_running", "1 while RetroArch is open over the board.")
PADS = REGISTRY.gauge("brick_retro_pads_connected", "Game controllers connected now.")
LAUNCHES = REGISTRY.counter("brick_retro_launches_total", "Times RetroArch was opened for a controller.")
CLOSES = REGISTRY.counter("brick_retro_closes_total",
                          "Times brick-retro closed RetroArch: idle (no controller) or home (Home held).",
                          ["reason"])
CONNECTS = REGISTRY.counter("brick_retro_pad_connects_total", "Controllers that connected.")
for reason in ("idle", "home"):
    CLOSES.inc(0, reason=reason)
LAUNCHES.inc(0)
CONNECTS.inc(0)

_written = ""


def flush(path: str = os.path.join(TEXTFILE_DIR, "brick-retro.prom")) -> None:
    """Write the textfile if anything changed since the last write."""
    global _written
    text = REGISTRY.render()
    if text == _written or not os.path.isdir(os.path.dirname(path)):
        return
    try:
        write_textfile(path, text)
        _written = text
    except OSError as e:
        log.warning("metrics not written to %s: %s", path, e)


def games_text(counts: dict[str, int]) -> str:
    r = Registry()
    g = r.gauge("brick_retro_games", "Games in each system's playlist.", ["system"])
    for system, n in counts.items():
        g.set(n, system=system)
    return r.render()


def write_games(counts: dict[str, int], path: str = os.path.join(TEXTFILE_DIR, "brick-retro-games.prom")) -> None:
    if os.path.isdir(os.path.dirname(path)):
        write_textfile(path, games_text(counts))
