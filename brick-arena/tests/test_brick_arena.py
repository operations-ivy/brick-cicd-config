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
from brick_arena.__main__ import Panel, State, art_file, is_kiosk
from brick_arena.backlight import DEFAULT_QUIET, Backlight, QuietSchedule, in_window, parse_window


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

    def test_new_holds_only_the_last_updates_events(self):
        t = collect.Tracker()
        t.update([pod("a-1", "brick666", "ns/a")], 0)
        t.update([pod("a-2", "brick666", "ns/a")], 10)
        self.assertEqual([e.kind for e in t.new], ["new"])
        t.update([pod("a-2", "brick666", "ns/a")], 20)
        self.assertEqual(t.new, [])

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
        Backlight(tempfile.mkdtemp()).off()

    def test_off_writes_zero_and_back_on(self):
        d = Path(tempfile.mkdtemp()) / "10-0045"
        d.mkdir()
        (d / "max_brightness").write_text("255\n")
        (d / "brightness").write_text("255\n")
        light = Backlight(str(d.parent))
        light.off()
        self.assertEqual((d / "brightness").read_text(), "0")
        light.set_fraction(0.0)  # dimmest lit level, not off
        self.assertEqual((d / "brightness").read_text(), "1")

    def test_default_quiet_mirrors_brick9000(self):
        q = QuietSchedule.parse(DEFAULT_QUIET)
        # 2026-10-12 is a Monday; 2026-10-17 a Saturday.
        self.assertTrue(q.is_quiet(datetime(2026, 10, 12, 9, 0)))
        self.assertTrue(q.is_quiet(datetime(2026, 10, 16, 15, 59)))
        self.assertFalse(q.is_quiet(datetime(2026, 10, 12, 16, 0)))
        self.assertFalse(q.is_quiet(datetime(2026, 10, 16, 23, 59)))
        self.assertFalse(q.is_quiet(datetime(2026, 10, 17, 3, 0)))
        self.assertFalse(q.is_quiet(datetime(2026, 10, 18, 23, 59)))


class FakeLight:
    def __init__(self):
        self.level = None

    def off(self):
        self.level = 0

    def set_fraction(self, fraction):
        self.level = fraction


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.light = FakeLight()
        self.panel = Panel(self.light, QuietSchedule.parse(DEFAULT_QUIET), parse_window("23:00-07:00"),
                           0.08, 1.0, 1800)

    @staticmethod
    def at(*args):
        return datetime(*args).timestamp()

    def test_off_in_quiet_hours_dim_and_day_otherwise(self):
        self.panel.update(self.at(2026, 10, 12, 9, 0))  # Monday morning
        self.assertEqual(self.light.level, 0)
        self.panel.update(self.at(2026, 10, 12, 20, 0))
        self.assertEqual(self.light.level, 1.0)
        self.panel.update(self.at(2026, 10, 17, 23, 30))  # Saturday night
        self.assertEqual(self.light.level, 0.08)

    def test_tap_wakes_for_wake_seconds_from_the_last_tap(self):
        t = self.at(2026, 10, 12, 9, 0)
        self.panel.wake(t)
        self.assertEqual(self.light.level, 1.0)
        self.assertFalse(self.panel.asleep(t + 1700))
        self.panel.wake(t + 1700)
        self.panel.update(t + 1800 + 60)
        self.assertEqual(self.light.level, 1.0)
        self.panel.update(t + 1700 + 1800)
        self.assertEqual(self.light.level, 0)

    def test_panel_metrics_follow_the_light(self):
        from brick_arena import arena_metrics as am
        self.panel.update(self.at(2026, 10, 12, 9, 0))  # Monday morning: quiet
        self.assertEqual((am.ASLEEP.get(), am.BRIGHTNESS.get()), (1, 0))
        self.panel.update(self.at(2026, 10, 17, 23, 30))  # Saturday night: dim
        self.assertEqual((am.ASLEEP.get(), am.BRIGHTNESS.get()), (0, 0.08))

    def test_tap_outside_quiet_hours_leaves_nothing_behind(self):
        evening = self.at(2026, 10, 12, 20, 0)
        self.panel.wake(evening)
        self.assertTrue(self.panel.asleep(self.at(2026, 10, 13, 0, 30)))


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
        self.assertEqual(state.get()["screen"]["view"], "main")
        self.assertTrue(state.show("weather"))
        at = state.get()["screen"]["at"]
        self.assertEqual(state.get()["screen"]["view"], "weather")
        state.show("weather")  # unchanged: keeps when it switched
        self.assertEqual(state.get()["screen"]["at"], at)
        self.assertTrue(state.show("main", 7))
        self.assertEqual((state.get()["screen"]["view"], state.get()["screen"]["body"]), ("main", 7))
        for bad_body in (-1, 100, "3", 2.5, True, None):
            self.assertFalse(state.show("main", bad_body), bad_body)
        state.show("weather")
        self.assertFalse(state.show("nonsense"))
        self.assertFalse(state.show(None))
        self.assertEqual(state.get()["screen"]["view"], "weather")


class ArtTests(unittest.TestCase):
    def test_serves_only_plain_png_names_in_art(self):
        self.assertEqual(art_file("/art/earth.png").name, "earth.png")
        for bad in ["/art/../index.html", "/art/..%2findex.html", "/art/README.md", "/art/missing.png",
                    "/art/sub/earth.png", "/art/.png", "/art/Earth.PNG"]:
            self.assertIsNone(art_file(bad), bad)

    def test_earth_sheet_is_twenty_32px_frames(self):
        path = art_file("/art/earth.png")
        width, height = struct.unpack(">II", path.read_bytes()[16:24])
        self.assertEqual((width, height), (32 * 20, 32))

    def test_every_n64_planet_has_a_30_frame_64px_sheet(self):
        for name in ["alien", "candy", "city", "desert", "forest", "gas", "ice", "ocean", "rock", "terran"]:
            for ringed in ("", "-ringed"):
                path = art_file(f"/art/n64-{name}{ringed}.png")
                self.assertIsNotNone(path, name + ringed)
                self.assertEqual(struct.unpack(">II", path.read_bytes()[16:24]), (64 * 30, 64))


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
