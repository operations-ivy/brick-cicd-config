"""brick-arena: the display daemon on brick1982's 7" touchscreen.

Polls Prometheus, serves the page Chromium shows in cage, dims the panel
overnight and turns it off on brick9000's quiet schedule (a tap wakes it). The page rotates through its views, coming back to main (a turning
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

from . import arena_metrics as am
from . import collect
from .backlight import DEFAULT_QUIET, Backlight, QuietSchedule, in_window, parse_window

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
                if view != self.screen["view"]:
                    am.VIEW_CHANGES.inc(view=view)
                self.screen = {"view": view, "body": body, "at": time.time()}
        for v in VIEWS:
            am.VIEW.set(1 if v == view else 0, view=v)
        am.BODY.set(body)
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


class Panel:
    """The backlight: off on the quiet schedule unless a tap woke it, dimmed
    overnight, otherwise at the day level. Like brick9000's side button, a
    tap during quiet hours keeps it on for wake_seconds (from the last tap)."""

    def __init__(self, light: Backlight, quiet: QuietSchedule, dim_window, dim_level: float,
                 day_level: float, wake_seconds: float):
        self.light, self.quiet, self.dim_window = light, quiet, dim_window
        self.dim_level, self.day_level, self.wake_seconds = dim_level, day_level, wake_seconds
        self._awake_until = 0.0
        self._lock = threading.Lock()

    def asleep(self, now: float) -> bool:
        return now >= self._awake_until and self.quiet.is_quiet(datetime.fromtimestamp(now))

    def wake(self, now: float) -> None:
        am.WAKES.inc()
        with self._lock:
            if self.quiet.is_quiet(datetime.fromtimestamp(now)):
                self._awake_until = now + self.wake_seconds
        self.update(now)

    def update(self, now: float) -> None:
        with self._lock:
            asleep = self.asleep(now)
            if asleep:
                self.light.off()
                level = 0.0
            else:
                dim = in_window(self.dim_window, datetime.fromtimestamp(now))
                level = self.dim_level if dim else self.day_level
                self.light.set_fraction(level)
        am.ASLEEP.set(1 if asleep else 0)
        am.BRIGHTNESS.set(level)


def panel_from_env() -> Panel:
    return Panel(Backlight(), QuietSchedule.parse(env("QUIET", DEFAULT_QUIET)),
                 parse_window(env("DIM", "23:00-07:00")), float(env("DIM_LEVEL", "0.08")),
                 float(env("DAY_LEVEL", "1.0")), float(env("WAKE_SECONDS", "1800")))


def poll(state: State, panel: Panel) -> None:
    prom = collect.Prometheus(env("PROMETHEUS_URL", "https://prometheus.brick.nozdormu.cloud"))
    tracker = collect.Tracker()
    seconds = float(env("POLL_SECONDS", "10"))
    while True:
        started = time.monotonic()
        tracker.new = []
        try:
            state.set(collect.snapshot(prom, tracker))
            am.POLLS.inc(result="ok")
        except Exception as e:  # network errors, bad responses: keep the last picture
            log.warning("poll failed: %s", e)
            state.fail(str(e))
            am.POLLS.inc(result="error")
        for e in tracker.new:
            am.EVENTS.inc(kind=e.kind)
        am.POLL_SECONDS.set(time.monotonic() - started)
        am.POLLED_AT.set(time.time())
        panel.update(time.time())
        time.sleep(seconds)


def serve(state: State, host: str, port: int, rotate: int, panel: Panel | None = None) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/state":
                now = time.time()
                kiosk = is_kiosk(self.client_address[0], self.headers)
                if kiosk:
                    am.KIOSK_SEEN.set(now)
                body = json.dumps({"boot": BOOT, "rotate": rotate, "now": now,
                                   "kiosk": kiosk,
                                   "asleep": bool(panel and panel.asleep(now)), **state.get()})
                self._send(200, "application/json", body.encode())
            elif self.path == "/metrics":
                self._send(200, am.CONTENT_TYPE, am.REGISTRY.render().encode())
            elif self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", (STATIC / "index.html").read_bytes())
            elif self.path.startswith("/art/") and (art := art_file(self.path)):
                self._send(200, "image/png", art.read_bytes())
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):
            # The kiosk says which view the panel is on, and wakes it with a
            # tap; nobody else may.
            if self.path not in ("/api/screen", "/api/wake"):
                return self._send(404, "text/plain", b"not found")
            if not is_kiosk(self.client_address[0], self.headers):
                return self._send(403, "text/plain", b"only brick1982's own screen sets the view")
            if self.path == "/api/wake":
                if panel:
                    panel.wake(time.time())
                return self._send(204, "text/plain", b"")
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
            am.HTTP.inc(path=am.http_path(self.path), code=str(code))
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
    state, panel = State(), panel_from_env()
    threading.Thread(target=poll, args=(state, panel), daemon=True).start()
    # Every address, not just localhost: brick9000's proxy shows the same page
    # at arena.brick.nozdormu.cloud. Only the kiosk itself can change the view
    # (is_kiosk); it holds no secrets.
    serve(state, env("HTTP_HOST", "0.0.0.0"), int(env("HTTP_PORT", "8766")),
          int(env("ROTATE_SECONDS", "45")), panel)


if __name__ == "__main__":
    main()
