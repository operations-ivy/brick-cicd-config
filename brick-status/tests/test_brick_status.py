import struct
import unittest
import zlib
from datetime import datetime, time

from brick_status import checks, lights, patterns
from brick_status.board import ACTIVE, IDLE, QUIET, Board, in_quiet_hours
from brick_status.config import VIEWS


def ts(hour, minute=0):
    return datetime(2026, 9, 26, hour, minute).timestamp()


class QuietHoursTest(unittest.TestCase):
    def test_same_day_window(self):
        self.assertTrue(in_quiet_hours(time(3), time(0), time(6)))
        self.assertFalse(in_quiet_hours(time(6), time(0), time(6)))
        self.assertFalse(in_quiet_hours(time(23, 59), time(0), time(6)))

    def test_window_across_midnight(self):
        self.assertTrue(in_quiet_hours(time(23), time(22), time(6)))
        self.assertTrue(in_quiet_hours(time(1), time(22), time(6)))
        self.assertFalse(in_quiet_hours(time(12), time(22), time(6)))

    def test_equal_start_and_end_disables(self):
        self.assertFalse(in_quiet_hours(time(3), time(0), time(0)))


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.board = Board(VIEWS, active_seconds=120, quiet_start=time(0), quiet_end=time(6))

    def test_idle_shows_overview(self):
        self.assertEqual(self.board.mode(ts(12)), IDLE)
        self.assertEqual(self.board.view(ts(12)), 0)

    def test_press_activates_view_then_times_out(self):
        self.board.press_view(2, ts(12))
        self.assertEqual(self.board.mode(ts(12, 1)), ACTIVE)
        self.assertEqual(self.board.view(ts(12, 1)), 2)
        self.assertEqual(self.board.mode(ts(12, 3)), IDLE)
        self.assertEqual(self.board.view(ts(12, 3)), 0)

    def test_quiet_hours_and_wake(self):
        self.assertEqual(self.board.mode(ts(3)), QUIET)
        self.board.press_view(1, ts(3))
        self.assertEqual(self.board.mode(ts(3, 1)), ACTIVE)
        self.assertEqual(self.board.mode(ts(3, 5)), QUIET)

    def test_other_key_wakes_without_changing_view(self):
        self.board.press_view(2, ts(2, 50))
        self.board.wake(ts(3))
        self.assertEqual(self.board.mode(ts(3, 1)), ACTIVE)
        self.assertEqual(self.board.view(ts(3, 1)), 2)

    def test_step_wraps(self):
        self.board.step_view(-1, ts(12))
        self.assertEqual(self.board.view(ts(12)), len(VIEWS) - 1)


def check(group, state):
    return {"group": group, "name": "x", "state": state, "detail": ""}


class IdlePatternTest(unittest.TestCase):
    def test_priorities(self):
        self.assertEqual(lights.idle_pattern([check("wigle", "active"), check("cluster", "fail")]), "alert")
        self.assertEqual(lights.idle_pattern([check("wigle", "active"), check("builds", "active")]), "building")
        self.assertEqual(lights.idle_pattern([check("wigle", "active"), check("cluster", "warn")]), "uploading")
        self.assertEqual(lights.idle_pattern([check("cluster", "warn")]), "warn")
        self.assertEqual(lights.idle_pattern([]), "unknown")
        self.assertEqual(lights.idle_pattern([check("cluster", "ok")]), "calm")

    def test_every_idle_pattern_exists(self):
        for states in (["fail"], ["warn"], ["ok"], []):
            self.assertIn(lights.idle_pattern([check("cluster", s) for s in states]), patterns.IDLE)


class PatternTest(unittest.TestCase):
    def test_png_is_valid_rgb(self):
        frames = patterns.IDLE["building"]()
        data = patterns.png(frames)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        width, height, depth, colour = struct.unpack(">IIBB", data[16:26])
        self.assertEqual((width, height, depth, colour), (patterns.WIDTH, len(frames), 8, 2))
        idat_len = struct.unpack(">I", data[33:37])[0]
        raw = zlib.decompress(data[41:41 + idat_len])
        self.assertEqual(len(raw), height * (1 + width * 3))

    def test_active_frames_light_only_view_buttons(self):
        frames = lights.active_frames({"builds": "fail"}, VIEWS, [0, 1, 2, 3, 4, 5], selected_view=1)
        first = frames[0]
        self.assertEqual(first[4 * 5], patterns.OFF)  # button 6 has no view
        r, g, b = first[4 * 1]
        self.assertTrue(r > 0 and g == 0)  # builds failing: red


class DebouncerTest(unittest.TestCase):
    def run_events(self, events):
        from brick_status.hardware import Debouncer

        d = Debouncer()
        accepted = []
        for code, kind, t in events:
            if kind == "down":
                accepted.append(d.press(code, t))
            else:
                d.release(code, t)
        return accepted

    def test_drops_bounce_keeps_real_presses(self):
        # Recorded from brick9000's top-middle button: three slow presses
        # (the last held ~450ms) that produced five key-downs.
        events = [(56, "down", 58.019), (56, "up", 58.431),
                  (56, "down", 59.824), (56, "up", 59.879),
                  (56, "down", 59.945), (56, "up", 60.330),
                  (56, "down", 61.884), (56, "up", 62.331),
                  (56, "down", 62.347), (56, "up", 62.379)]
        self.assertEqual(self.run_events(events), [True, True, False, True, False])

    def test_keys_are_independent(self):
        events = [(56, "down", 1.0), (56, "up", 1.1), (44, "down", 1.12)]
        self.assertEqual(self.run_events(events), [True, True])


class PlasmaTest(unittest.TestCase):
    def test_missed_command_is_retried(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            fifo = os.path.join(d, "plasma")
            plasma = lights.Plasma(fifo, d, "brick-status")
            plasma.show("calm")  # no FIFO yet: nothing sent
            self.assertIsNone(plasma.current)

            os.mkfifo(fifo)
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            try:
                plasma.show("calm")  # same pattern again: must be retried
                self.assertEqual(plasma.current, "calm")
                self.assertEqual(os.read(reader, 100), b"brick-status/calm\n")
            finally:
                os.close(reader)


class FakeProm:
    def __init__(self, results):
        self.results = results

    def query(self, promql):
        for key, value in self.results.items():
            if key in promql:
                return value
        return []


class ChecksTest(unittest.TestCase):
    def test_jenkins_results(self):
        prom = FakeProm({
            'up{job="jenkins"}': [({}, 1)],
            "executors_busy": [({}, 1)],
            "last_build_result_ordinal": [({"jenkins_job": "b"}, 2), ({"jenkins_job": "a"}, 0)],
        })
        got = [(c.name, c.state) for c in checks.build_checks(prom)]
        self.assertEqual(got, [("running", "active"), ("a", "ok"), ("b", "fail")])

    def test_jenkins_down(self):
        self.assertEqual(checks.build_checks(FakeProm({}))[0].state, "fail")

    def test_wigle_uploading_and_stale(self):
        now = 1_000_000.0
        prom = FakeProm({
            "kube_job_status_active": [({}, 1)],
            "last_success": [({}, now - 4 * 3600)],
        })
        got = {c.name: c.state for c in checks.wigle_checks(prom, now)}
        self.assertEqual(got["sync"], "active")
        self.assertEqual(got["last success"], "warn")

    def test_vitals_join_names_and_sort_by_number(self):
        prom = FakeProm({
            "node_uname_info": [({"instance": "a:1", "nodename": "brick2000"}, 1),
                                ({"instance": "b:1", "nodename": "brick420"}, 1)],
            "node_cpu_seconds_total": [({"instance": "a:1"}, 7.46), ({"instance": "b:1"}, 3.2)],
        })
        got = checks.cluster_vitals(prom)
        self.assertEqual([n["node"] for n in got], ["brick420", "brick2000"])
        self.assertEqual(got[1]["cpu"], 7.5)

    def test_unreachable_prometheus_is_unknown_not_crash(self):
        class Broken:
            def query(self, promql):
                raise OSError("connection refused")

        summary = checks.summarize(checks.collect(Broken()))
        self.assertEqual(summary["overall"], "unknown")
        self.assertEqual(set(summary["groups"]), {"builds", "cluster", "wigle"})


if __name__ == "__main__":
    unittest.main()
