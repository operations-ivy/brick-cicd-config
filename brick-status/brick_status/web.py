"""The board page Chromium shows, the JSON state it polls, the CRT window's
stream, and the volume page."""

import base64
import ipaddress
import json
import mimetypes
import subprocess
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import board_metrics as bm
from . import matrix, volume
from .metrics import CONTENT_TYPE

STATIC = (Path(__file__).parent / "static").resolve()
# Changes every time brick-status starts, e.g. after a deploy; the page
# reloads itself when it sees a new one, so it always runs the latest code.
BOOT = str(time.time())
mimetypes.add_type("font/woff2", ".woff2")
# The volume page's own name. status.brick.nozdormu.cloud stays a read-only
# mirror: the volume can only be changed through this one (or locally).
VOLUME_HOST = "volume.brick.nozdormu.cloud"


def is_local(client: str, headers) -> bool:
    """brick9000's own kiosk: a loopback client, not through the proxy. It
    listens on the LAN too (for the cluster's front door), and a LAN client
    without X-Forwarded-For is still a viewer, not the kiosk."""
    try:
        loopback = ipaddress.ip_address(client).is_loopback
    except ValueError:
        return False
    return loopback and not headers.get("X-Forwarded-For")


def can_set_volume(local: bool, headers) -> bool:
    if local:
        return True  # brick9000 itself
    return (headers.get("Host") or "").split(":")[0].lower() == VOLUME_HOST


def crt_playlist(settings) -> dict:
    """The CRT window's programs that are installed, in order, and how long each plays."""
    def entry(name):
        p = matrix.PROGRAMS[name]
        return {"name": name, "tint": p.tint, "font": p.font}
    programs = [entry(n) for n in settings.crt_programs
                if n in matrix.PROGRAMS and matrix.PROGRAMS[n].available(settings.cmatrix)]
    return {"programs": programs or [entry("cmatrix")], "minutes": settings.crt_minutes}


def serve(settings, board, status) -> None:
    crt = crt_playlist(settings)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/api/state":
                now = time.time()
                if self._local():  # the kiosk, not the mirror
                    bm.KIOSK_SEEN.set(now)
                body = json.dumps({
                    "boot": BOOT,
                    "mode": board.mode(now),
                    "awake_until": board.awake_until(now),
                    "view": board.views[board.view(now)],
                    "views": board.views,
                    "updated": status.updated,
                    "now": now,
                    "crt": crt,
                    **status.get(),
                }).encode()
                self._send(200, "application/json", body)
            elif url.path == "/api/matrix":
                # Only the kiosk runs cmatrix. Through the LAN proxy (the read-only
                # status.brick.nozdormu.cloud mirror) it's refused: only one cmatrix runs at
                # a time, so a viewer starting one would stop the kiosk's.
                if not self._local():
                    self._send(403, "text/plain", b"cmatrix is for brick9000's own screen")
                else:
                    self._matrix(parse_qs(url.query))
            elif url.path == "/metrics":
                self._send(200, CONTENT_TYPE, bm.REGISTRY.render().encode())
            elif url.path == "/volume":
                self._file(STATIC / "volume.html")
            elif url.path == "/api/volume":
                self._volume()
            elif url.path in ("/", "/index.html"):
                self._file(STATIC / "index.html")
            elif url.path.startswith("/static/"):
                path = (STATIC / url.path.removeprefix("/static/")).resolve()
                if path.is_relative_to(STATIC) and path.is_file():
                    self._file(path)
                else:
                    self._send(404, "text/plain", b"not found")
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):
            if urlsplit(self.path).path != "/api/volume":
                self._send(404, "text/plain", b"not found")
            elif not can_set_volume(self._local(), self.headers):
                self._send(403, "text/plain", f"change the volume at https://{VOLUME_HOST}".encode())
            else:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(min(length, 1024)) or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError("not an object")
                    if "level" in body:
                        level = float(body["level"])
                        if level != level:  # NaN
                            raise ValueError("level is NaN")
                        volume.set_level(settings.wpctl, settings.max_volume, level)
                    if "muted" in body:
                        volume.set_muted(settings.wpctl, bool(body["muted"]))
                except (ValueError, TypeError) as e:
                    self._send(400, "text/plain", f"bad request: {e}".encode())
                    return
                except (OSError, subprocess.SubprocessError):
                    self._send(503, "text/plain", b"couldn't reach PipeWire")
                    return
                self._volume()

        def _local(self) -> bool:
            return is_local(self.client_address[0], self.headers)

        def _volume(self) -> None:
            try:
                state = volume.get(settings.wpctl, settings.max_volume)
            except (OSError, ValueError, subprocess.SubprocessError):
                self._send(503, "text/plain", b"couldn't reach PipeWire")
                return
            state["can_set"] = can_set_volume(self._local(), self.headers)
            self._send(200, "application/json", json.dumps(state).encode())

        def _matrix(self, query: dict) -> None:
            """Server-sent events, one base64 chunk of terminal output per event."""
            def arg(name, default):
                return query.get(name, [default])[0]

            self.send_response(200)
            bm.HTTP.inc(path="/api/matrix", code="200")
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                size = int(arg("cols", "80")), int(arg("rows", "24"))
            except ValueError:
                return
            program = arg("program", "cmatrix")
            bm.CRT_STARTS.inc(program=program if program in matrix.PROGRAMS else "cmatrix")
            bm.CRT_STREAMS.inc()
            # closing() stops the program as soon as the page goes away.
            try:
                with closing(matrix.stream(program, *size, arg("colour", "green"),
                                           settings.cmatrix)) as chunks:
                    try:
                        for chunk in chunks:
                            self.wfile.write(b"data: " + base64.b64encode(chunk) + b"\n\n")
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # page closed the stream (left the overview, resized, recoloured)
            finally:
                bm.CRT_STREAMS.inc(-1)

        def _file(self, path: Path) -> None:
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, ctype, path.read_bytes())

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            bm.HTTP.inc(path=bm.http_path(urlsplit(self.path).path), code=str(code))
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # the page polls every second; keep the journal quiet
            pass

    ThreadingHTTPServer((settings.http_host, settings.http_port), Handler).serve_forever()
