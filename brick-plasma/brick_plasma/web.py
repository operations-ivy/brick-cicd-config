"""The plasma API. See the README, "The plasma API"."""

import json
import re
import time
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import apa102, patterns, signals

SOURCE = re.compile(r"^/signals/([A-Za-z0-9._-]{1,64})$")
MAX_BODY = 4096
MIN_SECONDS, MAX_SECONDS = 0.2, 10.0


class BadRequest(Exception):
    pass


def _colour(value) -> tuple:
    if isinstance(value, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", value):
        digits = value.lstrip("#")
        return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
    if (isinstance(value, list) and len(value) == 3
            and all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255 for v in value)):
        return tuple(value)
    raise BadRequest(f"not a colour: {value!r} (use [r, g, b] or \"#rrggbb\")")


def _number(body: dict, key: str, default: float, low: float, high: float) -> float:
    value = body.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value != value:
        raise BadRequest(f"{key} must be a number")
    return max(low, min(high, float(value)))


class Catalog:
    """Turns a request into frames ready to clock out. The same request gets
    the same object back, which is how a repeat is known to be one."""

    def __init__(self):
        self.named = {name: tuple(apa102.encode(f) for f in frames)
                      for name, frames in patterns.named().items()}
        self.custom = lru_cache(maxsize=64)(self._build)

    @staticmethod
    def _build(kind: str, *args) -> tuple:
        if kind == "buttons":
            colours, selected, busy = args
            frames = patterns.buttons(dict(colours), selected, set(busy))
        else:
            frames = patterns.EFFECTS[kind](*args)
        return tuple(apa102.encode(f) for f in frames)

    def resolve(self, body: dict) -> tuple[str, tuple]:
        """A body's (label, frames)."""
        asked = [k for k in ("pattern", "color", "buttons") if k in body]
        if len(asked) != 1:
            raise BadRequest("send exactly one of pattern, color or buttons")
        if asked == ["pattern"]:
            name = body["pattern"]
            if not isinstance(name, str) or name not in self.named:
                raise BadRequest(f"no pattern {name!r}: GET /patterns")
            return name, self.named[name]
        if asked == ["color"]:
            effect = body.get("effect", "solid")
            if not isinstance(effect, str) or effect not in patterns.EFFECTS:
                raise BadRequest(f"no effect {effect!r}: GET /patterns")
            seconds = _number(body, "seconds", 2.0, MIN_SECONDS, MAX_SECONDS)
            return "custom", self.custom(effect, _colour(body["color"]), seconds)
        return "buttons", self.custom("buttons", *self._buttons(body["buttons"]))

    @staticmethod
    def _buttons(spec) -> tuple:
        if not isinstance(spec, dict) or not isinstance(spec.get("colors"), dict):
            raise BadRequest("buttons needs colors: {slot: colour}")
        def slot(value):
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise BadRequest(f"not a slot: {value!r}") from None
            if not 0 <= number < patterns.SLOTS:
                raise BadRequest(f"not a slot: {value!r}")
            return number
        colours = tuple(sorted((slot(k), _colour(v)) for k, v in spec["colors"].items()))
        selected = spec.get("selected")
        busy = spec.get("busy", [])
        if not isinstance(busy, list):
            raise BadRequest("busy must be a list of slots")
        return colours, None if selected is None else slot(selected), tuple(sorted(slot(b) for b in busy))


def describe(signal, now: float) -> dict:
    return {"source": signal.source, "pattern": signal.label, "priority": signal.priority,
            "expires_in": round(signal.expires - now, 1)}


def make_server(host: str, port: int, catalog: Catalog, board: signals.Signals, stats, clock=time.monotonic):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # the front door keeps its connection open
        disable_nagle_algorithm = True  # a signal goes out now, not after an ACK
        timeout = 30

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                now = clock()
                showing = [describe(s, now) for s in board.active(now)]
                self._json(200, {"showing": showing[0] if showing else None, "signals": showing})
            elif path == "/patterns":
                self._json(200, {"patterns": sorted(catalog.named), "effects": sorted(patterns.EFFECTS)})
            elif path == "/healthz":
                self._json(200, {"ok": True})
            elif path == "/metrics":
                self._reply(200, "text/plain; version=0.0.4", stats.render(len(board.active(clock()))).encode())
            else:
                self._json(404, {"error": "not found"})

        def do_PUT(self):
            match = SOURCE.match(urlsplit(self.path).path)
            body = self._body()  # read even when refusing, or keep-alive breaks
            if not match:
                self._json(404, {"error": "PUT /signals/<source>"})
                return
            try:
                if body is None:
                    raise BadRequest("the body must be a JSON object")
                label, frames = catalog.resolve(body)
                priority = int(_number(body, "priority", signals.DEFAULT_PRIORITY, 0, 100))
                ttl = _number(body, "ttl", signals.DEFAULT_TTL, 0.1, signals.MAX_TTL)
                board.put(match[1], label, frames, priority, ttl, clock())
            except BadRequest as e:
                self._json(400, {"error": str(e)})
            except signals.Full:
                self._json(429, {"error": f"{signals.MAX_SIGNALS} signals held already"})
            else:
                self._json(200, {"source": match[1], "pattern": label, "priority": priority, "ttl": ttl})

        def do_DELETE(self):
            match = SOURCE.match(urlsplit(self.path).path)
            self._body()
            if not match:
                self._json(404, {"error": "DELETE /signals/<source>"})
            else:
                board.remove(match[1])  # gone already is what was asked for
                self._reply(204, None, b"")

        def _body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self.close_connection = True  # the rest of it can't be skipped
                return None
            try:
                value = json.loads(self.rfile.read(length) or b"null")
            except ValueError:
                return None
            return value if isinstance(value, dict) else None

        def _json(self, code: int, value: dict) -> None:
            self._reply(code, "application/json", json.dumps(value).encode())

        def _reply(self, code: int, ctype: str | None, body: bytes) -> None:
            stats.count(code)
            self.send_response(code)
            if ctype:
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # a build makes dozens of these
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
