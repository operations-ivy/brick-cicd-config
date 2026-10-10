"""Shared bits for the functional tests: a stub Prometheus over real HTTP,
free ports, and a directory of fake commands to put first on PATH."""

import json
import os
import socket
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StubPrometheus:
    """Answers /api/v1/query with the first result whose key is in the query."""

    def __init__(self, results: dict[str, list[tuple[dict, float]]]):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                query = parse_qs(urlsplit(self.path).query).get("query", [""])[0]
                stub.queries.append(query)
                found = next((v for k, v in stub.results.items() if k in query), [])
                body = json.dumps({"status": "success", "data": {"resultType": "vector", "result": [
                    {"metric": m, "value": [0, str(v)]} for m, v in found]}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.results, self.queries = results, []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def fake_commands(directory: Path, scripts: dict[str, str]) -> str:
    """Write executable shell scripts into `directory`; returns a PATH with it first."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in scripts.items():
        p = directory / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return f"{directory}{os.pathsep}{os.environ['PATH']}"
