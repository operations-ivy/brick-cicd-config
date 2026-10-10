"""brick-arena: the display daemon on brick1982's 7" touchscreen.

Polls Prometheus, serves the page Chromium shows in cage, and dims the panel
overnight. The page rotates through its views, coming back to main (a turning
pixel-art planet) between arena (the cluster's pods by node, with moves
animated) and weather (the network as weather). A tap moves to the next view.
The wardrive radar moved to wigle-console (radar.brick.nozdormu.cloud).

    python3 -m brick_arena
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import collect
from .backlight import Backlight, in_window, parse_window

log = logging.getLogger("brick-arena")
STATIC = (Path(__file__).parent / "static").resolve()
VIEWS = ("main", "arena", "weather")
ART = STATIC / "art"
# The page reloads itself when this changes, so a deploy shows the new page.
BOOT = str(time.time())


def env(name: str, default: str) -> str:
    return os.environ.get(f"BRICK_ARENA_{name}", default)


class State:
    def __init__(self):
        self._lock = threading.Lock()
        self._snap: dict = {}
        self.updated = 0.0
        self.error = ""
        # What the panel shows: reported by the kiosk's own browser, followed
        # by every other browser (arena.brick.nozdormu.cloud).
        self.screen = {"view": VIEWS[0], "body": 0, "at": 0.0}

    def set(self, snap: dict) -> None:
        with self._lock:
            self._snap, self.updated, self.error = snap, time.time(), ""

    def fail(self, error: str) -> None:
        with self._lock:
            self.error = error

    def get(self) -> dict:
        with self._lock:
            return {**self._snap, "updated": self.updated, "error": self.error, "screen": dict(self.screen)}

    def show(self, view: str, body: int = 0) -> bool:
        """The kiosk's view, and which planet the main view has up (an index
        into the page's list of bodies)."""
        if view not in VIEWS or type(body) is not int or not 0 <= body < 100:
            return False
        with self._lock:
            if (view, body) != (self.screen["view"], self.screen["body"]):
                self.screen = {"view": view, "body": body, "at": time.time()}
        return True


def art_file(url_path: str) -> Path | None:
    """The file under static/art/ that /art/<name> names, or None. Only plain
    PNG names, so nothing outside that directory can be served."""
    name = url_path.removeprefix("/art/")
    if not re.fullmatch(r"[a-z0-9-]+\.png", name):
        return None
    path = ART / name
    return path if path.is_file() else None


def is_kiosk(client_host: str, headers) -> bool:
    """The panel's own Chromium: on the same host, not through a proxy.
    Everyone else only watches."""
    return client_host in ("127.0.0.1", "::1") and not headers.get("X-Forwarded-For")


def poll(state: State) -> None:
    prom = collect.Prometheus(env("PROMETHEUS_URL", "https://prometheus.brick.nozdormu.cloud"))
    tracker = collect.Tracker()
    seconds = float(env("POLL_SECONDS", "10"))
    dim_window = parse_window(env("DIM", "23:00-07:00"))
    dim_level, day_level = float(env("DIM_LEVEL", "0.08")), float(env("DAY_LEVEL", "1.0"))
    light = Backlight()
    while True:
        try:
            state.set(collect.snapshot(prom, tracker))
        except Exception as e:  # network errors, bad responses: keep the last picture
            log.warning("poll failed: %s", e)
            state.fail(str(e))
        light.set_fraction(dim_level if in_window(dim_window, datetime.now()) else day_level)
        time.sleep(seconds)


def serve(state: State, host: str, port: int, rotate: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/state":
                body = json.dumps({"boot": BOOT, "rotate": rotate, "now": time.time(),
                                   "kiosk": is_kiosk(self.client_address[0], self.headers), **state.get()})
                self._send(200, "application/json", body.encode())
            elif self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", (STATIC / "index.html").read_bytes())
            elif self.path.startswith("/art/") and (art := art_file(self.path)):
                self._send(200, "image/png", art.read_bytes())
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):
            # The kiosk says which view the panel is on; nobody else may.
            if self.path != "/api/screen":
                return self._send(404, "text/plain", b"not found")
            if not is_kiosk(self.client_address[0], self.headers):
                return self._send(403, "text/plain", b"only brick1982's own screen sets the view")
            try:
                length = max(0, min(int(self.headers.get("Content-Length", 0)), 1024))
                report = json.loads(self.rfile.read(length) or b"{}")
                view, body = report.get("view"), report.get("body", 0)
            except (ValueError, AttributeError):
                view, body = None, 0
            if state.show(view, body):
                self._send(204, "text/plain", b"")
            else:
                self._send(400, "text/plain", b"unknown view")

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
    # at arena.brick.nozdormu.cloud. Only the kiosk itself can change the view
    # (is_kiosk); it holds no secrets.
    serve(state, env("HTTP_HOST", "0.0.0.0"), int(env("HTTP_PORT", "8766")),
          int(env("ROTATE_SECONDS", "45")))


if __name__ == "__main__":
    main()
