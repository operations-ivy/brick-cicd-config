"""What brick-arena measures about itself, served at /metrics (README,
"Metrics"). Prometheus scrapes brick1982:8766 directly.
"""

import sys
import time
from pathlib import Path

# The metrics module is brick-status's, from the same checkout (like backlight.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brick-status"))
from brick_status.metrics import CONTENT_TYPE, Registry  # noqa: E402

__all__ = ["CONTENT_TYPE", "REGISTRY", "http_path"]

REGISTRY = Registry()
START = REGISTRY.gauge("brick_arena_start_time_seconds", "When brick-arena started.")
POLLS = REGISTRY.counter("brick_arena_polls_total", "Polls of Prometheus, by result.", ["result"])
POLL_SECONDS = REGISTRY.gauge("brick_arena_poll_duration_seconds", "How long the last poll took.")
POLLED_AT = REGISTRY.gauge("brick_arena_last_poll_timestamp_seconds", "When the last poll finished.")
EVENTS = REGISTRY.counter("brick_arena_events_total",
                          "Pod events the arena animates: move, restart, new.", ["kind"])
ASLEEP = REGISTRY.gauge("brick_arena_panel_asleep", "1 while the panel is off on the quiet schedule.")
BRIGHTNESS = REGISTRY.gauge("brick_arena_backlight_fraction", "The panel's backlight, 0 (off) to 1 (full).")
WAKES = REGISTRY.counter("brick_arena_wakes_total", "Taps on the panel that woke it or kept it awake.")
VIEW = REGISTRY.gauge("brick_arena_view", "1 for the view on the panel.", ["view"])
VIEW_CHANGES = REGISTRY.counter("brick_arena_view_changes_total", "Times the panel moved to each view.", ["view"])
BODY = REGISTRY.gauge("brick_arena_planet", "Which planet the main view shows (an index into the page's list).")
KIOSK_SEEN = REGISTRY.gauge("brick_arena_kiosk_last_seen_timestamp_seconds",
                            "When the panel's own browser last fetched the state (it polls every few seconds).")
HTTP = REGISTRY.counter("brick_arena_http_requests_total", "HTTP requests served.", ["path", "code"])

START.set(time.time())
for r in ("ok", "error"):
    POLLS.inc(0, result=r)
for k in ("move", "restart", "new"):
    EVENTS.inc(0, kind=k)
WAKES.inc(0)

HTTP_PATHS = {"/", "/index.html", "/api/state", "/api/screen", "/api/wake", "/metrics"}


def http_path(path: str) -> str:
    if path.startswith("/art/"):
        return "/art/"
    return path if path in HTTP_PATHS else "other"
