import itertools
import re
import socket
import struct
import subprocess
import sys
import threading
import time as pytime
import unittest
import urllib.error
import urllib.request
import zlib
from datetime import datetime, time
from pathlib import Path

from brick_status import build, checks, host, lights, notify, patterns, web
from brick_status.board import ACTIVE, IDLE, QUIET, Board, QuietSchedule, in_quiet_hours
from brick_status.config import VIEWS, Settings


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


class QuietScheduleTest(unittest.TestCase):
    WORK = QuietSchedule.parse("00:00-06:00; 2026-10-09: Mon 00:00-Fri 16:00")

    def quiet(self, when):
        return self.WORK.is_quiet(datetime.fromisoformat(when))

    def test_daily_until_the_change(self):
        self.assertTrue(self.quiet("2026-10-01 03:00"))
        self.assertFalse(self.quiet("2026-10-01 12:00"))
        self.assertFalse(self.quiet("2026-10-08 23:59"))

    def test_work_week_from_october_9(self):
        self.assertTrue(self.quiet("2026-10-09 00:00"))   # Friday, the change: off until 16:00
        self.assertTrue(self.quiet("2026-10-09 15:59"))
        self.assertFalse(self.quiet("2026-10-09 16:00"))  # on all weekend, overnight too
        self.assertFalse(self.quiet("2026-10-10 03:00"))
        self.assertFalse(self.quiet("2026-10-11 23:59"))  # through the end of Sunday
        self.assertTrue(self.quiet("2026-10-12 00:00"))   # off from Monday 00:00
        self.assertTrue(self.quiet("2026-10-14 12:00"))
        self.assertFalse(self.quiet("2026-10-16 17:00"))

    def test_window_across_the_end_of_the_week(self):
        weekend = QuietSchedule.parse("Sat 00:00-Mon 06:00")
        self.assertTrue(weekend.is_quiet(datetime(2026, 10, 11, 12)))  # Sunday
        self.assertTrue(weekend.is_quiet(datetime(2026, 10, 12, 5)))   # Monday 05:00
        self.assertFalse(weekend.is_quiet(datetime(2026, 10, 12, 6)))

    def test_none_and_bad_input(self):
        self.assertFalse(QuietSchedule.parse("none").is_quiet(datetime(2026, 10, 1, 3)))
        for bad in ("Mon 00:00-16:00", "Xyz 00:00-Fri 16:00", "midnight-6am", "2026-13-01: 00:00-06:00"):
            with self.assertRaises(ValueError, msg=bad):
                QuietSchedule.parse(bad)


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.board = Board(VIEWS, active_seconds=120, quiet=QuietSchedule.parse("00:00-06:00"), wake_seconds=1800)

    def test_idle_shows_overview(self):
        self.assertEqual(self.board.mode(ts(12)), IDLE)
        self.assertEqual(self.board.view(ts(12)), 0)

    def test_press_activates_view_then_times_out(self):
        self.board.press_view(2, ts(12))
        self.assertEqual(self.board.mode(ts(12, 1)), ACTIVE)
        self.assertEqual(self.board.view(ts(12, 1)), 2)
        self.assertEqual(self.board.mode(ts(12, 3)), IDLE)
        self.assertEqual(self.board.view(ts(12, 3)), 0)

    def test_keys_do_nothing_while_quiet(self):
        self.assertEqual(self.board.mode(ts(3)), QUIET)
        self.board.press_view(1, ts(3))
        self.board.step_view(1, ts(3))
        self.board.wake(ts(3))
        self.assertEqual(self.board.mode(ts(3, 1)), QUIET)

    def test_side_button_wakes_for_30_minutes(self):
        self.assertTrue(self.board.wake_button(ts(3)))
        self.assertEqual(self.board.mode(ts(3, 1)), IDLE)
        self.assertEqual(self.board.awake_until(ts(3, 1)), ts(3, 30))
        self.board.press_view(2, ts(3, 10))  # the other buttons work while it's awake
        self.assertEqual(self.board.view(ts(3, 11)), 2)
        self.assertEqual(self.board.mode(ts(3, 29)), IDLE)
        self.assertEqual(self.board.mode(ts(3, 30)), QUIET)
        self.assertIsNone(self.board.awake_until(ts(3, 30)))

    def test_side_button_does_nothing_while_on(self):
        self.assertFalse(self.board.wake_button(ts(12)))  # already on: daytime
        self.assertIsNone(self.board.awake_until(ts(12)))
        self.assertTrue(self.board.wake_button(ts(3)))
        self.assertFalse(self.board.wake_button(ts(3, 20)))  # already awake: no extension
        self.assertEqual(self.board.mode(ts(3, 30)), QUIET)

    def test_other_key_wakes_without_changing_view(self):
        self.board.press_view(2, ts(11, 50))
        self.board.wake(ts(12))
        self.assertEqual(self.board.mode(ts(12, 1)), ACTIVE)
        self.assertEqual(self.board.view(ts(12, 1)), 2)

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

    def test_busy_button_pulses_green(self):
        frames = lights.active_frames({"wigle": "active"}, VIEWS, [0, 1, 2, 3], selected_view=0)
        greens = {frame[4 * 3] for frame in frames}
        self.assertIn(patterns.GREEN, greens)
        self.assertTrue(all(r == 0 and b == 0 for r, _, b in greens))
        self.assertLess(min(g for _, g, _ in greens), 50)


class BuildLightsTest(unittest.TestCase):
    def pattern(self, status, now=1000.0, alive=True, **kw):
        import json
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "status.json")
            if status is not None:
                with open(path, "w") as f:
                    json.dump(status, f)
            return build.light_pattern(path, now, result_seconds=60, alive=lambda pid: alive, **kw)

    def test_building_shows_rainbow_while_the_build_runs(self):
        status = {"state": "building", "pid": 123, "started": 900}
        self.assertEqual(self.pattern(status), "image-building")
        self.assertIsNone(self.pattern(status, alive=False))  # killed without a result

    def test_deploy_boils_while_it_runs(self):
        status = {"state": "building", "pid": 123, "started": 900}
        self.assertEqual(self.pattern(status, running="deploying"), "deploying")
        done = {**status, "state": "succeeded", "finished": 990}
        self.assertEqual(self.pattern(done, running="deploying"), "image-pushed")

    def test_result_shows_for_a_while_then_stops(self):
        ok = {"state": "succeeded", "pid": 123, "started": 900, "finished": 990}
        self.assertEqual(self.pattern(ok), "image-pushed")
        self.assertIsNone(self.pattern(ok, now=1051))
        failed = {**ok, "state": "failed"}
        self.assertEqual(self.pattern(failed), "image-failed")

    def test_no_or_bad_status_file(self):
        self.assertIsNone(self.pattern(None))
        self.assertIsNone(self.pattern({"state": "unknown"}))

    def test_every_build_pattern_exists(self):
        for name in ("deploying", "image-building", "image-pushed", "image-failed"):
            self.assertIn(name, patterns.BUILD)

    def test_rainbow_and_flash_colours(self):
        hues = {px for frame in patterns.BUILD["image-building"]() for px in frame}
        self.assertTrue(any(r > 200 for r, _, _ in hues) and any(g > 200 for _, g, _ in hues)
                        and any(b > 200 for _, _, b in hues))
        flashes = {px for frame in patterns.BUILD["image-pushed"]() for px in frame}
        self.assertEqual(flashes, {patterns.GREEN, patterns.OFF})

    def test_boil_bubbles_every_slot_on_its_own_beat(self):
        frames = patterns.BUILD["deploying"]()
        self.assertEqual(len(frames), 2 * patterns.FPS)
        firsts = [patterns.LEDS_PER_SLOT * s for s in range(patterns.SLOTS)]
        for i in firsts:
            column = [frame[i] for frame in frames]
            # Swells to full brightness and drops back near dark (the pop).
            self.assertGreater(max(max(px) for px in column), 240)
            self.assertLess(min(max(px) for px in column), 20)
        # Many colours at once, and the slots don't pop in step.
        self.assertGreater(len({frames[60][i] for i in firsts}), 5)
        pops = [min(range(len(frames)), key=lambda f: max(frames[f][i])) for i in firsts]
        self.assertGreater(len(set(pops)), 3)
        # Seeded: the same PNG on every start.
        self.assertEqual(patterns.boil(2.0), frames)


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


class FakeJenkins:
    def __init__(self, jobs):
        self._jobs = jobs

    def jobs(self):
        return self._jobs


class ChecksTest(unittest.TestCase):
    def test_jenkins_jobs_running_and_finished(self):
        now = 10_000.0
        jenkins = FakeJenkins([
            {"name": "wigle-sync-now", "lastBuild": {"building": True, "timestamp": (now - 90) * 1000},
             "lastCompletedBuild": {"result": "SUCCESS", "timestamp": 0}},
            {"name": "prune-images", "lastBuild": {"building": False},
             "lastCompletedBuild": {"result": "FAILURE", "timestamp": (now - 7200) * 1000}},
            {"name": "mirror-repos", "lastBuild": None, "lastCompletedBuild": None},
        ])
        got = [(c.name, c.state, c.detail) for c in checks.build_checks(jenkins, now)]
        self.assertEqual(got, [("mirror-repos", "ok", "not run yet"),
                               ("prune-images", "fail", "failed 2h ago"),
                               ("wigle-sync-now", "active", "running 90s")])

    def test_jenkins_down_is_a_failure(self):
        class Down:
            def jobs(self):
                raise OSError("connection refused")

        [check] = checks.build_checks(Down())
        self.assertEqual((check.name, check.state), ("jenkins", "fail"))

    def test_jenkins_with_no_jobs(self):
        self.assertEqual(checks.build_checks(FakeJenkins([]))[0].detail, "up, no jobs yet")

    @staticmethod
    def wigle_prom(metrics: dict, **extra) -> "FakeProm":
        return FakeProm({
            "kube_job_status_active": extra.get("running", []),
            "min_over_time": extra.get("stuck", []),
            "next_schedule_time": extra.get("next", []),
            "wigle_sync_.+": [({"__name__": f"wigle_sync_{k}"}, v) for k, v in metrics.items()],
        })

    def test_wigle_shows_last_sync_uploads_and_next_sync(self):
        now = datetime(2026, 9, 30, 17, 10).timestamp()
        prom = self.wigle_prom({"last_pi_online_timestamp_seconds": now - 2 * 3600,
                                "last_sync_files_uploaded": 4, "last_sync_files_failed": 0,
                                "files_uploaded": 0, "pi_online": 0},
                               next=[({}, datetime(2026, 9, 30, 18, 0).timestamp())])
        got = [(c.name, c.state, c.detail) for c in checks.wigle_checks(prom, now)]
        self.assertEqual(got, [("last sync", "ok", "OK, 2h ago"),
                               ("files uploaded", "ok", "4"),
                               ("next sync", "ok", "6:00 PM")])

        tomorrow = self.wigle_prom({}, next=[({}, datetime(2026, 10, 1, 0, 0).timestamp())])
        self.assertEqual(checks.wigle_checks(tomorrow, now)[-1].detail, "Thu 12:00 AM")

    def test_wigle_waiting_for_internet_is_not_a_fault(self):
        prom = self.wigle_prom({"last_pi_online_timestamp_seconds": 100, "last_sync_files_uploaded": 0,
                                "last_sync_files_failed": 0, "last_sync_files_deferred": 4})
        last = checks.wigle_checks(prom, 200)[0]
        self.assertEqual((last.state, last.detail), ("ok", "waiting for internet, 4 held"))

    def test_wigle_uploading_shows_active(self):
        prom = self.wigle_prom({"last_pi_online_timestamp_seconds": 100}, running=[({}, 1)])
        self.assertEqual(checks.wigle_checks(prom, 200)[0].state, "active")

    def test_wigle_fails_only_on_errors_persisting_2h(self):
        metrics = {"last_pi_online_timestamp_seconds": 100, "last_sync_files_failed": 3}
        stuck = self.wigle_prom(metrics, stuck=[({}, 1)])
        self.assertEqual(checks.wigle_checks(stuck, 200)[0].state, "fail")

        brief = self.wigle_prom(metrics, stuck=[({}, 0)])
        self.assertEqual([c.state for c in checks.wigle_checks(brief, 200)], ["warn", "ok"])

    def test_image_pulls_while_internet_down_are_degraded_not_down(self):
        prom = FakeProm({
            "waiting_reason": [({"namespace": "kubernetes-dashboard", "pod": "kubernetes-dashboard-566cd-ph8cq",
                                 "reason": "ImagePullBackOff"}, 1),
                               ({"namespace": "chuck", "pod": "reader-68-wrd2h", "reason": "CrashLoopBackOff"}, 1)],
            "replicas_unavailable": [({"namespace": "kubernetes-dashboard", "deployment": "kubernetes-dashboard"}, 1),
                                     ({"namespace": "chuck", "deployment": "reader"}, 1)],
        })
        online = {c.name: c.state for c in checks.cluster_checks(prom, internet=True)}
        self.assertEqual(set(online.values()), {"fail"})

        offline = {c.name: c.state for c in checks.cluster_checks(prom, internet=False)}
        self.assertEqual(offline["kubernetes-dashboard/kubernetes-dashboard"], "warn")
        self.assertEqual(offline["kubernetes-dashboard/kubernetes-dashboard-566cd-ph8cq"], "warn")
        # A crash loop is the cluster's problem whatever the internet is doing.
        self.assertEqual(offline["chuck/reader"], "fail")
        self.assertEqual(offline["chuck/reader-68-wrd2h"], "fail")

    def test_control_plane_from_the_apiserver_scrape(self):
        down = FakeProm({'up{job="apiserver"}': [({"job": "apiserver"}, 0)]})
        [check] = [c for c in checks.cluster_checks(down) if c.name == "control plane"]
        self.assertEqual((check.state, check.detail), ("fail", "API down"))
        # No scrape result at all (not set up yet): no row, rather than a false alarm.
        self.assertFalse([c for c in checks.cluster_checks(FakeProm({})) if c.name == "control plane"])

    def test_finished_job_pods_are_not_scrape_failures(self):
        queries = []

        class Recording(FakeProm):
            def query(self, promql):
                queries.append(promql)
                return super().query(promql)

        checks.cluster_checks(Recording({}))
        [scrape] = [q for q in queries if q.startswith("up == 0")]
        self.assertIn('unless on (namespace, pod) (kube_pod_status_phase{phase=~"Succeeded|Failed"} == 1)', scrape)

    def test_internet_up_if_any_probe_answers(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            live = server.getsockname()
            with socket.socket() as closed:
                closed.bind(("127.0.0.1", 0))
                dead = closed.getsockname()
            self.assertTrue(host.internet_up([dead, live], timeout=1))
            self.assertFalse(host.internet_up([dead], timeout=1))

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

        summary = checks.summarize(checks.collect(Broken(), FakeJenkins([])))
        self.assertEqual(summary["groups"]["cluster"]["state"], "unknown")
        self.assertEqual(summary["groups"]["wigle"]["state"], "unknown")
        self.assertEqual(set(summary["groups"]), {"builds", "cluster", "wigle"})


class MirrorTest(unittest.TestCase):
    """status.brick.nozdormu.cloud reaches the board through the LAN proxy, which adds
    X-Forwarded-For: page and state yes, cmatrix no (it would stop the kiosk's)."""

    def setUp(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        settings = Settings(http_host="127.0.0.1", http_port=port)
        board = Board(VIEWS, active_seconds=120, quiet=QuietSchedule.parse("none"))
        status = type("S", (), {"updated": 0.0, "get": lambda self: checks.summarize([])})()
        threading.Thread(target=web.serve, args=(settings, board, status), daemon=True).start()
        self.base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(self.base + "/api/state", timeout=1)
                break
            except OSError:
                pytime.sleep(0.05)

    def get(self, path, proxied):
        headers = {"X-Forwarded-For": "192.168.1.50"} if proxied else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=headers), timeout=2) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_mirror_gets_page_and_state_but_no_cmatrix(self):
        self.assertEqual(self.get("/", proxied=True), 200)
        self.assertEqual(self.get("/api/state", proxied=True), 200)
        self.assertEqual(self.get("/api/matrix?cols=80&rows=24", proxied=True), 403)


class PixelArtTest(unittest.TestCase):
    def test_committed_svgs_match_the_generator(self):
        """Rerun brick-status/tools/pixel_art.py after editing a drawing."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        import pixel_art
        for path, canvas in pixel_art.drawings().items():
            self.assertEqual(path.read_text(), canvas.svg(), path.name)


    def test_committed_page_pngs_match_the_generator(self):
        """Compared decompressed, so a different zlib build can't fail it."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        import pixel_art

        def pixels(png):
            pos, header, idat = 8, b"", b""
            while pos < len(png):
                n, kind = struct.unpack(">I4s", png[pos:pos + 8])
                data = png[pos + 8:pos + 8 + n]
                if kind == b"IHDR":
                    header = data
                elif kind == b"IDAT":
                    idat += data
                pos += 12 + n
            return header, zlib.decompress(idat)

        for path, canvas in pixel_art.page_art().items():
            self.assertEqual(pixels(path.read_bytes()), pixels(canvas.png()), path.name)
            self.assertTrue((notify.ART / path.name).exists(), path.name)


def cluster_summary(*cluster, wigle=()):
    groups = [checks.Check("cluster", *c) for c in cluster] + [checks.Check("wigle", *c) for c in wigle]
    return checks.summarize(groups)


class FindProblemsTest(unittest.TestCase):
    def test_nodes_wigle_and_disks(self):
        summary = cluster_summary(("control plane", "ok", "API up"), ("brick420", "ok", "Ready"),
                                  ("brick2000", "fail", "NotReady"),
                                  wigle=[("last sync", "fail", "failing for 2h+")])
        vitals = [{"node": "brick420", "disk": 42.0}, {"node": "brick9000", "disk": 91.5}]
        found, seen = notify.find_problems(summary, True, vitals)
        self.assertEqual({p.key for p in found}, {"node:brick2000", "wigle", "disk:brick9000"})
        self.assertEqual(seen, {"unreachable", "control-plane", "node", "wigle",
                                "disk:brick420", "disk:brick9000"})

    def test_unreachable_prometheus_hides_everything_it_feeds(self):
        summary = cluster_summary(("prometheus", "unknown", "unreachable: timed out"),
                                  wigle=[("prometheus", "unknown", "unreachable: timed out")])
        found, seen = notify.find_problems(summary, True, [{"node": "brick9000", "disk": 50.0}])
        self.assertEqual([p.key for p in found], ["unreachable"])
        self.assertEqual(seen, {"unreachable"})

    def test_no_internet_means_no_unreachable_page(self):
        summary = cluster_summary(("prometheus", "unknown", "unreachable"))
        self.assertEqual(notify.find_problems(summary, False, []), ([], set()))

    def test_control_plane_down_leaves_nodes_unknown(self):
        # kube-state-metrics needs the API, so node rows go stale with it.
        summary = cluster_summary(("control plane", "fail", "API down"), ("brick420", "ok", "Ready"))
        found, seen = notify.find_problems(summary, True, [])
        self.assertEqual([p.key for p in found], ["control-plane"])
        self.assertNotIn("node", seen)


class PagerTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir))
        self.holds = {"node": 600, "wigle": 600}

    def pager(self):
        import random
        return notify.Pager(self.dir / "notify.json", self.dir / "incidents.jsonl", self.holds, random.Random(1))

    def incidents(self):
        return [__import__("json").loads(l) for l in (self.dir / "incidents.jsonl").read_text().splitlines()]

    NODE = notify.Problem("node", "brick2000")
    SEEN = {"node", "wigle"}

    def test_a_blip_never_pages_but_is_logged(self):
        p = self.pager()
        p.observe([self.NODE], self.SEEN, 0)
        p.observe([], self.SEEN, 300)
        self.assertEqual(p.take(), [])
        [incident] = self.incidents()
        self.assertEqual((incident["kind"], incident["subject"], incident["paged"]), ("node", "brick2000", False))

    def test_sustained_problem_pages_once_then_clears_once(self):
        p = self.pager()
        for t in (0, 300, 600, 900, 1200):
            p.observe([self.NODE], self.SEEN, t)
        [page] = p.take()
        self.assertEqual(page["title"], "brick2000 is down")
        self.assertIn("10 min", page["message"])
        self.assertEqual((page["priority"], page["art"]), (4, "server-fire"))
        p.sent(page)
        p.observe([], self.SEEN, 1800)
        [clear] = p.take()
        self.assertEqual((clear["title"], clear["priority"]), ("brick2000 is back", 3))
        self.assertIn("30 min", clear["message"])
        self.assertTrue(self.incidents()[0]["paged"])

    def test_restart_neither_repages_nor_forgets(self):
        p = self.pager()
        p.observe([self.NODE], self.SEEN, 0)
        p.observe([self.NODE], self.SEEN, 700)
        p.sent(p.take()[0])
        again = self.pager()                      # brick-status restarted
        again.observe([self.NODE], self.SEEN, 800)
        self.assertEqual(again.take(), [])
        again.observe([], self.SEEN, 900)
        self.assertEqual([m["title"] for m in again.take()], ["brick2000 is back"])

    def test_unsent_pages_survive_a_restart(self):
        p = self.pager()
        p.observe([self.NODE], self.SEEN, 0)
        p.observe([self.NODE], self.SEEN, 700)
        self.assertEqual(len(self.pager().take()), 1)

    def test_a_problem_out_of_sight_is_not_resolved(self):
        p = self.pager()
        p.observe([self.NODE], self.SEEN, 0)
        p.observe([self.NODE], self.SEEN, 700)
        p.sent(p.take()[0])
        p.observe([], {"wigle"}, 800)             # node readiness unknown: still open
        self.assertEqual(p.take(), [])
        self.assertFalse((self.dir / "incidents.jsonl").exists())

    def test_failed_sends_stay_queued(self):
        class Down:
            def send(self, message):
                raise OSError("offline")

        class Up:
            sent = []

            def send(self, message):
                self.sent.append(message["title"])

        p = self.pager()
        p.observe([self.NODE], self.SEEN, 0)
        p.observe([self.NODE], self.SEEN, 700)
        notify.deliver(p, Down())
        self.assertEqual(len(p.take()), 1)
        up = Up()
        notify.deliver(p, up)
        self.assertEqual((up.sent, p.take()), (["brick2000 is down"], []))


class PageWordingTest(unittest.TestCase):
    LEAKS = re.compile(r"\d+\.\d+\.\d+\.\d+|https?://|\.local\b|@")

    def test_every_page_is_bland(self):
        """Pages go through a third-party server: brick names, durations and states only."""
        import random
        for kind in notify.PAGES:
            for cleared in (False, True):
                for seed in range(5):
                    m = notify.page_message(notify.Problem(kind, "brick2000"), 4000, cleared, random.Random(seed))
                    for text in (m["title"], m["message"]):
                        self.assertIsNone(self.LEAKS.search(text), text)
                    self.assertTrue((notify.ART / f"{m['art']}.png").exists(), m["art"])
        self.assertEqual(set(notify.PAGES), set(notify.CLEARS))
        self.assertEqual(set(notify.PAGES), set(notify.HOLDS))

    VITALS = [{"node": "brick420", "cpu": 31.2, "mem": 58.9, "temp": 55.0, "disk": 61.0},
              {"node": "brick2000", "cpu": 12.4, "mem": 40.6, "temp": 50.0, "disk": 87.3},
              {"node": "brick9000", "cpu": 20.0, "mem": 25.0, "temp": 60.0, "disk": 30.0}]
    SUMMARY = {"groups": {
        "cluster": {"checks": [{"name": "brick420", "state": "ok", "detail": "Ready"},
                               {"name": "brick2000", "state": "fail", "detail": "NotReady"},
                               {"name": "chuck/reader", "state": "fail", "detail": "1 unavailable"},
                               {"name": "prometheus", "state": "unknown",
                                "detail": "unreachable: http://192.168.1.183:9090 refused"}]},
        "wigle": {"checks": [{"name": "last sync", "state": "fail", "detail": "failing for 2h+"},
                             {"name": "files uploaded", "state": "ok", "detail": "0"}]}}}

    def facts(self, kind, subject=""):
        return notify.facts(notify.Problem(kind, subject), self.SUMMARY, self.VITALS)

    def test_facts_are_a_few_numbers(self):
        self.assertEqual(self.facts("disk", "brick2000"), "Disk 87% · Mem 41% · CPU 12% · 122°F")
        self.assertEqual(self.facts("node", "brick2000"),
                         "Still answering: brick420 CPU 31% Mem 59% · Workloads short: 1")
        self.assertEqual(self.facts("control-plane"),
                         "Still answering: brick2000 CPU 12% Mem 41%, brick420 CPU 31% Mem 59%")
        self.assertEqual(self.facts("wigle"), "Last sync: failing for 2h+ · Uploaded: 0")
        self.assertEqual(self.facts("unreachable"), "")
        self.assertEqual(self.facts("disk", "brick69"), "")  # no vitals for it

    def test_facts_that_could_leak_are_dropped(self):
        summary = {"groups": {"wigle": {"checks": [
            {"name": "last sync", "state": "fail", "detail": "error from http://192.168.1.183"}]}}}
        self.assertEqual(notify.facts(notify.Problem("wigle"), summary, []), "")

    def test_facts_ride_under_the_page_and_stay_bland(self):
        import random
        for kind in notify.PAGES:
            for cleared in (False, True):
                extra = self.facts(kind, "brick2000")
                m = notify.page_message(notify.Problem(kind, "brick2000"), 4000, cleared, random.Random(1), extra)
                self.assertIsNone(self.LEAKS.search(m["message"]), m["message"])
                if extra:
                    self.assertTrue(m["message"].endswith("\n" + extra))

    def test_test_page_shows_a_facts_line(self):
        m = notify.test_message(self.VITALS[2])
        self.assertEqual(m["message"].split("\n")[1], "brick9000: Disk 30% · Mem 25% · CPU 20% · 140°F")
        self.assertNotIn("\n", notify.test_message()["message"])

    def test_two_line_messages_survive_the_header(self):
        import base64
        encoded = notify._header("Line one\nDisk 87%")
        self.assertTrue(encoded.startswith("=?UTF-8?B?"))
        self.assertEqual(base64.b64decode(encoded[10:-2]).decode(), "Line one\nDisk 87%")
        self.assertEqual(notify._header("plain"), "plain")

    def test_durations(self):
        self.assertEqual([notify.duration(s) for s in (59, 1800, 5400, 12 * 3600, 3 * 86400)],
                         ["0 min", "30 min", "2 h", "12 h", "3 days"])


class NtfyTest(unittest.TestCase):
    """Sends to a local stand-in for ntfy and checks what arrived."""

    def serve(self, refuse_files=False):
        import http.server
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((self.path, dict(self.headers), body))
                code = 413 if refuse_files and "Filename" in self.headers else 200
                self.send_response(code)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}", received

    def test_page_with_art(self):
        url, received = self.serve()
        message = {"title": "brick2000 is down", "message": "Lost brick2000 30 min back.",
                   "tags": "fire,pick", "priority": 4, "art": "server-fire"}
        notify.Ntfy(url, "topic-abc", click="https://status.brick.nozdormu.cloud").send(message)
        [(path, headers, body)] = received
        self.assertEqual(path, "/topic-abc")
        self.assertEqual(body, (notify.ART / "server-fire.png").read_bytes())
        self.assertEqual((headers["Title"], headers["Message"], headers["Filename"], headers["Priority"],
                          headers["Tags"], headers["Click"]),
                         ("brick2000 is down", "Lost brick2000 30 min back.", "server-fire.png", "4",
                          "fire,pick", "https://status.brick.nozdormu.cloud"))

    def test_refused_attachment_falls_back_to_text_and_non_ascii_is_encoded(self):
        url, received = self.serve(refuse_files=True)
        message = {"title": "Ol’ Brick", "message": "café °F", "tags": "pick",
                   "priority": 3, "art": "server-ok"}
        notify.Ntfy(url, "t").send(message)
        self.assertEqual(len(received), 2)
        _, headers, body = received[1]
        self.assertNotIn("Filename", headers)
        self.assertEqual(body.decode(), "café °F")
        self.assertTrue(headers["Title"].startswith("=?UTF-8?B?"))

    def test_topic_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix="topic") as f:
            f.write("abc123\n")
            f.flush()
            self.assertEqual(notify.read_topic(f.name), "abc123")
        self.assertEqual(notify.read_topic("/nonexistent/topic"), "")


JENKINS = Path(__file__).resolve().parents[2] / "brick9000" / "jenkins"


class JenkinsJobsTest(unittest.TestCase):
    """What seed.groovy expects of each job file, and scripts that at least parse.
    deploy runs these before switching brick9000 to a new commit."""

    def test_job_headers(self):
        jobs = sorted((JENKINS / "jobs").glob("*.groovy"))
        self.assertTrue(jobs)
        for job in jobs:
            header = list(itertools.takewhile(lambda l: l.startswith("//"), job.read_text().splitlines()))
            lines = [l[2:].strip() for l in header]
            about = [l for l in lines if not l.startswith(("param ", "cron "))]
            self.assertTrue(about, f"{job.name}: no description comment")
            for line in lines:
                if line.startswith("param "):
                    self.assertRegex(line, r"^param (\w+)=(\S*)\s*(.*)$", job.name)
            self.assertIn("pipeline {", job.read_text(), job.name)
            self.assertRegex(job.stem, r"^[a-z0-9-]+$")

    def test_job_scripts_parse(self):
        scripts = ([p for p in (JENKINS / "bin").iterdir()] + [JENKINS / n for n in ("setup", "up", "reload")]
                   + [JENKINS.parent / n for n in ("build-image", "build-status.sh", "deploy", "install.sh", "proxy/up")])
        for script in scripts:
            result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{script.name}: {result.stderr}")

    def test_jobs_call_scripts_that_exist(self):
        for job in (JENKINS / "jobs").glob("*.groovy"):
            for path in re.findall(r"/brick-cicd-config/(brick9000/jenkins/bin/[\w-]+)", job.read_text()):
                self.assertTrue((JENKINS.parents[1] / path).is_file(), f"{job.name}: {path} missing")


if __name__ == "__main__":
    unittest.main()
