"""jenkins/bin/maintenance: windows in a stub Pushgateway, node choices from a stub Prometheus."""

import contextlib
import importlib.machinery
import importlib.util
import io
import math
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from functional.helpers import StubPrometheus

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True  # no __pycache__ in jenkins/bin: the image copies it
loader = importlib.machinery.SourceFileLoader("maintenance", str(ROOT / "jenkins/bin/maintenance"))
spec = importlib.util.spec_from_loader("maintenance", loader)
mt = importlib.util.module_from_spec(spec)
loader.exec_module(mt)


class StubPushgateway:
    """Records PUT and DELETE on /metrics/job/maintenance/node/<node>."""

    def __init__(self):
        stub = self
        self.calls = []

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, body=b""):
                stub.calls.append((self.command, self.path, body.decode()))
                self.send_response(202 if self.command == "PUT" else 202)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_PUT(self):
                self._answer(self.rfile.read(int(self.headers.get("Content-Length") or 0)))

            def do_DELETE(self):
                self._answer()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Pure(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(mt.until_for("1h", 1000), 4600)
        self.assertEqual(mt.until_for("24h", 0), 86400)
        self.assertEqual(mt.until_for("indefinite", 0), math.inf)
        with self.assertRaises(SystemExit):
            mt.until_for("2h", 0)

    def test_choices_are_online_bricks_plus_maintenance_never_brick420(self):
        got = mt.choices(["brick420", "brick2000", "brick666"], ["brick9000", "brick1982", "brick420", "laptop"],
                         ["brick69"])
        self.assertEqual(got, ["brick69", "brick666", "brick1982", "brick2000", "brick9000"])

    def test_active_windows(self):
        self.assertEqual(mt.active({"a": 50, "b": 200, "c": math.inf}, 100), {"b": 200, "c": math.inf})


class WithStubs(unittest.TestCase):
    def setUp(self):
        self.push = StubPushgateway()
        self.addCleanup(self.push.close)
        self.prom = StubPrometheus({
            "brick_maintenance_until_seconds": [({"node": "brick666"}, 1.0), ({"node": "brick1982"}, math.inf)],
            "kube_node_status_condition": [({"node": "brick420"}, 1), ({"node": "brick2000"}, 1)],
            "node_uname_info": [({"nodename": "brick9000"}, 1)],
        })
        self.addCleanup(self.prom.close)
        patcher = mock.patch.multiple(mt, PUSHGATEWAY=self.push.url, PROMETHEUS=self.prom.url)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_window(self, **env):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            mt.window()
        return out.getvalue()

    def test_a_window_is_pushed_with_its_end(self):
        with mock.patch.object(mt.time, "time", return_value=1000.0):
            said = self.run_window(NODE="brick666", DURATION="4h", END="false")
        method, path, body = self.push.calls[0]
        self.assertEqual((method, path), ("PUT", "/metrics/job/maintenance/node/brick666"))
        self.assertIn("brick_maintenance_until_seconds 15400\n", body)
        self.assertIn("in maintenance until", said)

    def test_indefinite_is_inf(self):
        self.run_window(NODE="brick2000", DURATION="indefinite", END="false")
        self.assertIn("brick_maintenance_until_seconds +Inf\n", self.push.calls[0][2])

    def test_end_deletes_it(self):
        said = self.run_window(NODE="brick666", DURATION="1h", END="true")
        self.assertEqual(self.push.calls, [("DELETE", "/metrics/job/maintenance/node/brick666", "")])
        self.assertIn("out of maintenance", said)

    def test_brick420_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_window(NODE="brick420", DURATION="1h", END="false")
        self.assertEqual(self.push.calls, [])

    def test_refresh_prunes_ended_windows_and_writes_choices(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            path = f"{d}/choices/maintenance-nodes"
            with mock.patch.object(mt.time, "time", return_value=100.0):
                self.assertEqual(mt.refresh(path), mt.CHANGED)
                # brick666's window ended at 1.0: deleted, and not listed (it isn't online)
                self.assertEqual(self.push.calls, [("DELETE", "/metrics/job/maintenance/node/brick666", "")])
                self.assertEqual(Path(path).read_text(), "brick1982\nbrick2000\nbrick9000\n")
                self.assertEqual(mt.refresh(path), 0)  # unchanged: no re-seed


if __name__ == "__main__":
    unittest.main()
