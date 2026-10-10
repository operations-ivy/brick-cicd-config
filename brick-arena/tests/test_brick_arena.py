import importlib.machinery
import importlib.util
import json
import struct
import tempfile
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path

from brick_arena import collect
from brick_arena.__main__ import State, home_from, is_kiosk
from brick_arena.backlight import Backlight, in_window, parse_window


class FakeProm:
    def __init__(self, results):
        self.results = results

    def query(self, promql):
        for key, value in self.results.items():
            if key in promql:
                return value
        return []


def info(ns, pod, node, kind, owner):
    return ({"namespace": ns, "pod": pod, "node": node,
             "created_by_kind": kind, "created_by_name": owner}, 1)


class WorkloadTests(unittest.TestCase):
    def test_deployment_pods_share_a_name_across_rollouts(self):
        a = collect.workload_of(info("wigle", "wigle-console-769b4d8474-24vns", "brick666",
                                     "ReplicaSet", "wigle-console-769b4d8474")[0])
        b = collect.workload_of(info("wigle", "wigle-console-68c979c8c5-f7s4w", "brick1982",
                                     "ReplicaSet", "wigle-console-68c979c8c5")[0])
        self.assertEqual(a, ("Deployment", "wigle/wigle-console"))
        self.assertEqual(a, b)

    def test_daemonset_pods_are_one_per_node(self):
        kind, name = collect.workload_of(info("monitoring", "promtail-jn92l", "brick1982",
                                              "DaemonSet", "promtail")[0])
        self.assertEqual((kind, name), ("DaemonSet", "monitoring/promtail@brick1982"))

    def test_statefulset_and_job_names(self):
        self.assertEqual(collect.workload_of(info("jenkins", "jenkins-0", "brick2000", "StatefulSet", "jenkins")[0]),
                         ("StatefulSet", "jenkins/jenkins-0"))
        self.assertEqual(collect.workload_of(info("chuck", "importer-kvlcb", "brick1982", "Job", "importer")[0]),
                         ("Job", "chuck/importer"))

    def test_nodes_ordered_by_number(self):
        names = ["brick2000", "brick666", "brick1982", "brick420"]
        self.assertEqual(sorted(names, key=collect.node_order), ["brick420", "brick666", "brick1982", "brick2000"])

    def test_finished_pods_are_left_out(self):
        prom = FakeProm({
            "kube_pod_info": [info("wigle", "wigle-sync-29860620-nppqk", "brick666", "Job", "wigle-sync-29860620"),
                              info("chuck", "reader-7d-qn4mw", "brick666", "ReplicaSet", "reader-7d778c88c9")],
            "kube_pod_status_phase": [({"namespace": "wigle", "pod": "wigle-sync-29860620-nppqk"}, 1)],
        })
        self.assertEqual([p.workload for p in collect.pods(prom)], ["chuck/reader"])


def pod(name, node, workload, restarts=0):
    return collect.Pod("ns", name, node, workload, "Deployment", restarts=restarts)


class TrackerTests(unittest.TestCase):
    def test_first_snapshot_has_no_events(self):
        t = collect.Tracker()
        self.assertEqual(t.update([pod("a-1", "brick666", "ns/a")], 0), [])

    def test_new_pod_on_another_node_is_a_move(self):
        t = collect.Tracker()
        t.update([pod("a-1", "brick666", "ns/a")], 0)
        # The rollout: old and new pods overlap, then the old one goes.
        [move] = t.update([pod("a-1", "brick666", "ns/a"), pod("a-2", "brick1982", "ns/a")], 10)
        self.assertEqual((move.kind, move.src, move.dst), ("move", "brick666", "brick1982"))
        self.assertEqual(t.update([pod("a-2", "brick1982", "ns/a")], 20), [])

    def test_new_pod_on_same_node_and_restarts(self):
        t = collect.Tracker()
        t.update([pod("a-1", "brick666", "ns/a")], 0)
        [new] = t.update([pod("a-2", "brick666", "ns/a")], 10)
        self.assertEqual(new.kind, "new")
        [restart] = t.update([pod("a-2", "brick666", "ns/a", restarts=1)], 20)
        self.assertEqual((restart.kind, restart.dst), ("restart", "brick666"))

    def test_events_expire(self):
        t = collect.Tracker()
        t.update([], 0)
        t.update([pod("a-1", "brick666", "ns/a")], 10)
        self.assertEqual(len(t.events), 1)
        t.update([pod("a-1", "brick666", "ns/a")], 10 + collect.EVENT_SECONDS)
        self.assertEqual(t.events, [])


class WeatherTests(unittest.TestCase):
    def test_forecast_words(self):
        self.assertEqual(collect.forecast(0, 0), "Calm, dry")
        self.assertEqual(collect.forecast(200_000, 0.3), "Light breeze, drizzle")
        self.assertEqual(collect.forecast(5_000_000, 20), "Gale, downpour")

    def test_weather_sums_nodes(self):
        prom = FakeProm({"node_network_receive_bytes_total": [({"instance": "a"}, 100.0), ({"instance": "b"}, 50.0)],
                         "coredns_dns_requests_total": [({}, 0.25)]})
        self.assertEqual(collect.weather(prom), {"wind_bps": 150, "rain_qps": 0.25, "forecast": "Calm, drizzle"})


class RadarTests(unittest.TestCase):
    HOME = (40.0, -75.0)

    def write(self, rows):
        d = tempfile.mkdtemp()
        path = Path(d) / "radar.json"
        path.write_text(json.dumps(rows))
        return path

    def test_bearing_and_range_from_home(self):
        # About 1 km due north and 1 km due east of home.
        path = self.write([{"lat": 40.009, "lon": -75.0, "seen": 0},
                           {"lat": 40.0, "lon": -74.98826, "seen": 0},
                           {"lat": 41.0, "lon": -75.0, "seen": 0}])
        r = collect.radar_contacts(path, self.HOME, now=100)
        self.assertTrue(r["feed"])
        self.assertEqual(r["total"], 3)
        north, east = r["contacts"]  # the one 111 km away is out of range
        self.assertAlmostEqual(north["bearing"], 0, delta=0.5)
        self.assertAlmostEqual(north["range"], 1 / 3, delta=0.01)
        self.assertAlmostEqual(east["bearing"], 90, delta=0.5)
        self.assertEqual(north["age"], 100)

    def test_no_home_or_file_is_an_empty_scope(self):
        self.assertFalse(collect.radar_contacts(Path("/nonexistent"), self.HOME, 0)["feed"])
        self.assertFalse(collect.radar_contacts(self.write([]), None, 0)["feed"])

    def test_bad_rows_are_skipped(self):
        r = collect.radar_contacts(self.write([{"lat": "x"}, {"lon": 1}, {"lat": 40, "lon": -75}]), self.HOME, 0)
        self.assertEqual(len(r["contacts"]), 1)

    def test_home_parsing(self):
        self.assertEqual(home_from("40.1, -75.2"), (40.1, -75.2))
        self.assertIsNone(home_from(""))


class BacklightTests(unittest.TestCase):
    def test_window_wraps_midnight(self):
        w = parse_window("23:00-07:00")
        self.assertTrue(in_window(w, datetime(2026, 10, 10, 23, 30)))
        self.assertTrue(in_window(w, datetime(2026, 10, 11, 6, 59)))
        self.assertFalse(in_window(w, datetime(2026, 10, 11, 7, 0)))
        self.assertFalse(in_window(parse_window(""), datetime(2026, 10, 11, 1, 0)))

    def test_writes_level_once(self):
        d = Path(tempfile.mkdtemp()) / "10-0045"
        d.mkdir()
        (d / "max_brightness").write_text("255\n")
        (d / "brightness").write_text("255\n")
        light = Backlight(str(d.parent))
        light.set_fraction(0.08)
        self.assertEqual((d / "brightness").read_text(), "20")
        (d / "brightness").write_text("99")
        light.set_fraction(0.08)  # unchanged: not rewritten
        self.assertEqual((d / "brightness").read_text(), "99")

    def test_no_backlight_is_fine(self):
        Backlight(tempfile.mkdtemp()).set_fraction(1.0)


class ScreenTests(unittest.TestCase):
    def test_only_the_local_unproxied_browser_is_the_kiosk(self):
        self.assertTrue(is_kiosk("127.0.0.1", {}))
        self.assertTrue(is_kiosk("::1", {}))
        # Through brick9000's proxy, or straight from the LAN: viewers.
        self.assertFalse(is_kiosk("192.0.2.21", {"X-Forwarded-For": "192.0.2.50"}))
        self.assertFalse(is_kiosk("192.0.2.50", {}))
        self.assertFalse(is_kiosk("127.0.0.1", {"X-Forwarded-For": "192.0.2.50"}))

    def test_viewers_see_what_the_kiosk_reported(self):
        state = State()
        self.assertEqual(state.get()["screen"]["view"], "arena")
        self.assertTrue(state.show("weather"))
        at = state.get()["screen"]["at"]
        self.assertEqual(state.get()["screen"]["view"], "weather")
        state.show("weather")  # unchanged: keeps when it switched
        self.assertEqual(state.get()["screen"]["at"], at)
        self.assertFalse(state.show("nonsense"))
        self.assertFalse(state.show(None))
        self.assertEqual(state.get()["screen"]["view"], "weather")


class BlankCursorTests(unittest.TestCase):
    def test_every_cursor_is_one_transparent_pixel(self):
        path = Path(__file__).resolve().parents[2] / "brick1982" / "blank-cursors"
        loader = importlib.machinery.SourceFileLoader("blank_cursors", str(path))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("blank_cursors", loader))
        loader.exec_module(mod)
        theme = Path(tempfile.mkdtemp()) / "default"
        with unittest.mock.patch("sys.argv", ["blank-cursors", str(theme)]):
            mod.main()
        data = (theme / "cursors" / "default").read_bytes()
        magic, header, _, ntoc = struct.unpack_from("<4sIII", data)
        self.assertEqual((magic, header, ntoc), (b"Xcur", 16, 1))
        _, _, pos = struct.unpack_from("<III", data, 16)
        width, height = struct.unpack_from("<II", data, pos + 16)
        self.assertEqual((width, height), (1, 1))
        self.assertEqual(struct.unpack_from("<I", data, pos + 36)[0], 0)  # alpha 0
        self.assertTrue((theme / "cursors" / "left_ptr").is_file())


if __name__ == "__main__":
    unittest.main()
