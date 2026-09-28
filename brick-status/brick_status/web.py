"""The board page Chromium shows, the JSON state it polls, and the cmatrix stream."""

import base64
import json
import mimetypes
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import matrix

STATIC = (Path(__file__).parent / "static").resolve()
# Changes every time brick-status starts, e.g. after a deploy; the page
# reloads itself when it sees a new one, so it always runs the latest code.
BOOT = str(time.time())
mimetypes.add_type("font/woff2", ".woff2")


def serve(settings, board, status) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/api/state":
                now = time.time()
                body = json.dumps({
                    "boot": BOOT,
                    "mode": board.mode(now),
                    "view": board.views[board.view(now)],
                    "views": board.views,
                    "updated": status.updated,
                    "now": now,
                    **status.get(),
                }).encode()
                self._send(200, "application/json", body)
            elif url.path == "/api/matrix":
                self._matrix(parse_qs(url.query))
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

        def _matrix(self, query: dict) -> None:
            """Server-sent events, one base64 chunk of terminal output per event."""
            def arg(name, default):
                return query.get(name, [default])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                size = int(arg("cols", "80")), int(arg("rows", "24"))
            except ValueError:
                return
            # closing() stops cmatrix as soon as the page goes away.
            with closing(matrix.stream(settings.cmatrix, *size, arg("colour", "green"))) as chunks:
                try:
                    for chunk in chunks:
                        self.wfile.write(b"data: " + base64.b64encode(chunk) + b"\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # page closed the stream (left the overview, resized, recoloured)

        def _file(self, path: Path) -> None:
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, ctype, path.read_bytes())

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # the page polls every second; keep the journal quiet
            pass

    ThreadingHTTPServer((settings.http_host, settings.http_port), Handler).serve_forever()
