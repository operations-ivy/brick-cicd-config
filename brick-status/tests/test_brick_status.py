import itertools
import re
import socket
import subprocess
import struct
import unittest
import zlib
from datetime import datetime, time
from pathlib import Path

from brick_status import build, checks, host, lights, patterns
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

    def test_busy_button_pulses_green(self):
        frames = lights.active_frames({"wigle": "active"}, VIEWS, [0, 1, 2, 3], selected_view=0)
        greens = {frame[4 * 3] for frame in frames}
        self.assertIn(patterns.GREEN, greens)
        self.assertTrue(all(r == 0 and b == 0 for r, _, b in greens))
        self.assertLess(min(g for _, g, _ in greens), 50)


class BuildLightsTest(unittest.TestCase):
    def pattern(self, status, now=1000.0, alive=True):
        import json
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "status.json")
            if status is not None:
                with open(path, "w") as f:
                    json.dump(status, f)
            return build.light_pattern(path, now, result_seconds=60, alive=lambda pid: alive)

    def test_building_shows_rainbow_while_the_build_runs(self):
        status = {"state": "building", "pid": 123, "started": 900}
        self.assertEqual(self.pattern(status), "image-building")
        self.assertIsNone(self.pattern(status, alive=False))  # killed without a result

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
        for name in ("image-building", "image-pushed", "image-failed"):
            self.assertIn(name, patterns.BUILD)

    def test_rainbow_and_flash_colours(self):
        hues = {px for frame in patterns.BUILD["image-building"]() for px in frame}
        self.assertTrue(any(r > 200 for r, _, _ in hues) and any(g > 200 for _, g, _ in hues)
                        and any(b > 200 for _, _, b in hues))
        flashes = {px for frame in patterns.BUILD["image-pushed"]() for px in frame}
        self.assertEqual(flashes, {patterns.GREEN, patterns.OFF})


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
                   + [JENKINS.parent / n for n in ("build-image", "build-status.sh", "deploy", "install.sh")])
        for script in scripts:
            result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{script.name}: {result.stderr}")

    def test_jobs_call_scripts_that_exist(self):
        for job in (JENKINS / "jobs").glob("*.groovy"):
            for path in re.findall(r"/brick-cicd-config/(brick9000/jenkins/bin/[\w-]+)", job.read_text()):
                self.assertTrue((JENKINS.parents[1] / path).is_file(), f"{job.name}: {path} missing")


if __name__ == "__main__":
    unittest.main()
