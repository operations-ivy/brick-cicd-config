"""brick-arena's HTTP server and snapshot, end to end, against a stub
Prometheus over real HTTP."""

import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

from functional.helpers import ROOT, StubPrometheus, free_port

sys.path.insert(0, str(ROOT / "brick-arena"))
from brick_arena import collect  # noqa: E402
from brick_arena.__main__ import Panel, State, serve  # noqa: E402
from brick_arena.backlight import QuietSchedule  # noqa: E402

RESULTS = {
    'kube_node_status_condition': [({"node": "brick420"}, 1), ({"node": "brick1982"}, 1)],
    "node_uname_info": [({"instance": "a:9100", "nodename": "brick420"}, 1), ({"instance": "b:9100", "nodename": "brick1982"}, 1)],
    'mode="idle"': [({"instance": "a:9100"}, 12.0), ({"instance": "b:9100"}, 30.0)],
    "kube_pod_info": [({"namespace": "wigle", "pod": "wigle-console-6b9b-9ndtv", "node": "brick1982",
                        "created_by_kind": "ReplicaSet", "created_by_name": "wigle-console-6b9bddfbd8"}, 1)],
    "kube_pod_status_ready": [({"namespace": "wigle", "pod": "wigle-console-6b9b-9ndtv"}, 1)],
    "coredns_dns_requests_total": [({}, 0.4)],
}


class ArenaOverHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prom = StubPrometheus(RESULTS)
        state = State()
        state.set(collect.snapshot(collect.Prometheus(cls.prom.url), collect.Tracker()))
        cls.port = free_port()

        class Light:
            level = None
            def off(self): self.level = 0
            def set_fraction(self, f): self.level = f
        cls.light = Light()
        # Quiet around the clock, so the panel starts asleep.
        cls.panel = Panel(cls.light, QuietSchedule.parse("00:00-23:59"), None, 0.08, 1.0, 1800)
        threading.Thread(target=serve, args=(state, "127.0.0.1", cls.port, 45, cls.panel), daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(cls.base + "/api/state", timeout=1)
                break
            except OSError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.prom.close()

    def get(self, path, headers=None):
        return urllib.request.urlopen(urllib.request.Request(self.base + path, headers=headers or {}), timeout=5)

    def post_view(self, view, headers=None):
        req = urllib.request.Request(self.base + "/api/screen", data=json.dumps({"view": view}).encode(),
                                     headers=headers or {}, method="POST")
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    def test_state_carries_what_prometheus_said(self):
        s = json.load(self.get("/api/state"))
        self.assertEqual([n["node"] for n in s["nodes"]], ["brick420", "brick1982"])
        self.assertEqual(s["pods"][0]["workload"], "wigle/wigle-console")
        self.assertEqual(s["weather"]["forecast"], "Calm, drizzle")
        self.assertTrue(s["kiosk"])  # this test talks to it over loopback

    def test_metrics(self):
        self.get("/api/state")
        self.assertEqual(self.post_view("arena"), 204)
        r = self.get("/metrics")
        self.assertTrue(r.headers["Content-Type"].startswith("text/plain; version=0.0.4"))
        text = r.read().decode()
        self.assertIn('brick_arena_view{view="arena"} 1', text)
        self.assertIn('brick_arena_http_requests_total{path="/api/screen",code="204"}', text)
        self.assertRegex(text, r"brick_arena_kiosk_last_seen_timestamp_seconds [1-9]")

    def test_only_the_local_unproxied_browser_sets_the_view(self):
        self.assertEqual(self.post_view("weather"), 204)
        self.assertEqual(json.load(self.get("/api/state"))["screen"]["view"], "weather")
        self.assertEqual(self.post_view("arena", {"X-Forwarded-For": "192.0.2.7"}), 403)
        self.assertEqual(self.post_view("nonsense"), 400)
        viewer = json.load(self.get("/api/state", {"X-Forwarded-For": "192.0.2.7"}))
        self.assertFalse(viewer["kiosk"])
        self.assertEqual(viewer["screen"]["view"], "weather")

    def test_only_the_kiosk_wakes_the_panel(self):
        def wake(headers=None):
            req = urllib.request.Request(self.base + "/api/wake", data=b"", headers=headers or {}, method="POST")
            try:
                return urllib.request.urlopen(req, timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(wake({"X-Forwarded-For": "192.0.2.7"}), 403)
        if time.localtime().tm_hour == 23 and time.localtime().tm_min == 59:
            self.skipTest("outside the always-quiet test schedule for this minute")
        self.assertTrue(json.load(self.get("/api/state"))["asleep"])
        self.assertEqual(wake(), 204)
        self.assertEqual(self.light.level, 1.0)
        self.assertFalse(json.load(self.get("/api/state"))["asleep"])

    def test_art_is_served_and_nothing_else(self):
        self.assertEqual(self.get("/art/earth.png").headers["Content-Type"], "image/png")
        for path in ("/art/../__main__.py", "/art/README.md", "/static/index.html", "/art/missing.png"):
            with self.assertRaises(urllib.error.HTTPError, msg=path) as e:
                self.get(path)
            self.assertEqual(e.exception.code, 404)

    def test_the_page_itself(self):
        page = self.get("/").read().decode()
        self.assertIn('const SEQUENCE = ["main", "arena", "main", "weather"]', page)
