"""brick-arena: the display daemon on brick1982's 7" touchscreen.

Polls Prometheus, serves the page Chromium shows in cage, and dims the panel
overnight. The page rotates through three views: arena (the cluster's pods by
node, with moves animated), weather (the network as weather) and radar
(wardriving contacts around home). A tap moves to the next view.

    python3 -m brick_arena
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import collect
from .backlight import Backlight, in_window, parse_window

log = logging.getLogger("brick-arena")
STATIC = (Path(__file__).parent / "static").resolve()
# The page reloads itself when this changes, so a deploy shows the new page.
BOOT = str(time.time())


def env(name: str, default: str) -> str:
    return os.environ.get(f"BRICK_ARENA_{name}", default)


def home_from(text: str) -> tuple[float, float] | None:
    """"lat,lon" of home, the radar's centre. Kept in the env file only."""
    try:
        lat, lon = (float(v) for v in text.split(","))
        return lat, lon
    except ValueError:
        return None


class State:
    def __init__(self):
        self._lock = threading.Lock()
        self._snap: dict = {}
        self.updated = 0.0
        self.error = ""

    def set(self, snap: dict) -> None:
        with self._lock:
            self._snap, self.updated, self.error = snap, time.time(), ""

    def fail(self, error: str) -> None:
        with self._lock:
            self.error = error

    def get(self) -> dict:
        with self._lock:
            return {**self._snap, "updated": self.updated, "error": self.error}


def poll(state: State) -> None:
    prom = collect.Prometheus(env("PROMETHEUS_URL", "https://prometheus.brick.nozdormu.cloud"))
    tracker = collect.Tracker()
    radar_file = Path(os.path.expanduser(env("RADAR_FILE", "~/.local/share/brick-arena/radar.json")))
    home = home_from(env("HOME_LATLON", ""))
    seconds = float(env("POLL_SECONDS", "10"))
    dim_window = parse_window(env("DIM", "23:00-07:00"))
    dim_level, day_level = float(env("DIM_LEVEL", "0.08")), float(env("DAY_LEVEL", "1.0"))
    light = Backlight()
    while True:
        try:
            state.set(collect.snapshot(prom, tracker, radar_file, home))
        except Exception as e:  # network errors, bad responses: keep the last picture
            log.warning("poll failed: %s", e)
            state.fail(str(e))
        light.set_fraction(dim_level if in_window(dim_window, datetime.now()) else day_level)
        time.sleep(seconds)


def serve(state: State, host: str, port: int, rotate: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/state":
                body = json.dumps({"boot": BOOT, "rotate": rotate, "now": time.time(), **state.get()})
                self._send(200, "application/json", body.encode())
            elif self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", (STATIC / "index.html").read_bytes())
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    ThreadingHTTPServer((host, port), Handler).serve_forever()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    state = State()
    threading.Thread(target=poll, args=(state,), daemon=True).start()
    # Every address, not just localhost: brick9000's proxy shows the same page
    # at arena.brick.nozdormu.cloud. It only answers GETs and holds no secrets.
    serve(state, env("HTTP_HOST", "0.0.0.0"), int(env("HTTP_PORT", "8766")),
          int(env("ROTATE_SECONDS", "45")))


if __name__ == "__main__":
    main()
