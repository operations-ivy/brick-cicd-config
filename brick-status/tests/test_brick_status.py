import io
import itertools
import logging
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time as pytime
import unittest
import wave
import json
import urllib.error
import urllib.request
import zlib
from datetime import datetime, time
from pathlib import Path
from unittest import mock

import structlog
from structlog.testing import capture_logs

from brick_status import (board_metrics, jobs, checks, logs, crt_activity, crt_gallery, host, lights, matrix, metrics, notify,
                          party, party_sound, units, volume, web)
from brick_status.__main__ import button_name
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
    WORK = QuietSchedule.parse("00:00-06:00; 2026-10-09: Mon 00:01-Mon 16:00, Tue 00:00-Tue 16:00, "
                               "Wed 00:00-Wed 16:00, Thu 00:00-Thu 16:00, Fri 00:00-Fri 16:00")

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
        self.assertFalse(self.quiet("2026-10-12 00:00"))  # and Monday's first minute
        self.assertTrue(self.quiet("2026-10-12 00:01"))   # off from Monday 00:01
        self.assertTrue(self.quiet("2026-10-12 15:59"))
        self.assertFalse(self.quiet("2026-10-12 16:00"))  # weekday evenings on, 16:00 to midnight
        self.assertFalse(self.quiet("2026-10-12 23:59"))
        self.assertTrue(self.quiet("2026-10-13 00:00"))   # off again from Tuesday 00:00
        self.assertTrue(self.quiet("2026-10-14 12:00"))
        self.assertFalse(self.quiet("2026-10-14 20:00"))
        self.assertTrue(self.quiet("2026-10-16 09:00"))   # Friday morning off
        self.assertFalse(self.quiet("2026-10-16 17:00"))

    def test_window_across_the_end_of_the_week(self):
        weekend = QuietSchedule.parse("Sat 00:00-Mon 06:00")
        self.assertTrue(weekend.is_quiet(datetime(2026, 10, 11, 12)))  # Sunday
        self.assertTrue(weekend.is_quiet(datetime(2026, 10, 12, 5)))   # Monday 05:00
        self.assertFalse(weekend.is_quiet(datetime(2026, 10, 12, 6)))

    def test_several_windows_in_one_entry(self):
        s = QuietSchedule.parse("Mon 01:00-Mon 02:00, 03:00-04:00, none")
        self.assertTrue(s.is_quiet(datetime(2026, 10, 12, 1, 30)))   # Monday window
        self.assertFalse(s.is_quiet(datetime(2026, 10, 13, 1, 30)))  # not on Tuesday
        self.assertTrue(s.is_quiet(datetime(2026, 10, 13, 3, 30)))   # daily window
        self.assertFalse(s.is_quiet(datetime(2026, 10, 13, 5, 0)))

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

    def test_the_crt_button_toggles_and_only_it_undoes_it(self):
        self.assertFalse(self.board.crt_full)
        self.assertTrue(self.board.toggle_crt(ts(12)))
        self.assertEqual(self.board.mode(ts(12, 1)), ACTIVE)  # a press, like any button
        # Idle puts the overview back; the CRT stays full screen until pressed again.
        self.assertEqual(self.board.mode(ts(12, 30)), IDLE)
        self.assertTrue(self.board.crt_full)
        self.board.press_view(2, ts(13))
        self.assertTrue(self.board.crt_full)
        self.assertFalse(self.board.toggle_crt(ts(13, 1)))
        self.assertFalse(self.board.crt_full)

    def test_the_crt_button_does_nothing_while_quiet(self):
        self.assertIsNone(self.board.toggle_crt(ts(3)))
        self.assertFalse(self.board.crt_full)

    def test_side_button_wakes_for_30_minutes(self):
        self.assertEqual(self.board.side_button(ts(3)), "awake")
        self.assertEqual(self.board.mode(ts(3, 1)), IDLE)
        self.assertEqual(self.board.awake_until(ts(3, 1)), ts(3, 30))
        self.board.press_view(2, ts(3, 10))  # the other buttons work while it's awake
        self.assertEqual(self.board.view(ts(3, 11)), 2)
        self.assertEqual(self.board.mode(ts(3, 29)), IDLE)
        self.assertEqual(self.board.mode(ts(3, 30)), QUIET)
        self.assertIsNone(self.board.awake_until(ts(3, 30)))

    def test_an_open_game_keeps_it_on_in_quiet_hours(self):
        self.board.keep_awake(ts(3), 60)  # brick-status refreshes this every second
        self.assertEqual(self.board.mode(ts(3, 0) + 59), IDLE)
        self.assertEqual(self.board.mode(ts(3, 0) + 60), QUIET)
        self.board.side_button(ts(3, 5))
        self.board.keep_awake(ts(3, 6), 60)  # never shortens a side-button wake
        self.assertEqual(self.board.awake_until(ts(3, 6)), ts(3, 35))

    def test_other_key_wakes_without_changing_view(self):
        self.board.press_view(2, ts(11, 50))
        self.board.wake(ts(12))
        self.assertEqual(self.board.mode(ts(12, 1)), ACTIVE)
        self.assertEqual(self.board.view(ts(12, 1)), 2)

    def test_step_wraps(self):
        self.board.step_view(-1, ts(12))
        self.assertEqual(self.board.view(ts(12)), len(VIEWS) - 1)

    # ts() is Saturday 2026-09-26; the schedule is quiet 00:00-06:00 daily.
    def day(self, day, hour, minute=0):
        return datetime(2026, 9, day, hour, minute).timestamp()

    def test_side_button_sleeps_until_the_schedule_turns_it_on(self):
        self.assertEqual(self.board.side_button(ts(18)), "asleep")
        self.assertEqual(self.board.mode(ts(18, 1)), QUIET)
        self.assertEqual(self.board.asleep_until(ts(18, 1)), self.day(27, 6))
        self.assertEqual(self.board.mode(self.day(27, 5, 59)), QUIET)
        self.assertEqual(self.board.mode(self.day(27, 6)), IDLE)

    def test_a_sleep_with_no_quiet_window_coming_ends_in_the_morning(self):
        board = Board(VIEWS, 120, QuietSchedule.parse("Mon 00:00-Mon 16:00"), 1800, time(8, 0))
        board.side_button(ts(18))  # Saturday evening: nothing is quiet until Monday
        self.assertEqual(board.asleep_until(ts(18, 1)), self.day(27, 8))
        self.assertEqual(board.mode(self.day(27, 8)), IDLE)

    def test_the_users_weekday_sleep(self):
        work = QuietSchedule.parse("00:00-06:00; 2026-10-09: Mon 00:01-Mon 16:00, Tue 00:00-Tue 16:00, "
                                   "Wed 00:00-Wed 16:00, Thu 00:00-Thu 16:00, Fri 00:00-Fri 16:00")
        board = Board(VIEWS, 120, work, 1800, time(8, 0))
        monday = lambda d, h, m=0: datetime(2026, 10, d, h, m).timestamp()
        board.side_button(monday(12, 18))  # 6 PM Monday: off until 4 PM Tuesday
        self.assertEqual(board.mode(monday(13, 12)), QUIET)
        self.assertEqual(board.mode(monday(13, 15, 59)), QUIET)
        self.assertEqual(board.mode(monday(13, 16)), IDLE)

    def test_side_button_puts_a_wake_back_to_sleep(self):
        self.board.side_button(ts(3))  # woken in quiet hours
        self.assertEqual(self.board.side_button(ts(3, 5)), "asleep")
        self.assertEqual(self.board.mode(ts(3, 6)), QUIET)
        self.assertEqual(self.board.mode(ts(6)), IDLE)  # the schedule's on time

    def test_waking_a_sleep_in_the_on_hours_stays_on(self):
        self.board.side_button(ts(18))
        self.assertEqual(self.board.side_button(ts(20)), "on")
        self.assertIsNone(self.board.asleep_until(ts(20)))
        self.assertEqual(self.board.mode(ts(21)), IDLE)  # past wake_seconds: still on
        self.assertEqual(self.board.mode(self.day(27, 0, 1)), QUIET)  # the schedule's off time

    def test_waking_a_sleep_in_quiet_hours_is_a_30_minute_wake(self):
        self.board.side_button(ts(18))
        self.assertEqual(self.board.side_button(self.day(27, 2)), "awake")
        self.assertEqual(self.board.mode(self.day(27, 2, 29)), IDLE)
        self.assertEqual(self.board.mode(self.day(27, 2, 30)), QUIET)

    def test_sleep_ends_an_active_view_and_ignores_keys(self):
        self.board.press_view(2, ts(12))
        self.board.side_button(ts(12, 0) + 1)
        self.assertEqual(self.board.mode(ts(12, 0) + 2), QUIET)
        self.board.press_view(1, ts(12, 1))
        self.board.step_view(1, ts(12, 1))
        self.board.wake(ts(12, 1))
        self.assertEqual(self.board.mode(ts(12, 1) + 1), QUIET)

    def test_a_sleep_survives_a_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = f"{d}/state/board.json"
            Board(VIEWS, 120, QuietSchedule.parse("00:00-06:00"), state_path=path).side_button(ts(18))
            again = Board(VIEWS, 120, QuietSchedule.parse("00:00-06:00"), state_path=path)
            self.assertEqual(again.mode(ts(19)), QUIET)
            self.assertEqual(again.side_button(ts(19)), "on")
            self.assertEqual(Board(VIEWS, 120, QuietSchedule.parse("00:00-06:00"),
                                   state_path=path).mode(ts(19, 1)), IDLE)

    def test_unreadable_state_is_ignored(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            Path(d, "board.json").write_text("not json")
            board = Board(VIEWS, 120, QuietSchedule.parse("00:00-06:00"), state_path=f"{d}/board.json")
            self.assertEqual(board.mode(ts(12)), IDLE)


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


class FakeClock:
    now = 1000.0

    def __call__(self):
        return self.now


class PlasmaTest(unittest.TestCase):
    """The board as a client of the plasma API, against a real HTTP server."""

    def setUp(self):
        import http.server
        import threading

        calls = self.calls = []
        self.status = 200
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, body))
                self.send_response(test.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.clock = FakeClock()
        self.plasma = lights.Plasma(f"http://127.0.0.1:{self.server.server_port}", clock=self.clock)

    def test_a_pattern_is_sent_once_then_repeated_as_a_heartbeat(self):
        self.plasma.show("calm")
        self.plasma.show("calm")
        self.assertEqual(self.calls, [("/signals/board", {"pattern": "calm", "ttl": 30.0})])
        self.clock.now += lights.Plasma.HEARTBEAT
        self.plasma.show("calm")
        self.assertEqual(len(self.calls), 2)
        self.assertLess(lights.Plasma.HEARTBEAT * 2, lights.Plasma.TTL)

    def test_a_change_goes_out_at_once(self):
        self.plasma.show("calm")
        self.plasma.off()
        self.assertEqual(self.calls[-1][1]["pattern"], "off")

    def test_a_refused_signal_is_retried(self):
        self.status = 500
        self.plasma.show("calm")
        self.status = 200
        self.plasma.show("calm")
        self.assertEqual(len(self.calls), 2)

    def test_the_api_being_down_is_survived(self):
        plasma = lights.Plasma("http://127.0.0.1:9", clock=self.clock)
        with capture_logs() as captured:
            plasma.show("calm")
            plasma.show("calm")
        self.assertEqual([e["log_level"] for e in captured], ["warning"])  # once, not every second
        self.assertIsNone(plasma.sent)

    def test_buttons_light_each_view_in_its_health_colour(self):
        self.plasma.buttons({"builds": "fail", "wigle": "active"}, VIEWS, [2, 1, 0, 3], selected_view=1)
        path, body = self.calls[0]
        self.assertEqual(body["priority"], lights.PRIORITY_ACTIVE)
        buttons = body["buttons"]
        self.assertEqual(buttons["selected"], 1)
        self.assertEqual(buttons["colors"]["1"], lights.RED)  # builds, the second view
        self.assertEqual(buttons["colors"]["2"], lights.GREEN)  # the overview is well by default
        self.assertEqual(buttons["busy"], [slot for slot, view in zip([2, 1, 0, 3], VIEWS) if view == "wigle"])


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

    def test_a_poller_at_work_shows_its_last_result_not_a_build(self):
        now = 10_000.0
        jenkins = FakeJenkins([
            {"name": "build-branches", "lastBuild": {"building": True, "timestamp": (now - 1) * 1000},
             "lastCompletedBuild": {"result": "SUCCESS", "timestamp": (now - 120) * 1000}},
        ])
        [check] = checks.build_checks(jenkins, now)
        self.assertEqual((check.name, check.state), ("build-branches", "ok"))

    def test_jenkins_unreachable_is_no_data(self):
        # Jenkins is on the cluster: when it can't be reached, the cluster view
        # says why, and the builds themselves haven't failed.
        class Down:
            def jobs(self):
                raise OSError("connection refused")

        [check] = checks.build_checks(Down())
        self.assertEqual((check.name, check.state), ("jenkins", "unknown"))
        self.assertIn("unreachable", check.detail)

    def test_passing_builds_on_the_cluster(self):
        now = 10_000.0
        jenkins = FakeJenkins([
            {"name": "mirror-repos", "lastBuild": {"building": False},
             "lastCompletedBuild": {"result": "SUCCESS", "timestamp": (now - 300) * 1000}},
        ])
        summary = checks.summarize(checks.build_checks(jenkins, now))
        self.assertEqual(summary["groups"]["builds"]["state"], "ok")
        self.assertEqual(summary["groups"]["builds"]["checks"][0]["detail"], "passed 5m ago")

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

    def argo(self, apps, stuck=(), syncs=()):
        # FakeProm takes the first key in the query: the stuck query has "offset"
        # and also names argocd_app_info, so it goes first.
        info = [({"name": n, "sync_status": sync, "health_status": health}, 1.0) for n, sync, health in apps]
        return checks.argocd_checks(FakeProm({
            "offset": [({"name": n}, 1.0) for n in stuck],
            "argocd_app_sync_total": [({"name": n, "phase": phase}, ts) for n, phase, ts in syncs],
            "argocd_app_info": info,
        }), now=ts(12))

    def test_argocd_all_in_sync_is_a_summary_and_the_last_syncs(self):
        got = self.argo([("jenkins", "Synced", "Healthy"), ("secrets", "Synced", "Healthy")],
                        syncs=[("jenkins", "Succeeded", ts(11, 40)), ("secrets", "Succeeded", ts(11, 57))])
        self.assertEqual([(c.name, c.state, c.detail) for c in got], [
            ("apps", "ok", "2/2 synced and healthy"),
            ("synced secrets", "ok", "succeeded, 3m ago"),
            ("synced jenkins", "ok", "succeeded, 20m ago")])
        self.assertTrue(all(c.group == "argocd" for c in got))

    def test_argocd_out_of_sync_is_syncing_until_its_stuck(self):
        got = {c.name: c for c in self.argo([("a", "OutOfSync", "Healthy"), ("b", "OutOfSync", "Healthy"),
                                              ("c", "Synced", "Healthy")], stuck=["b"])}
        self.assertEqual(got["a"].state, "active")
        self.assertEqual(got["b"].state, "fail")
        self.assertIn("for 15m+", got["b"].detail)
        self.assertNotIn("c", got)  # healthy apps aren't listed one by one
        self.assertEqual((got["apps"].state, got["apps"].detail), ("fail", "1/3 synced and healthy"))

    def test_argocd_health_and_a_failed_sync(self):
        got = {c.name: c for c in self.argo(
            [("down", "Synced", "Degraded"), ("rolling", "Synced", "Progressing"), ("oops", "Synced", "Healthy")],
            syncs=[("oops", "Succeeded", ts(10)), ("oops", "Failed", ts(11))])}
        self.assertEqual(got["down"].state, "fail")
        self.assertEqual(got["rolling"].state, "active")
        self.assertEqual(got["oops"].state, "warn")
        self.assertIn("last sync failed 60m ago", got["oops"].detail)
        self.assertEqual(got["synced oops"].state, "warn")  # the newest result per app

    def test_argocd_without_metrics_is_unknown(self):
        got = self.argo([])
        self.assertEqual([(c.name, c.state) for c in got], [("apps", "unknown")])

    def test_argocd_shows_only_the_newest_syncs(self):
        apps = [(f"app{i}", "Synced", "Healthy") for i in range(6)]
        got = self.argo(apps, syncs=[(f"app{i}", "Succeeded", ts(11, i)) for i in range(6)])
        self.assertEqual([c.name for c in got[1:]], ["synced app5", "synced app4", "synced app3", "synced app2"])

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

    def test_unhealthy_workloads_are_degraded_not_down(self):
        prom = FakeProm({
            "waiting_reason": [({"namespace": "kubernetes-dashboard", "pod": "kubernetes-dashboard-566cd-ph8cq",
                                 "reason": "ImagePullBackOff"}, 1),
                               ({"namespace": "chuck", "pod": "reader-68-wrd2h", "reason": "CrashLoopBackOff"}, 1)],
            "replicas_unavailable": [({"namespace": "kubernetes-dashboard", "deployment": "kubernetes-dashboard"}, 1),
                                     ({"namespace": "chuck", "deployment": "reader"}, 1)],
        })
        online = checks.cluster_checks(prom, internet=True)
        self.assertEqual({c.state for c in online}, {"warn"})
        self.assertEqual(checks.summarize(online)["groups"]["cluster"]["state"], "warn")

        offline = {c.name: c.detail for c in checks.cluster_checks(prom, internet=False)}
        self.assertEqual(offline["kubernetes-dashboard/kubernetes-dashboard"], "1 not ready, internet down")
        self.assertEqual(offline["kubernetes-dashboard/kubernetes-dashboard-566cd-ph8cq"],
                         "ImagePullBackOff, internet down")
        # A crash loop isn't the internet's fault, so it doesn't say so.
        self.assertEqual(offline["chuck/reader"], "1 not ready")
        self.assertEqual(offline["chuck/reader-68-wrd2h"], "CrashLoopBackOff")

    def test_node_not_ready_is_down(self):
        prom = FakeProm({"kube_node_status_condition": [({"node": "brick666"}, 0)]})
        [node] = checks.cluster_checks(prom)[:1]
        self.assertEqual((node.name, node.state, node.detail), ("brick666", "fail", "NotReady"))

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

    def test_vitals_only_ask_the_clusters_node_exporter(self):
        """brick9000 reads its own; brick69 is scraped too, but isn't a node."""
        asked = []
        prom = type("P", (), {"query": lambda self, q: asked.append(q) or []})()
        checks.cluster_vitals(prom)
        self.assertTrue(asked)
        for q in asked:
            self.assertEqual(q.count('job="node-exporter"'), q.count("node_"), q)

    def test_unreachable_prometheus_is_unknown_not_crash(self):
        class Broken:
            def query(self, promql):
                raise OSError("connection refused")

        summary = checks.summarize(checks.collect(Broken(), FakeJenkins([])))
        self.assertEqual(summary["groups"]["cluster"]["state"], "unknown")
        self.assertEqual(summary["groups"]["wigle"]["state"], "unknown")
        self.assertEqual(summary["groups"]["argocd"]["state"], "unknown")
        self.assertEqual(set(summary["groups"]), {"builds", "cluster", "wigle", "argocd"})


class MirrorTest(unittest.TestCase):
    """status.brick.nozdormu.cloud reaches the board through the LAN proxy, which adds
    X-Forwarded-For: page and state yes, cmatrix no (it would stop the kiosk's)."""

    def setUp(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        settings = Settings(http_host="127.0.0.1", http_port=port)
        self.board = Board(VIEWS, active_seconds=120, quiet=QuietSchedule.parse("none"))
        status = type("S", (), {"updated": 0.0, "get": lambda self: checks.summarize([])})()
        threading.Thread(target=web.serve, args=(settings, self.board, status), daemon=True).start()
        self.base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(self.base + "/api/state", timeout=1)
                break
            except OSError:
                pytime.sleep(0.05)

    def test_state_says_whether_the_crt_is_full_screen(self):
        def crt_full():
            with urllib.request.urlopen(self.base + "/api/state", timeout=2) as r:
                return json.load(r)["crt_full"]
        self.assertFalse(crt_full())
        self.board.toggle_crt(pytime.time())
        self.assertTrue(crt_full())

    def get(self, path, proxied):
        headers = {"X-Forwarded-For": "192.168.1.50"} if proxied else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=headers), timeout=2) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_only_a_loopback_client_without_the_proxy_is_the_kiosk(self):
        proxied = {"X-Forwarded-For": "192.168.1.50"}
        self.assertTrue(web.is_local("127.0.0.1", {}))
        self.assertFalse(web.is_local("127.0.0.1", proxied))
        self.assertFalse(web.is_local("192.168.1.50", {}))   # a LAN client straight to the port
        self.assertFalse(web.is_local("", {}))
        self.assertFalse(web.can_set_volume(False, {"Host": "192.168.1.224:8765"}))
        self.assertTrue(web.can_set_volume(False, {"Host": "volume.brick.nozdormu.cloud"}))

    def test_mirror_gets_page_and_state_but_no_cmatrix(self):
        self.assertEqual(self.get("/", proxied=True), 200)
        self.assertEqual(self.get("/api/state", proxied=True), 200)
        self.assertEqual(self.get("/api/matrix?cols=80&rows=24", proxied=True), 403)

    def test_metrics_endpoint(self):
        self.get("/api/state", proxied=False)
        with urllib.request.urlopen(self.base + "/metrics", timeout=2) as r:
            self.assertTrue(r.headers["Content-Type"].startswith("text/plain; version=0.0.4"))
            text = r.read().decode()
        self.assertIn("# TYPE brick_status_polls_total counter", text)
        self.assertRegex(text, r'brick_status_http_requests_total\{path="/api/state",code="200"\} [1-9]')


class VolumeTest(unittest.TestCase):
    """The volume page's API, against a stand-in wpctl that keeps its state in a file."""

    def setUp(self):
        import json as _json
        import tempfile
        import shutil
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = self.dir / "state.json"
        self.state.write_text(_json.dumps({"volume": 0.25, "muted": False}))
        self.calls = self.dir / "calls"
        fake = self.dir / "wpctl"
        fake.write_text(f"""#!/usr/bin/env python3
import json, sys
state = json.load(open({str(self.state)!r}))
open({str(self.calls)!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[0] == "get-volume":
    print(f"Volume: {{state['volume']:.2f}}" + (" [MUTED]" if state["muted"] else ""))
elif args[0] == "set-volume":
    limit = float(args[args.index("-l") + 1])
    state["volume"] = min(limit, float(args[-1]))
elif args[0] == "set-mute":
    state["muted"] = args[-1] == "1"
json.dump(state, open({str(self.state)!r}, "w"))
""")
        fake.chmod(0o755)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        settings = Settings(http_host="127.0.0.1", http_port=port, wpctl=str(fake), max_volume=0.5)
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

    def request(self, path, body=None, host=None, proxied=False):
        import json as _json
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if proxied:
            headers["X-Forwarded-For"] = "192.168.1.50"
        if host:
            headers["Host"] = host
        data = (body if isinstance(body, bytes) else _json.dumps(body).encode()) if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_parse(self):
        self.assertEqual(volume.parse("Volume: 0.25\n"), (0.25, False))
        self.assertEqual(volume.parse("Volume: 0.40 [MUTED]"), (0.40, True))
        with self.assertRaises(ValueError):
            volume.parse("Failed to connect to PipeWire")

    def test_get_is_a_fraction_of_the_cap(self):
        import json as _json
        code, body = self.request("/api/volume")
        self.assertEqual(code, 200)
        self.assertEqual(_json.loads(body), {"level": 0.5, "muted": False, "max": 0.5, "can_set": True})

    def test_set_level_maps_onto_the_cap_and_is_clamped(self):
        import json as _json
        code, body = self.request("/api/volume", {"level": 0.8}, host=web.VOLUME_HOST, proxied=True)
        self.assertEqual(code, 200)
        self.assertEqual(_json.loads(body)["level"], 0.8)
        self.assertIn("set-volume -l 0.5 @DEFAULT_AUDIO_SINK@ 0.400", self.calls.read_text())
        code, body = self.request("/api/volume", {"level": 7}, host=web.VOLUME_HOST, proxied=True)
        self.assertEqual(_json.loads(body)["level"], 1.0)
        self.assertEqual(_json.loads(self.state.read_text())["volume"], 0.5)  # never past the cap

    def test_mute_and_unmute(self):
        import json as _json
        _, body = self.request("/api/volume", {"muted": True}, host=web.VOLUME_HOST, proxied=True)
        self.assertTrue(_json.loads(body)["muted"])
        _, body = self.request("/api/volume", {"muted": False, "level": 0.2}, host=web.VOLUME_HOST, proxied=True)
        self.assertEqual(_json.loads(body)["muted"], False)

    def test_the_status_mirror_stays_read_only(self):
        import json as _json
        code, body = self.request("/api/volume", {"level": 1}, host="status.brick.nozdormu.cloud", proxied=True)
        self.assertEqual(code, 403)
        self.assertEqual(_json.loads(self.state.read_text())["volume"], 0.25)
        code, body = self.request("/api/volume", host="status.brick.nozdormu.cloud", proxied=True)
        self.assertEqual(_json.loads(body)["can_set"], False)

    def test_bad_bodies_are_400(self):
        for body in (b"not json", b"[1]", {"level": "loud"}, {"level": float("nan")}):
            code, _ = self.request("/api/volume", body, host=web.VOLUME_HOST, proxied=True)
            self.assertEqual(code, 400, body)
        self.assertFalse(self.calls.exists())  # none of them reached wpctl

    def test_pipewire_down_is_503(self):
        self.state.write_text("broken")
        code, _ = self.request("/api/volume")
        self.assertEqual(code, 503)

    def test_page_is_served(self):
        code, body = self.request("/volume", host=web.VOLUME_HOST, proxied=True)
        self.assertEqual(code, 200)
        self.assertIn(b'type="range"', body)


class MetricsTest(unittest.TestCase):
    def test_render_text_format(self):
        r = metrics.Registry()
        c = r.counter("x_total", "Things.", ["kind"])
        g = r.gauge("y", "A level.")
        c.inc(kind='a"b')
        c.inc(2, kind='a"b')
        g.set(0.5)
        self.assertEqual(r.render(), "\n".join([
            "# HELP x_total Things.", "# TYPE x_total counter", 'x_total{kind="a\\"b"} 3',
            "# HELP y A level.", "# TYPE y gauge", "y 0.5"]) + "\n")

    def test_labels_must_match(self):
        g = metrics.Registry().gauge("z", "Z.", ["a"])
        with self.assertRaises(ValueError):
            g.set(1, b="x")

    def test_replace_drops_old_series(self):
        r = metrics.Registry()
        g = r.gauge("z", "Z.", ["a"])
        g.set(1, a="old")
        g.replace({("new",): 2})
        self.assertNotIn("old", r.render())
        self.assertIn('z{a="new"} 2', r.render())

    def test_write_textfile_replaces_in_one_step(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = f"{d}/x.prom"
            metrics.write_textfile(path, "a 1\n")
            metrics.write_textfile(path, "a 2\n")
            self.assertEqual(Path(path).read_text(), "a 2\n")
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["x.prom"])

    def test_summary_states_as_numbers(self):
        summary = checks.summarize([checks.Check("cluster", "api", "ok"),
                                    checks.Check("cluster", "pods", "warn"),
                                    checks.Check("wigle", "sync", "fail")])
        board_metrics.record_summary(summary)
        text = board_metrics.REGISTRY.render()
        self.assertIn("brick_status_overall_state 4", text)
        self.assertIn('brick_status_group_state{group="cluster"} 3', text)
        self.assertIn('brick_status_check_state{group="cluster",check="api"} 0', text)

    def test_button_names(self):
        from brick_status.config import BUTTON_KEYS, KEY_LEFT, KEY_WAKE
        # The sixth is the check button (since 2026-11-08), named for that.
        self.assertEqual([button_name(k) for k in BUTTON_KEYS],
                         ["overview", "builds", "cluster", "anomalies", "crt", "check"])
        self.assertEqual(button_name(KEY_WAKE), "side")
        from brick_status.config import KEY_CHECK
        self.assertEqual(button_name(KEY_CHECK), "check")
        self.assertEqual(button_name(KEY_LEFT), "left")
        from brick_status.config import KEY_LAMP
        self.assertEqual(button_name(KEY_LAMP), "lamp")
        from brick_status.config import KEY_PARTY
        self.assertEqual(button_name(KEY_PARTY), "party")
        self.assertEqual(button_name(999), "other")

    def test_the_check_and_party_buttons(self):
        from brick_status.config import BUTTON_KEYS, KEY_CHECK, KEY_LAMP, KEY_PARTY, KEY_WAKE
        self.assertEqual(KEY_CHECK, BUTTON_KEYS[5])  # the bottom row's right-hand button
        self.assertEqual(KEY_PARTY, 24)  # 1UP, KEY_O
        self.assertEqual(len({KEY_CHECK, KEY_LAMP, KEY_PARTY, KEY_WAKE}), 4)

    def test_http_paths_are_bounded(self):
        self.assertEqual(board_metrics.http_path("/static/vendor/xterm.js"), "/static/")
        self.assertEqual(board_metrics.http_path("/wp-login.php"), "other")
        self.assertEqual(board_metrics.http_path("/metrics"), "/metrics")

class FakeHA:
    """Home Assistant's light calls, recorded; state() answers with `state`."""

    def __init__(self, state):
        self._state, self.calls = state, []

    def state(self, entity):
        return self._state

    def call(self, service, entity, **data):
        self.calls.append((service, data))
        return True, "on"


class PartyTest(unittest.TestCase):
    def test_twenty_seconds_then_done(self):
        self.assertEqual(party.SECONDS, 20.0)  # the user, 2026-11-08
        clock = FakeClock()
        show = party.Show(clock=clock)
        self.assertFalse(show.on())
        self.assertTrue(show.start())
        clock.now += party.SECONDS - 0.1
        self.assertTrue(show.on())
        clock.now += 0.2
        self.assertFalse(show.on())
        self.assertEqual(show.remaining(), 0.0)

    def test_a_press_during_a_show_starts_it_again(self):
        clock = FakeClock()
        show = party.Show(clock=clock)
        self.assertTrue(show.start())
        clock.now += 6
        self.assertFalse(show.start())  # not a new show: no second lamp party
        self.assertAlmostEqual(show.remaining(), party.SECONDS)
        clock.now += party.SECONDS + 0.1
        self.assertFalse(show.on())

    def test_the_lamp_goes_back_as_it_was(self):
        cases = [
            ({"state": "off", "attributes": {}}, ("turn_off", {"transition": 1})),
            ({"state": "on", "attributes": {"brightness": 140, "color_mode": "color_temp", "color_temp_kelvin": 2700}},
             ("turn_on", {"transition": 1, "brightness": 140, "color_temp_kelvin": 2700})),
            ({"state": "on", "attributes": {"brightness": 178, "color_mode": "xy", "xy_color": [0.17, 0.7]}},
             ("turn_on", {"transition": 1, "brightness": 178, "xy_color": [0.17, 0.7]})),
            ({"state": "on", "attributes": {"brightness": 13, "color_mode": "hs", "hs_color": [30, 80]}},
             ("turn_on", {"transition": 1, "brightness": 13, "hs_color": [30, 80]})),
            ({"state": "unavailable", "attributes": {}}, ("turn_off", {"transition": 1})),
        ]
        for state, wanted in cases:
            self.assertEqual(party.restore_call(state), wanted, state)

    def test_colours_for_a_colour_lamp_a_flicker_for_a_white_one(self):
        colour = {"attributes": {"supported_color_modes": ["color_temp", "xy"]}}
        white = {"attributes": {"supported_color_modes": ["color_temp"]}}
        self.assertEqual(party.party_call(colour, 0)["rgb_color"], party.COLOURS[0])
        self.assertEqual(party.party_call(colour, len(party.COLOURS))["rgb_color"], party.COLOURS[0])
        self.assertNotIn("rgb_color", party.party_call(white, 0))
        self.assertEqual([party.party_call(white, i)["brightness_pct"] for i in range(3)], [100, 15, 100])

    def test_the_party_runs_while_the_show_is_on_then_restores(self):
        clock = FakeClock()
        show = party.Show(clock=clock)
        before = {"state": "on", "attributes": {"brightness": 140, "color_mode": "color_temp",
                                                "color_temp_kelvin": 2700, "supported_color_modes": ["xy", "color_temp"]}}
        ha = FakeHA(before)

        def sleep(seconds):
            clock.now += seconds

        show.start()
        party.LampParty(ha, "light.lamp", show, sleep=sleep).run()
        services = [s for s, _ in ha.calls]
        steps = int(party.SECONDS / party.STEP) + 1
        self.assertEqual(services, ["turn_on"] * (steps + 1))  # the last one puts the lamp back
        self.assertIn("rgb_color", ha.calls[0][1])
        self.assertEqual(ha.calls[-1], ("turn_on", {"transition": 1, "brightness": 140, "color_temp_kelvin": 2700}))

    def test_an_unreadable_lamp_stays_out_of_the_show(self):
        clock = FakeClock()
        show = party.Show(clock=clock)
        show.start()
        ha = FakeHA(None)
        party.LampParty(ha, "light.lamp", show, sleep=lambda s: None).run()
        self.assertEqual(ha.calls, [])  # nothing changed, so nothing to put back

    def test_the_lamp_is_put_back_even_if_the_show_breaks(self):
        clock = FakeClock()
        show = party.Show(clock=clock)
        show.start()
        ha = FakeHA({"state": "off", "attributes": {"supported_color_modes": ["xy"]}})

        def sleep(seconds):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            party.LampParty(ha, "light.lamp", show, sleep=sleep).run()
        self.assertEqual(ha.calls[-1], ("turn_off", {"transition": 1}))

    def test_the_sound_fills_the_show_and_never_clips(self):
        samples = party_sound.synth()
        self.assertEqual(len(samples), int(party_sound.SECONDS * party_sound.RATE))
        self.assertAlmostEqual(max(abs(v) for v in samples), party_sound.PEAK)
        # Every part of the show has sound in it: no silent second.
        rate = party_sound.RATE
        for second in range(int(party_sound.SECONDS)):
            chunk = samples[second * rate:(second + 1) * rate]
            self.assertGreater(max(abs(v) for v in chunk), 0.05, f"second {second}")
        self.assertEqual(samples, party_sound.synth())  # the same every time
        self.assertEqual(party_sound.SECONDS, party.SECONDS)

    def test_the_sound_plays_and_a_second_press_starts_it_again(self):
        with tempfile.TemporaryDirectory() as d:
            player = party_sound.Player(Path(d) / "party.wav", command="true")
            player.play()  # not made yet: silent, no error
            self.assertIsNone(player._playing)
            player.prepare()
            with wave.open(str(player.path)) as w:
                self.assertEqual((w.getnchannels(), w.getframerate()), (1, party_sound.RATE))
            with mock.patch.object(party_sound.subprocess, "Popen") as popen:
                running = popen.return_value
                running.poll.return_value = None
                player.play()
                player.play()
                self.assertEqual(popen.call_count, 2)
                running.terminate.assert_called_once()
                self.assertEqual(popen.call_args.args[0], ["true", str(player.path)])

    def test_the_page_draws_the_show_only_while_it_is_on(self):
        page = (Path(web.__file__).parent / "static" / "index.html").read_text()
        self.assertIn("if (s.party > 0) partyShow(s.party);", page)
        self.assertIn("if (now >= partyEnd)", page)
        self.assertIn('<b>1UP</b> <span id="party-name">PARTY</span>', page)
        self.assertIn("ctx.fillText(partyName", page)

    def test_main_never_rebinds_a_module_name(self):
        # A local named like a module (party = party.LampParty(...)) makes that
        # module unreachable in main(): brick-status would die at start, and
        # nothing else here runs main().
        import ast
        import brick_status.__main__ as entry
        tree = ast.parse(Path(entry.__file__).read_text())
        modules = {a.asname or a.name for n in tree.body if isinstance(n, ast.ImportFrom) for a in n.names}
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        assigned = {t.id for n in ast.walk(main) if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)}
        self.assertEqual(assigned & modules, set())

    def test_the_party_name_is_set_per_cabinet(self):
        self.assertEqual(Settings().party_name, "PARTY")
        with mock.patch.dict(os.environ, {"BRICK_STATUS_PARTY_NAME": "  Disco  "}):
            self.assertEqual(Settings.from_env().party_name, "DISCO")
        with mock.patch.dict(os.environ, {"BRICK_STATUS_PARTY_NAME": " "}):
            self.assertEqual(Settings.from_env().party_name, "PARTY")



class JobsTest(unittest.TestCase):
    def test_success_records_now(self):
        text = jobs.render("brick-roms", 1000.0, True, 12.5, 0.0)
        self.assertIn('brick_job_last_success{unit="brick-roms"} 1', text)
        self.assertIn('brick_job_last_success_timestamp_seconds{unit="brick-roms"} 1000', text)
        self.assertIn('brick_job_last_duration_seconds{unit="brick-roms"} 12.5', text)

    def test_failure_keeps_the_last_success(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = f"{d}/brick-job-x.prom"
            Path(path).write_text(jobs.render("x", 1000.0, True, None, 0.0))
            text = jobs.render("x", 2000.0, False, None, jobs.previous_success(path, "x"))
        self.assertIn('brick_job_last_success{unit="x"} 0', text)
        self.assertIn('brick_job_last_success_timestamp_seconds{unit="x"} 1000', text)
        self.assertIn('brick_job_last_run_timestamp_seconds{unit="x"} 2000', text)
        self.assertNotIn("duration", text)

    def test_checkout_info(self):
        text = jobs.render("brick-deploy", 1.0, True, None, 0.0, ("main", "abc1234"))
        self.assertIn('brick_job_checkout_info{unit="brick-deploy",branch="main",commit="abc1234"} 1', text)

    def test_detached_checkout_names_the_remote_branches_at_it(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            def git(*args, cwd=f"{d}/clone"):
                subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
            subprocess.run(["git", "init", "-q", "-b", "main", f"{d}/origin"], check=True)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x",
                cwd=f"{d}/origin")
            subprocess.run(["git", "clone", "-q", f"{d}/origin", f"{d}/clone"], check=True)
            git("checkout", "-q", "--detach", "origin/main")
            branch, commit = jobs.git_info(f"{d}/clone")
        self.assertEqual(branch, "main")
        self.assertTrue(commit)

    def test_main_reads_systemds_result(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "SERVICE_RESULT": "exit-code", "EXIT_STATUS": "1",
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
            subprocess.run([sys.executable, "-m", "brick_status.jobs", "x", "--dir", d], env=env, check=True)
            self.assertIn('brick_job_last_success{unit="x"} 0', Path(f"{d}/brick-job-x.prom").read_text())


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


class UpnpTest(unittest.TestCase):
    """overwatch's UPnP watch on the board, and the page it sends."""

    def check(self, **results):
        prom = FakeProm({f"overwatch_upnp_{k}": [({}, v)] for k, v in results.items()})
        return checks.upnp_checks(prom)[0]

    def test_states(self):
        c = self.check(unexpected=0, reachable=1, mappings=0)
        self.assertEqual((c.group, c.name, c.state, c.detail), ("cluster", "upnp forwards", "ok", "none"))
        self.assertEqual(self.check(unexpected=0, reachable=1, mappings=2).detail, "2 allowed")
        bad = self.check(unexpected=1, reachable=1, mappings=1)
        self.assertEqual((bad.state, bad.detail), ("fail", "1 unexpected forward: see overwatch"))
        self.assertEqual(self.check(unexpected=0, reachable=0, mappings=0).state, "warn")
        self.assertEqual(checks.upnp_checks(FakeProm({}))[0].state, "unknown")

    def test_an_unexpected_forward_pages_at_once_and_clears(self):
        bad = cluster_summary(("brick420", "ok", "Ready"), ("upnp forwards", "fail", "1 unexpected forward: see overwatch"))
        found, seen = notify.find_problems(bad, True, [])
        self.assertEqual([p.key for p in found], ["upnp"])
        self.assertIn("upnp", seen)
        import random
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d))
        pager = notify.Pager(d / "n.json", d / "i.jsonl", notify.HOLDS, random.Random(1))
        pager.observe(found, seen, 0)
        self.assertEqual([m["title"] for m in pager.take()], ["A door's open to the outside"])
        for m in pager.take():
            pager.sent(m)
        ok = cluster_summary(("brick420", "ok", "Ready"), ("upnp forwards", "ok", "none"))
        pager.observe(*notify.find_problems(ok, True, []), 120)
        self.assertEqual([m["title"] for m in pager.take()], ["That door's shut"])

    def test_no_overwatch_data_neither_pages_nor_clears(self):
        summary = cluster_summary(("brick420", "ok", "Ready"), ("upnp forwards", "unknown", "no data from overwatch"))
        found, seen = notify.find_problems(summary, True, [])
        self.assertEqual(found, [])
        self.assertNotIn("upnp", seen)


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

    def test_paused_pages_are_dropped_not_queued(self):
        p = self.pager()
        p.observe([self.NODE], self.SEEN, 0)
        p.observe([self.NODE], self.SEEN, 700)
        self.assertFalse(notify.paused(str(self.dir / "pages-paused")))
        (self.dir / "pages-paused").touch()
        self.assertTrue(notify.paused(str(self.dir / "pages-paused")))
        self.assertEqual(notify.hold(p), 1)
        self.assertEqual(p.take(), [])
        p.observe([], self.SEEN, 800)              # the all-clear is dropped too
        self.assertEqual(notify.hold(p), 1)
        self.assertEqual(len((self.dir / "incidents.jsonl").read_text().splitlines()), 1)

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
        self.assertEqual(notify.deliver(p, Down()), (0, 1))
        self.assertEqual(len(p.take()), 1)
        up = Up()
        self.assertEqual(notify.deliver(p, up), (1, 0))
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


JENKINS = Path(__file__).resolve().parents[2] / "jenkins"
BRICK9000 = JENKINS.parent / "brick9000"


class JenkinsImageTagTest(unittest.TestCase):
    """jenkins/image-tag: the tag is worked out from the image's files."""

    def setUp(self):
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader("image_tag", str(JENKINS / "image-tag"))
        spec = importlib.util.spec_from_loader("image_tag", loader)
        self.it = importlib.util.module_from_spec(spec)
        sys.dont_write_bytecode, before = True, sys.dont_write_bytecode
        try:
            loader.exec_module(self.it)
        finally:
            sys.dont_write_bytecode = before

    def test_values_yaml_names_the_tag_the_files_make(self):
        self.assertEqual(self.it.pinned(), self.it.tag(),
                         "run jenkins/image-tag --write after changing anything under jenkins/")
        self.assertRegex(self.it.tag(), r"^\d+\.\d+\.\d+-[0-9a-f]{12}$")

    def test_the_inputs_are_what_the_dockerfile_copies(self):
        dockerfile = (JENKINS / "Dockerfile").read_text()
        copied = set(re.findall(r"^COPY (?:--\S+ )*(\S+) ", dockerfile, re.M))
        self.assertEqual(copied | {"Dockerfile"}, set(self.it.INPUTS))

    def test_the_changes_listener_is_an_init_script(self):
        # Jenkins runs init.groovy.d scripts at start; .override replaces an old image's copy.
        dockerfile = (JENKINS / "Dockerfile").read_text()
        self.assertIn("changes-listener.groovy /usr/share/jenkins/ref/init.groovy.d/changes-listener.groovy.override",
                      dockerfile)
        listener = (JENKINS / "changes-listener.groovy").read_text()
        self.assertIn("loki.monitoring.svc.cluster.local:3100/loki/api/v1/push", listener)
        self.assertIn("source: 'jenkins', kind: 'change'", listener)

    def copy(self, d):
        import shutil as sh
        for name in self.it.INPUTS + ["values.yaml"]:
            src = JENKINS / name
            (sh.copytree if src.is_dir() else sh.copy)(src, Path(d) / name)
        return Path(d)

    def test_any_change_is_a_new_tag_and_leftovers_are_not(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = self.copy(d)  # not a git checkout: every file on disk counts
            first = self.it.tag(root)
            (root / "bin" / "__pycache__").mkdir()
            (root / "bin" / "__pycache__" / "x.cpython-313.pyc").write_bytes(b"junk")
            self.assertEqual(self.it.tag(root), first)
            (root / "jobs" / "test-all.groovy").write_text((root / "jobs" / "test-all.groovy").read_text() + "\n")
            second = self.it.tag(root)
            self.assertNotEqual(second, first)
            (root / "values.yaml").write_text((root / "values.yaml").read_text() + "# a comment\n")
            self.assertEqual(self.it.tag(root), second)  # values.yaml isn't in the image

    def test_write_puts_it_in_values_yaml(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = self.copy(d)
            values = root / "values.yaml"
            values.write_text(self.it.TAG_LINE.sub(r'\g<1>2.580.1-18\g<3>', values.read_text()))
            self.assertEqual(self.it.pinned(root), "2.580.1-18")
            self.assertEqual(self.it.write(root), self.it.tag(root))
            self.assertEqual(self.it.pinned(root), self.it.tag(root))


class JenkinsJobsTest(unittest.TestCase):
    """What seed.groovy expects of each job file, and scripts that at least parse.
    deploy runs these before switching brick9000 to a new commit."""

    def test_job_headers(self):
        jobs = sorted((JENKINS / "jobs").glob("*.groovy"))
        self.assertTrue(jobs)
        for job in jobs:
            header = list(itertools.takewhile(lambda l: l.startswith("//"), job.read_text().splitlines()))
            lines = [l[2:].strip() for l in header]
            about = [l for l in lines if not l.startswith(("param ", "choice ", "cron "))]
            self.assertTrue(about, f"{job.name}: no description comment")
            for line in lines:
                if line.startswith("param "):
                    self.assertRegex(line, r"^param (\w+)=(\S*)\s*(.*)$", job.name)
                if line.startswith("choice "):  # a dropdown: listed options, or @list from $JENKINS_HOME/choices
                    self.assertRegex(line, r"^choice (\w+)=(@[a-z0-9-]+|[^,\s]+(,[^,\s]+)*)\s*(.*)$", job.name)
            self.assertIn("pipeline {", job.read_text(), job.name)
            self.assertRegex(job.stem, r"^[a-z0-9-]+$")

    def maintenance_jobs(self):
        """Every job that runs on the controller: not the ones in agent pods
        (kubernetes, e.g. test-*, build-image, verify-images) or that only
        start others (test-all)."""
        return [j for j in (JENKINS / "jobs").glob("*.groovy")
                if "kubernetes {" not in j.read_text() and j.stem != "test-all"]

    def test_jobs_run_on_the_controller(self):
        # The controller is EXCLUSIVE (values.yaml): a job that doesn't ask for
        # the built-in node waits for an agent pod, which has none of the tools.
        for job in self.maintenance_jobs():
            self.assertIn("agent { label 'built-in' }", job.read_text(), job.name)

    def test_test_jobs_run_in_agent_pods(self):
        for job in (JENKINS / "jobs").glob("test-*.groovy"):
            text = job.read_text()
            if job.stem == "test-all":
                self.assertIn("agent none", text)
            else:
                self.assertIn("kubernetes {", text, job.name)
                self.assertNotIn("label 'built-in'", text, job.name)

    def test_agent_pods_single_source_the_controller_tag(self):
        # test-brick-cicd-config and the build pod's tools run the controller's
        # image. Its tag is set in one place - values.yaml - and stamped into
        # the pods at build time (ARG BRICK_JENKINS_TAG), so it can't drift and
        # a stale copy can't run old tools (hub-image-info, scripts/test's).
        values = (JENKINS / "values.yaml").read_text()
        self.assertRegex(values, r'(?m)^    tag: ".+"$', "no controller image tag in values.yaml")
        groovy = [JENKINS / "jobs" / "test-brick-cicd-config.groovy", JENKINS / "lib" / "build.groovy"]
        for f in groovy:
            text = f.read_text()
            self.assertIn("image: docker.io/whitepatrick/brick-jenkins:@BRICK_JENKINS_TAG@", text, f.name)
            self.assertNotRegex(text, r"brick-jenkins:\d", f.name)   # no literal tag left to drift
        # jenkins/release passes the tag; the Dockerfile stamps it into both.
        self.assertIn('--build-arg "BRICK_JENKINS_TAG=$tag"', (JENKINS / "release").read_text())
        dockerfile = (JENKINS / "Dockerfile").read_text()
        self.assertIn("ARG BRICK_JENKINS_TAG", dockerfile)
        for f in groovy:
            self.assertIn(f.name, dockerfile, f"{f.name} not stamped by the Dockerfile")

    def test_build_jobs_share_one_recipe_and_one_image_table(self):
        lib = (JENKINS / "lib" / "build.groovy").read_text()
        for job in ("build-image", "verify-images"):
            text = (JENKINS / "jobs" / f"{job}.groovy").read_text()
            self.assertIn("yaml buildkitPod()", text, job)
            self.assertIn("buildImage(", text, job)
            self.assertIn("checkoutForBuild(", text, job)
        # BuildKit pinned by digest (another version can change the digest).
        self.assertRegex(lib, r"moby/buildkit:v[\d.]+-rootless@sha256:[0-9a-f]{64}")
        # Reproducible: commit time, rewritten timestamps, the labels verify-images reads.
        for needle in ("SOURCE_DATE_EPOCH=", "rewrite-timestamp=true", "org.opencontainers.image.revision=",
                       "org.opencontainers.image.source="):
            self.assertIn(needle, lib)
        images = set(re.findall(r"'(whitepatrick/[\w-]+)':", lib))
        self.assertEqual(images, {"whitepatrick/wigle-sync", "whitepatrick/wigle-console", "whitepatrick/joke-reader",
                                  "whitepatrick/joke-importer", "whitepatrick/overwatch", "whitepatrick/presto-photos",
                                  "whitepatrick/brick-jenkins"})

    def test_jenkins_builds_its_own_image_and_marks_the_commit(self):
        lib = (JENKINS / "lib" / "build.groovy").read_text()
        entry = re.search(r"'whitepatrick/brick-jenkins': \[(.*?)\]", lib, re.S).group(1)
        for needle in ("repo: 'brick-cicd-config'", "context: 'jenkins'", "tagArg: 'BRICK_JENKINS_TAG'",
                       "values: 'jenkins/values.yaml'", "verify: false"):
            self.assertIn(needle, entry)
        self.assertIn("ARG BRICK_JENKINS_TAG", (JENKINS / "Dockerfile").read_text())
        self.assertIn("it.value.verify != false", (JENKINS / "jobs" / "verify-images.groovy").read_text())
        branches = (JENKINS / "jobs" / "build-branches.groovy").read_text()
        self.assertIn("--statuses", branches)
        self.assertIn("entry.values", branches)
        self.assertEqual(branches.count("/usr/share/brick-jenkins/bin/github-status"), 2)  # after each poll and build
        self.assertTrue((JENKINS / "bin" / "github-status").stat().st_mode & 0o111)

    def test_a_postsync_hook_checks_every_job_loaded(self):
        import yaml
        objects = yaml.safe_load((JENKINS / "values.yaml").read_text())["extraObjects"]
        named = {(o["kind"], o["metadata"]["name"]): o for o in objects}
        job = named[("Job", "jenkins-check-jobs")]
        self.assertEqual(job["metadata"]["annotations"]["argocd.argoproj.io/hook"], "PostSync")
        pod = job["spec"]["template"]["spec"]
        container = pod["containers"][0]
        # The image Argo just deployed, so it expects exactly that image's jobs.
        self.assertEqual(container["image"], "{{ .Values.controller.image.registry }}/"
                         "{{ .Values.controller.image.repository }}:{{ .Values.controller.image.tag }}")
        self.assertIn("/usr/share/brick-jenkins/jobs/*.groovy", container["args"][0])
        self.assertIn('"$new" != "$old"', container["args"][0])  # waits for the new pod, not the old
        self.assertEqual(pod["serviceAccountName"], "jenkins-check-jobs")
        rules = named[("Role", "jenkins-check-jobs")]["rules"]
        deletes = [r for r in rules if "delete" in r["verbs"]]
        self.assertEqual(deletes, [{"apiGroups": [""], "resources": ["pods"], "resourceNames": ["jenkins-0"],
                                    "verbs": ["delete"]}])
        self.assertTrue(all(set(r["verbs"]) <= {"get", "list", "watch"} for r in rules if r not in deletes))

    def test_the_hub_push_credential_comes_from_the_sealed_secret(self):
        values = (JENKINS / "values.yaml").read_text()
        self.assertIn("id: dockerhub", values)
        self.assertIn('username: "${dockerhub-username}"', values)
        self.assertIn('password: "${dockerhub-token}"', values)

    def test_test_all_runs_every_test_job(self):
        jobs = {p.stem.removeprefix("test-") for p in (JENKINS / "jobs").glob("test-*.groovy")} - {"all"}
        listed = re.search(r"for \(repo in \[(.*?)\]\)", (JENKINS / "jobs" / "test-all.groovy").read_text()).group(1)
        self.assertEqual(set(re.findall(r"'([\w-]+)'", listed)), jobs)

    def test_agent_pods_get_no_service_account_token(self):
        # Test jobs run each repo's code; it mustn't reach the cluster API.
        values = (JENKINS / "values.yaml").read_text()
        self.assertRegex(values, r"(?m)^serviceAccountAgent:\n  create: true\n  automountServiceAccountToken: false$")
        self.assertRegex(values, r"(?m)^  namespace: jenkins-agents$")
        self.assertIn("name: jenkins-agents", (JENKINS / "namespace.yaml").read_text())

    def test_job_scripts_parse(self):
        scripts = ([p for p in (JENKINS / "bin").iterdir()] + [JENKINS / n for n in ("release", "seal-secrets")]
                   + [BRICK9000 / n for n in ("build-status.sh", "deploy", "install.sh", "proxy/retire", "wifi-watchdog")])
        for script in scripts:
            text = script.read_text()
            if text.startswith("#!/usr/bin/env python3"):
                compile(text, script.name, "exec")  # in memory: leaves no __pycache__ in bin/
                continue
            result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{script.name}: {result.stderr}")

    def test_jobs_call_scripts_that_exist(self):
        for job in self.maintenance_jobs():
            paths = re.findall(r"/usr/share/brick-jenkins/(bin/[\w-]+)", job.read_text())
            self.assertTrue(paths, f"{job.name}: runs no script")
            for path in paths:
                self.assertTrue((JENKINS / path).is_file(), f"{job.name}: {path} missing")

    def test_ssh_config_is_not_writable(self):
        # ssh refuses a config file anyone but root can write, and COPY keeps
        # the checkout's mode, which can be group-writable.
        self.assertIn("COPY --chmod=644 ssh_config /etc/ssh/ssh_config.d/", (JENKINS / "Dockerfile").read_text())

    def test_test_pod_has_the_python_packages(self):
        # test-brick-cicd-config runs the tests in this image, so every Python
        # package brick9000 installs must be in it too, except the hardware
        # ones the tests stub. structlog was missing (test-all #48).
        hardware = {"python3-evdev", "python3-libgpiod"}
        installed = set(re.findall(r"python3-[\w-]+", (BRICK9000 / "install.sh").read_text()))
        dockerfile = (JENKINS / "Dockerfile").read_text()
        for package in sorted(installed - hardware):
            self.assertIn(package, dockerfile, f"{package}: brick9000 has it, the test pod doesn't")

    def test_secret_keys_are_sealed(self):
        # Every key Jenkins reads from the brick-jenkins Secret is one seal-secrets puts there.
        sealed = set(re.findall(r"--from-file=([\w-]+)", (JENKINS / "seal-secrets").read_text()))
        used = set(re.findall(r"\$\{([\w-]+)\}", (JENKINS / "values.yaml").read_text()))
        for f in [JENKINS / "seed.groovy", JENKINS / "ssh_config", *(JENKINS / "bin").iterdir()]:
            used |= set(re.findall(r"/run/secrets/additional/([\w-]+)", f.read_text()))
            used |= set(re.findall(r'\$secrets/([\w-]+)', f.read_text()))
        self.assertTrue(used)
        self.assertLessEqual(used, sealed)


class CrtActivityTest(unittest.TestCase):
    ONE = {"source": "a", "title": "overwatch:0.8.6", "status": "build", "details": {"job": "build-image"}}
    TWO = {"source": "b", "title": "deploy-brick9000", "status": "deploy", "details": {}}
    DEPLOY = {"source": "d", "title": "deploy brick9000", "status": "deploy",
              "details": {"branch": "feature/crt-branch", "commit": "5886cd2"}}

    def setUp(self):
        self.out, self.keys, self.now = [], [], 100.0
        self.screen = crt_activity.Screen(60, self.out.append, sleep=self.keys.append, clock=lambda: self.now)

    def drawn(self):
        text = "".join(self.out)
        self.out.clear()
        return text

    def test_the_title_is_typed_a_key_at_a_time_and_nothing_else_is(self):
        self.screen.update([self.ONE])
        self.assertEqual(len(self.keys), len(self.ONE["title"]))
        self.assertIn("o", self.out)  # the first letter went out on its own
        self.assertNotIn("status   build", self.out)
        self.assertIn("status   build", "".join(self.out))

    def test_details_appear_after_the_title(self):
        self.screen.update([self.ONE])
        text = self.drawn()
        self.assertLess(text.index("6"), text.index("status   build"))
        self.assertIn("job      build-image", text)

    def test_a_new_status_doesnt_type_the_title_again(self):
        self.screen.update([self.ONE])
        self.keys.clear()
        self.drawn()
        self.screen.update([{**self.ONE, "status": "push"}])
        self.assertEqual(self.keys, [])
        self.assertIn("status   push", self.drawn())

    def test_two_at_once_show_only_their_titles(self):
        self.screen.update([self.ONE, self.TWO])
        text = self.drawn()
        self.assertEqual(len(self.keys), len(self.ONE["title"]) + len(self.TWO["title"]))
        self.assertNotIn("status", text)
        self.assertNotIn("build-image", text)

    def test_a_second_job_clears_the_firsts_details(self):
        self.screen.update([self.ONE])
        self.keys.clear()
        self.drawn()
        self.screen.update([self.ONE, self.TWO])
        self.assertEqual(len(self.keys), len(self.TWO["title"]))  # only the new one is typed
        text = self.drawn()
        self.assertIn("\x1b[2J", text)
        self.assertNotIn("status", text)

    def test_back_to_one_shows_its_details_again(self):
        self.screen.update([self.ONE, self.TWO])
        self.keys.clear()
        self.drawn()
        self.screen.update([self.TWO])
        self.assertEqual(self.keys, [])
        self.assertIn("status   deploy", self.drawn())

    def test_running_counts_up_from_when_it_was_first_seen(self):
        self.screen.update([self.ONE])
        self.now += 75
        self.drawn()
        self.screen.update([self.ONE])
        self.assertIn("running  1m 15s", self.drawn())

    def test_a_long_title_fits_the_window(self):
        self.screen.update([{**self.ONE, "title": "x" * 200}])
        self.assertLess(len(self.keys), 60)

    def rows_drawn(self, rows):
        screen = crt_activity.Screen(60, self.out.append, sleep=self.keys.append, clock=lambda: self.now, rows=rows)
        screen.update([self.DEPLOY])
        # Each row as last drawn (a later write to a row replaces it).
        drawn = {int(row): text.strip()
                 for row, text in re.findall(r"\x1b\[(\d+);3H\x1b\[2K([^\x1b]*)", "".join(self.out))}
        self.out.clear()
        return {row: text for row, text in drawn.items() if text and row > 1}  # row 1 is the title

    def test_the_card_has_a_gap_when_there_is_room(self):
        self.assertEqual(self.rows_drawn(6), {3: "status   deploy", 4: "branch   feature/crt-branch",
                                             5: "commit   5886cd2", 6: "running  0s"})

    def test_short_of_rows_the_gap_goes_first(self):
        self.assertEqual(self.rows_drawn(5), {2: "status   deploy", 3: "branch   feature/crt-branch",
                                             4: "commit   5886cd2", 5: "running  0s"})

    def test_then_the_last_details_but_running_always_shows(self):
        # A terminal puts a cursor sent past its last row on that row: every
        # line beyond it would have landed on top of the last one.
        self.assertEqual(self.rows_drawn(4), {2: "status   deploy", 3: "branch   feature/crt-branch",
                                             4: "running  0s"})

    def test_nothing_running_clears_the_screen(self):
        self.screen.update([self.ONE])
        self.drawn()
        self.screen.update([])
        self.assertIn("\x1b[2J", self.drawn())
        self.assertEqual(self.screen.typed, {})

    def test_activity_is_not_in_the_playlist(self):
        self.assertIn("activity", matrix.PROGRAMS)
        self.assertNotIn("activity", Settings().crt_programs)


class ActivityTest(unittest.TestCase):
    def test_it_asks_again_only_after_a_second(self):
        now = [0.0]
        activity = lights.Activity("http://127.0.0.1:1", clock=lambda: now[0])
        calls = []
        activity._ask = lambda: calls.append(1) or True
        self.assertTrue(activity.running())
        activity.running()
        self.assertEqual(len(calls), 1)
        now[0] = 1.5
        activity.running()
        self.assertEqual(len(calls), 2)

    def test_no_plasma_api_means_nothing_running(self):
        self.assertFalse(lights.Activity("http://127.0.0.1:1").running())


if __name__ == "__main__":
    unittest.main()


NOW = 1_800_000_000.0


def timer(unit, next_s, enabled=None, running=False):
    t = {"unit": unit, "next": int(next_s * 1e6) if next_s else 0, "running": running}
    if enabled is not None:
        t["enabled"] = enabled
    return t


class UnitsTest(unittest.TestCase):
    def test_failed_overdue_and_stopped_timers_are_problems(self):
        data = {"failed": ["brick-roms.service"],
                "timers": [timer("ok.timer", NOW + 60), timer("late.timer", NOW - 20 * 60),
                           timer("just-late.timer", NOW - 60), timer("stopped.timer", 0, enabled=True),
                           timer("disabled.timer", 0, enabled=False),
                           # No next run because its service is running right now.
                           timer("busy.timer", 0, enabled=True, running=True)]}
        found = units.problems(data, NOW)
        self.assertEqual([(u, kind) for u, kind, _ in found],
                         [("brick-roms.service", "failed"), ("late.timer", "overdue"), ("stopped.timer", "stopped")])
        self.assertEqual(found[1][2], "overdue by 20m")

    def test_a_quick_service_finishing_mid_read_is_not_a_stopped_timer(self):
        # wifi-watchdog's service takes 0.1 s: list-timers saw no next run, and by
        # is-active it had finished. The second look finds the re-armed timer.
        lists = iter([[{"unit": "brick-wifi-watchdog.timer", "next": 0,
                        "activates": "brick-wifi-watchdog.service"}],
                      [{"unit": "brick-wifi-watchdog.timer", "next": int((NOW + 300) * 1e6)}]])
        with mock.patch.object(units, "systemctl", lambda user, *args: [] if "list-units" in args else next(lists)), \
                mock.patch.object(units, "is_enabled", return_value=True), \
                mock.patch.object(units, "is_running", return_value=False):
            data = units.read(True)
        self.assertEqual(units.problems(data, NOW), [])

    def test_a_timer_still_without_a_next_run_is_stopped(self):
        lists = iter([[{"unit": "gone.timer", "next": 0, "activates": "gone.service"}], [{"unit": "gone.timer", "next": 0}]])
        with mock.patch.object(units, "systemctl", lambda user, *args: [] if "list-units" in args else next(lists)), \
                mock.patch.object(units, "is_enabled", return_value=True), \
                mock.patch.object(units, "is_running", return_value=False):
            data = units.read(True)
        self.assertEqual([(u, kind) for u, kind, _ in units.problems(data, NOW)], [("gone.timer", "stopped")])

    def test_local_checks_cover_both_scopes_and_say_ok_when_clean(self):
        scopes = {False: {"failed": [], "timers": []},
                  True: {"failed": ["brick-mirror.service"], "timers": []}}
        cs = units.local_checks("brick9000", NOW, reader=lambda user: scopes[user])
        self.assertEqual([(c.name, c.state) for c in cs], [("brick9000: brick-mirror.service", "warn")])
        scopes[True]["failed"] = []
        cs = units.local_checks("brick9000", NOW, reader=lambda user: scopes[user])
        self.assertEqual([(c.name, c.state, c.detail) for c in cs], [("brick9000", "ok", "no failed units")])

    def test_local_checks_survive_systemctl_failing(self):
        def broken(user):
            raise subprocess.TimeoutExpired("systemctl", 10)
        [c] = units.local_checks("brick9000", NOW, reader=broken)
        self.assertEqual((c.group, c.state), ("services", "unknown"))

    def test_remote_checks_name_hosts_and_skip_the_board_itself(self):
        prom = FakeProm({
            "node_uname_info": [({"instance": "a:19100", "nodename": "brick2000"}, 1),
                                ({"instance": "b:19100", "nodename": "brick1982"}, 1),
                                ({"instance": "c:19100", "nodename": "brick420"}, 1)],
            "node_systemd_unit_state": [({"instance": "a:19100", "name": "influxdb.service"}, 1),
                                        ({"instance": "c:19100", "name": "k3s.service"}, 1)],
            "brick_systemd_unit_problem": [({"instance": "b:19100", "unit": "lcars-deploy.service",
                                             "problem": "failed"}, 1)],
            "brick_systemd_report_timestamp_seconds": [({"instance": "b:19100"}, NOW - 60)],
        })
        cs = {c.name: (c.state, c.detail) for c in units.remote_checks(prom, "brick420", NOW)}
        self.assertEqual(cs, {
            # Known failures stay ok, with why; the host still gets its ok row.
            "brick2000: influxdb.service": ("ok", "expected: pironman5 runs its own influxd"),
            "brick2000": ("ok", "no failed units"),
            "brick1982: lcars-deploy.service": ("warn", "failed"),
        })

    def test_a_stale_textfile_report_is_a_problem(self):
        prom = FakeProm({"node_uname_info": [({"instance": "b:19100", "nodename": "brick1982"}, 1)],
                         "brick_systemd_report_timestamp_seconds": [({"instance": "b:19100"}, NOW - 3600)]})
        [c] = units.remote_checks(prom, "brick9000", NOW)
        self.assertEqual((c.name, c.state, c.detail),
                         ("brick1982: brick-units.timer", "warn", "user units unreported for 60m"))

    def test_board_checks_keep_local_rows_when_prometheus_is_down(self):
        class Down:
            def query(self, promql):
                raise urllib.error.URLError("timed out")
        cs = units.board_checks(Down(), "brick9000", NOW, reader=lambda user: {"failed": [], "timers": []})
        self.assertEqual([(c.name, c.state) for c in cs], [("brick9000", "ok"), ("other hosts", "unknown")])

    def test_textfile_metrics_parse_and_replace_atomically(self):
        text = units.metrics("user", [("lcars-deploy.service", "failed", "failed"),
                                      ('we"ird.timer', "overdue", "")], NOW)
        self.assertIn('brick_systemd_unit_problem{scope="user",unit="lcars-deploy.service",problem="failed"} 1', text)
        self.assertIn('unit="we\\"ird.timer"', text)
        self.assertIn('brick_systemd_report_timestamp_seconds{scope="user"} 1800000000', text)
        d = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        target = d / "brick-units.prom"
        units.write_textfile(str(target), text)
        self.assertEqual(target.read_text(), text)
        self.assertEqual(oct(target.stat().st_mode & 0o777), "0o644")
        self.assertEqual([p.name for p in d.iterdir()], ["brick-units.prom"])


class CrtTest(unittest.TestCase):
    def test_the_playlist_is_cmatrix_only(self):
        s = Settings()
        s.cmatrix = sys.executable  # stands in for an installed cmatrix
        crt = web.crt_playlist(s)
        self.assertEqual([p["name"] for p in crt["programs"]], ["cmatrix"])
        self.assertEqual([(p["tint"], p["font"]) for p in crt["programs"]], [(False, 11)])
        self.assertEqual(crt["minutes"], 360.0)

    def test_the_gallery_can_still_be_named(self):
        s = Settings()
        s.cmatrix = sys.executable
        s.crt_programs = ["cmatrix", "gallery"]
        crt = web.crt_playlist(s)
        self.assertEqual([(p["name"], p["tint"], p["font"]) for p in crt["programs"]],
                         [("cmatrix", False, 11), ("gallery", True, 7)])

    def test_the_other_programs_are_gone(self):
        self.assertEqual(set(matrix.PROGRAMS), {"cmatrix", "gallery", "activity", "activity-log"})

    def test_playlist_skips_programs_that_are_not_installed(self):
        s = Settings()
        s.crt_programs = ["cmatrix", "no-such-program", "gallery"]
        s.cmatrix = sys.executable
        orig = matrix.PROGRAMS["gallery"]
        matrix.PROGRAMS["gallery"] = matrix.Program(("/nonexistent/gallery",))
        try:
            crt = web.crt_playlist(s)
        finally:
            matrix.PROGRAMS["gallery"] = orig
        self.assertEqual([p["name"] for p in crt["programs"]], ["cmatrix"])

    def test_nothing_installed_falls_back_to_cmatrix(self):
        s = Settings()
        s.crt_programs = ["no-such-program"]
        self.assertEqual(web.crt_playlist(s)["programs"],
                         [{"name": "cmatrix", "tint": False, "font": 11}])

    def test_unknown_program_names_run_cmatrix(self):
        # The page names the program; only names in PROGRAMS ever run.
        ran = []

        class FakePopen:
            pid = 0

            def __init__(self, argv, **kw):
                ran.append(argv)

            def poll(self):
                return 0

        orig = subprocess.Popen
        matrix.subprocess.Popen = FakePopen
        try:
            list(matrix.stream("rm -rf /", 80, 24, "red", cmatrix="/usr/bin/cmatrix"))
        finally:
            matrix.subprocess.Popen = orig
        self.assertEqual(ran, [["/usr/bin/cmatrix", "-b", "-s", "-C", "red"]])

    def test_one_frame_for_every_program_and_no_train(self):
        # The user's pick (2026-10-13): the square CRT frame for everything; sl's train is out.
        self.assertNotIn("train", matrix.PROGRAMS)
        self.assertNotIn("train", Settings().crt_programs)
        page = (Path(web.STATIC) / "index.html").read_text()
        self.assertNotIn("crt-box", page)
        self.assertNotIn("layout", web.crt_playlist(Settings())["programs"][0])

    def test_every_painting_has_a_credit(self):
        names = crt_gallery.credits()
        files = sorted(p.name for p in Path(crt_gallery.PAINTINGS).glob("*.jpg"))
        self.assertGreaterEqual(len(files), 8)
        self.assertEqual(sorted(names), files)
        self.assertEqual(names["van-gogh-the-starry-night.jpg"], "The Starry Night, Vincent van Gogh, 1889")


class NightlyLightsTest(unittest.TestCase):
    def test_test_all_running_gets_the_show_even_over_a_failure(self):
        checks = [{"group": "builds", "name": "test-all", "state": "active"},
                  {"group": "services", "name": "brick1982: x.service", "state": "fail"}]
        self.assertEqual(lights.idle_pattern(checks), "nightly")
        checks[0]["name"] = "test-wigle-sync"
        self.assertEqual(lights.idle_pattern(checks), "alert")


class PlasmaLibraryTest(unittest.TestCase):
    """jenkins/lib/plasma.groovy marks running builds, for the board's Builds
    view (checks), and tells the plasma API what to show."""

    LIB = JENKINS / "lib" / "plasma.groovy"

    def test_every_part_agrees_on_the_kinds(self):
        groovy = re.search(r"def kinds = \[(.*?)\]", self.LIB.read_text()).group(1)
        kinds = re.findall(r"'(\w+)'", groovy)
        self.assertEqual(sorted(kinds), sorted(checks.PLASMA_STAGES))

    def test_the_library_asks_the_api_and_never_fails_a_build(self):
        lib = self.LIB.read_text()
        self.assertIn("httpRequest(", lib)
        self.assertIn("timeout: 1,", lib)
        self.assertIn("catch (Exception e)", lib)
        self.assertIn("http_request", (JENKINS / "plugins.txt").read_text().split())

    def test_jobs_use_only_known_kinds(self):
        used = set()
        for job in (JENKINS / "jobs").glob("*.groovy"):
            used |= set(re.findall(r"plasma\('(\w+)'\)", job.read_text()))
        self.assertTrue(used)
        self.assertLessEqual(used, set(checks.PLASMA_STAGES))

    def test_the_seed_puts_the_library_in_every_job_and_the_image_ships_it(self):
        seed = (JENKINS / "seed.groovy").read_text()
        self.assertIn("script(library + '\\n' + text)", seed)
        self.assertIn("COPY --chown=root:root --chmod=755 lib /usr/share/brick-jenkins/lib",
                      (JENKINS / "Dockerfile").read_text())

    def test_the_mark_is_read_from_a_running_build(self):
        self.assertEqual(checks.plasma_stage("plasma:test"), "test")
        self.assertEqual(checks.plasma_stage("plasma:dance"), "")
        self.assertEqual(checks.plasma_stage("a note someone wrote"), "")
        self.assertEqual(checks.plasma_stage(None), "")
        jobs = [{"name": "test-wigle-sync",
                 "lastBuild": {"building": True, "timestamp": 1000 * (NOW - 30), "description": "plasma:install"}}]
        [c] = checks.build_checks(FakeJenkins(jobs), now=NOW)
        self.assertEqual((c.state, c.stage, c.detail), ("active", "install", "running 30s, install"))

    def test_running_build_says_what_it_builds(self):
        self.assertEqual(checks.build_label("#15 overwatch:0.8.3"), "overwatch:0.8.3")
        self.assertEqual(checks.build_label("#15"), "")
        self.assertEqual(checks.build_label(None), "")
        jobs = [{"name": "build-image", "lastCompletedBuild": None,
                 "lastBuild": {"building": True, "timestamp": 1000 * (NOW - 30),
                               "displayName": "#15 overwatch:0.8.3", "description": "plasma:build"}}]
        [c] = checks.build_checks(FakeJenkins(jobs), now=NOW)
        self.assertEqual(c.detail, "overwatch:0.8.3, running 30s, build")


class CheckButtonTest(unittest.TestCase):
    def test_two_presses_within_the_window(self):
        from brick_status.check import DoublePress
        d = DoublePress(1.5)
        self.assertFalse(d.press(10.0))
        self.assertTrue(d.press(11.0))
        self.assertFalse(d.press(11.5))  # a third press arms again
        self.assertFalse(d.press(13.5))  # too late: arms again
        self.assertTrue(d.press(14.0))

    def serve(self, status, body):
        import http.server
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.path)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}", seen

    def secret(self, text):
        import tempfile
        f = tempfile.NamedTemporaryFile("w", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(Path(f.name).unlink)
        return f.name

    def test_starts_the_job_with_its_token(self):
        from brick_status.check import start_job
        url, seen = self.serve(200, b'{"jobs": {"check-all": {"triggered": true}}, "message": "Triggered jobs."}')
        self.assertTrue(start_job(url, "check-all", self.secret("s3cr&t\n")))
        self.assertEqual(seen, ["/generic-webhook-trigger/invoke?token=check-all-s3cr%26t"])  # secret-scan: allow (a dummy secret)

    def test_a_secret_never_reaches_the_log(self):
        from brick_status.check import start_job
        url, _ = self.serve(404, b'{"jobs": null, "message": "Did not find any jobs"}')
        with capture_logs() as captured:
            self.assertFalse(start_job(url, "check-all", self.secret("hunter2")))
        self.assertNotIn("hunter2", repr(captured))
        self.assertEqual(captured[0]["status"], 404)

    def test_nothing_started_is_a_failure(self):
        from brick_status.check import start_job
        url, _ = self.serve(200, b'{"jobs": {"check-all": {"triggered": false}}}')
        with capture_logs() as captured:
            self.assertFalse(start_job(url, "check-all", self.secret("x")))
        self.assertEqual(captured[0]["event"], "Jenkins answered but started nothing")

    def test_no_secret_or_no_jenkins(self):
        from brick_status.check import start_job
        with capture_logs() as captured:
            self.assertFalse(start_job("http://127.0.0.1:9", "check-all", "/nonexistent/secret"))
            self.assertFalse(start_job("http://127.0.0.1:9", "check-all", self.secret("  ")))
            self.assertFalse(start_job("http://127.0.0.1:9", "check-all", self.secret("x"), timeout=2))
        self.assertEqual([e["event"] for e in captured],
                         ["no webhook secret", "webhook secret file empty", "Jenkins unreachable"])


class CrtLogsTest(unittest.TestCase):
    def test_clean_drops_escapes_and_controls(self):
        from brick_status.crt_logs import clean
        self.assertEqual(clean("\x1b[32m+ poetry install\x1b[0m\r"), "+ poetry install")
        self.assertEqual(clean("\x1b]8;;http://x\x07link\x1b]8;;\x07\tdone\x07"), "link    done")

    def test_clean_blanks_secrets(self):
        from brick_status.crt_logs import clean
        for line, gone in [
            ("export WEBHOOK_SECRET=abc123", "abc123"),
            ("password: hunter2", "hunter2"),
            ('{"api_key": "k-1234"}', "k-1234"),
            ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("curl -X POST 'https://jenkins/invoke?token=check-all-s3cr'", "s3cr"),  # secret-scan: allow (a dummy)
            ("git clone https://bot:ghp_shh@github.com/x/y", "ghp_shh"),  # secret-scan: allow (a dummy)
            ("pushed " + "A1b2" * 10, "A1b2" * 10),
            ("checked out " + "0123456789abcdef" * 2 + "01234567", "0123456789abcdef"),
        ]:
            with self.subTest(line=line):
                self.assertNotIn(gone, clean(line))
                self.assertIn("[hidden]", clean(line))

    def test_clean_keeps_ordinary_lines_and_digests(self):
        from brick_status.crt_logs import clean
        digest = "sha256:" + "0123456789abcdef" * 4
        self.assertEqual(clean(f"pushed docker.io/whitepatrick/overwatch:0.8.7@{digest}"),
                         f"pushed docker.io/whitepatrick/overwatch:0.8.7@{digest}")
        self.assertEqual(clean("Ran 133 tests in 6.1s"), "Ran 133 tests in 6.1s")

    def test_jenkins_log_reads_on_from_where_it_was(self):
        import http.server
        from brick_status.crt_logs import JenkinsLog
        console = ["Started by webhook\nRunning on built-in\n+ smo", "ke\nall up\n"]
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                start = int(self.path.rpartition("start=")[2])
                text = "".join(console)[start:].encode()
                self.send_response(200)
                self.send_header("X-Text-Size", str(start + len(text)))
                self.end_headers()
                self.wfile.write(text)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        full = "".join(console)
        console[:] = [console[0]]
        log = JenkinsLog(f"http://127.0.0.1:{server.server_port}", "brick-status", "pw", "check-all", 7)
        self.assertEqual(log.poll(), ["Started by webhook", "Running on built-in"])  # "+ smo" waits for its end
        console[:] = [full]
        self.assertEqual(log.poll(), ["+ smoke", "all up"])
        self.assertEqual(log.poll(), [])
        self.assertEqual(seen[0][0], "/job/check-all/7/logText/progressiveText?start=0")
        self.assertTrue(seen[1][0].endswith(f"?start={len(console[0]) - len('ke' + chr(10) + 'all up' + chr(10))}"))
        self.assertTrue(seen[0][1].startswith("Basic "))

    def test_jenkins_log_unreachable_is_no_lines(self):
        from brick_status.crt_logs import JenkinsLog
        self.assertEqual(JenkinsLog("http://127.0.0.1:9", "u", "p", "x", 1, timeout=1).poll(), [])

    def test_journal_log_follows_the_cursor(self):
        import tempfile
        from brick_status.crt_logs import JournalLog
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d, "journalctl")
            fake.write_text('#!/bin/sh\necho "$@" >> "$0.args"\n'
                            'case "$*" in *--after-cursor*) echo "restarting brick-status"; echo "-- cursor: c2";;\n'
                            '*) echo "fetching"; echo "tests passed"; echo "-- cursor: c1";; esac\n')
            fake.chmod(0o755)
            log = JournalLog("brick-deploy.service", journalctl=str(fake))
            self.assertEqual(log.poll(), ["fetching", "tests passed"])
            self.assertEqual(log.poll(), ["restarting brick-status"])
            args = Path(d, "journalctl.args").read_text().splitlines()
            self.assertIn("_SYSTEMD_USER_UNIT=brick-deploy.service", args[0])
            self.assertIn("-n 50", args[0])
            self.assertIn("--after-cursor c1", args[1])

    def test_which_items_have_a_log(self):
        from brick_status.crt_logs import JenkinsLog, JournalLog, for_item
        env = {"BRICK_STATUS_JENKINS_URL": "https://j", "BRICK_STATUS_JENKINS_PASSWORD": "pw"}
        log = for_item({"source": "jenkins-build-image", "details": {"job": "build-image", "build": "#36"}}, env)
        self.assertIsInstance(log, JenkinsLog)
        self.assertEqual(log.url, "https://j/job/build-image/36/logText/progressiveText")
        self.assertIsInstance(for_item({"source": "brick9000-deploy", "details": {}}, env), JournalLog)
        self.assertIsNone(for_item({"source": "jenkins-x", "details": {"job": "x"}}, env))
        self.assertIsNone(for_item({"source": "jenkins-x", "details": {"job": "x", "build": "#?"}}, env))
        self.assertIsNone(for_item({"source": "brick9000-image", "details": {}}, env))


class CrtLogFollowerTest(unittest.TestCase):
    CHECK = {"source": "jenkins-check-all", "title": "check-all", "details": {"job": "check-all", "build": "#3"}}
    TESTS = {"source": "jenkins-test-all", "title": "test-all", "details": {"job": "test-all", "build": "#9"}}

    class Log:
        def __init__(self):
            self.pending = []

        def poll(self):
            out, self.pending = self.pending, []
            return out

    def setUp(self):
        from brick_status.crt_logs import Follower
        self.out, self.now, self.logs = [], 100.0, {}

        def log_for(item):
            self.logs[item["source"]] = self.Log()
            return self.logs[item["source"]]

        self.follower = Follower(40, self.out.append, log_for=log_for, clock=lambda: self.now)

    def lines(self):
        text = "".join(self.out)
        self.out.clear()
        return [line for line in text.split("\r\n") if line]

    def test_a_header_then_the_lines_as_they_come(self):
        self.follower.update([self.CHECK])
        self.assertEqual(self.lines(), ["\x1b[1m-- check-all " + "-" * 26 + "\x1b[0m"])
        self.logs["jenkins-check-all"].pending = ["+ smoke", "", "ok   status.brick"]
        self.follower.update([self.CHECK])
        self.assertEqual(self.lines(), ["+ smoke", "ok   status.brick"])  # blank lines skipped
        self.follower.update([self.CHECK])
        self.assertEqual(self.lines(), [])

    def test_long_lines_are_cut_not_wrapped(self):
        self.follower.update([self.CHECK])
        self.lines()
        self.logs["jenkins-check-all"].pending = ["x" * 100]
        self.follower.update([self.CHECK])
        self.assertEqual(self.lines(), ["x" * 39])

    def test_follows_the_newest_and_back(self):
        self.follower.update([self.CHECK])
        self.now += 1
        self.follower.update([self.CHECK, self.TESTS])  # check-all started test-all
        self.assertTrue(self.lines()[-1].startswith("\x1b[1m-- test-all"))
        self.logs["jenkins-check-all"].pending = ["parent"]
        self.logs["jenkins-test-all"].pending = ["child"]
        self.follower.update([self.CHECK, self.TESTS])
        self.assertEqual(self.lines(), ["child"])
        self.follower.update([self.CHECK])  # test-all done: back to check-all, from now on
        self.assertTrue(self.lines()[-1].startswith("\x1b[1m-- check-all"))

    def test_no_log_still_says_whats_running(self):
        from brick_status.crt_logs import Follower
        follower = Follower(40, self.out.append, log_for=lambda item: None)
        follower.update([{"source": "brick9000-image", "title": "overwatch:0.8.7"}])
        self.assertEqual(len(self.lines()), 1)
        follower.update([])
        follower.update([])
        self.assertEqual(self.lines(), [])


class CrtSlotsTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(matrix.PROGRAMS)
        self.addCleanup(lambda: (matrix.PROGRAMS.clear(), matrix.PROGRAMS.update(self.saved)))
        matrix.PROGRAMS["main-a"] = matrix.Program(("sleep", "30"))
        matrix.PROGRAMS["main-b"] = matrix.Program(("sleep", "30"))
        matrix.PROGRAMS["log"] = matrix.Program(("sleep", "30"), slot="log")

    def start(self, name):
        stream = matrix.stream(name, 80, 24, "green")
        thread = threading.Thread(target=lambda: list(stream), daemon=True)
        thread.start()
        for _ in range(100):
            if any(p.args[0] == "sleep" and p.poll() is None and p not in self.before
                   for p in matrix._current.values()):
                break
            pytime.sleep(0.02)
        return stream

    def test_a_log_runs_beside_the_main_program_and_each_slot_replaces_its_own(self):
        self.before = set(matrix._current.values())
        self.start("main-a")
        main_a = matrix._current["main"]
        self.before = set(matrix._current.values())
        self.start("log")
        log = matrix._current["log"]
        self.assertIsNone(main_a.poll())  # still running beside the log
        self.before = set(matrix._current.values())
        self.start("main-b")
        main_a.wait(timeout=5)  # replaced
        self.assertIsNone(log.poll())  # the log is in its own slot
        for proc in list(matrix._current.values()):
            matrix._stop(proc)

    def test_the_log_program_is_in_the_playlist_for_the_page(self):
        crt = web.crt_playlist(Settings())
        self.assertEqual(crt["activity"]["font"], 22)
        self.assertEqual(crt["activity_log"], {"name": "activity-log", "tint": True, "font": 12})
        self.assertEqual(self.saved["activity-log"].slot, "log")


class MaintenanceTest(unittest.TestCase):
    """A node in a maintenance window (the maintenance-window job): calm, never amber or paged."""

    def prom(self, **extra):
        results = {
            "brick_maintenance_until_seconds": [({"node": "brick2000"}, 1893456000.0), ({"node": "brick1982"}, float("inf"))],
            'kube_pod_info{node=~"brick1982|brick2000"}': [({"namespace": "chuck", "pod": "reader-5d9-abcde"}, 1),
                                                           ({"namespace": "presto", "pod": "presto-photos-web-1-x"}, 1)],
            "kube_node_status_condition": [({"node": "brick2000"}, 0), ({"node": "brick666"}, 0)],
            "kube_deployment_status_replicas_unavailable": [({"namespace": "chuck", "deployment": "reader"}, 1),
                                                            ({"namespace": "wigle", "deployment": "console"}, 1)],
            "kube_pod_container_status_waiting_reason": [
                ({"namespace": "presto", "pod": "presto-photos-web-1-x", "reason": "CrashLoopBackOff"}, 1)],
            "node_uname_info": [({"instance": "brick1982", "nodename": "brick1982"}, 1)],
            "up == 0": [({"job": "brick1982-node", "instance": "brick1982"}, 0),
                        ({"job": "lcars", "instance": "brick1982:8767"}, 0),
                        ({"job": "overwatch", "instance": "10.42.0.9:8080", "namespace": "overwatch", "pod": "ow-1"}, 0)],
        }
        results.update(extra)
        return FakeProm(results)

    def test_nodes_and_what_runs_on_them_are_calm(self):
        got = {c.name: (c.state, c.detail) for c in checks.cluster_checks(self.prom())}
        self.assertEqual(got["brick2000"][0], checks.OK)
        self.assertTrue(got["brick2000"][1].startswith("maintenance until "))
        self.assertEqual(got["brick1982"], (checks.OK, "maintenance, no end set"))  # a host, not a k8s node
        self.assertEqual(got["brick666"], (checks.FAIL, "NotReady"))  # not in maintenance: still down
        self.assertEqual(got["chuck/reader"], (checks.OK, "1 not ready, maintenance"))
        self.assertEqual(got["wigle/console"], (checks.WARN, "1 not ready"))  # not on a resting node
        self.assertNotIn("presto/presto-photos-web-1-x", got)  # its crash loop is on brick2000
        scrapes = [n for n in got if n.startswith("scrape")]
        self.assertEqual(scrapes, ["scrape overwatch"])  # brick1982's two are hidden

    def test_no_windows_changes_nothing(self):
        plain = self.prom(brick_maintenance_until_seconds=[])
        got = {c.name: c.state for c in checks.cluster_checks(plain)}
        self.assertEqual(got["brick2000"], checks.FAIL)
        self.assertNotIn("brick1982", got)

    def test_a_broken_query_means_no_windows(self):
        class Broken(FakeProm):
            def query(self, promql):
                if "brick_maintenance" in promql:
                    raise OSError("no pushgateway")
                return super().query(promql)

        self.assertEqual(checks.maintenance(Broken({})), {})

    def test_resting_nodes_never_page(self):
        summary = cluster_summary(("control plane", "ok", "API up"), ("brick2000", "ok", "maintenance until 14:00"),
                                  ("brick666", "fail", "NotReady"))
        vitals = [{"node": "brick2000", "disk": 99.0}, {"node": "brick9000", "disk": 90.0}]
        found, seen = notify.find_problems(summary, True, vitals)
        self.assertEqual({p.key for p in found}, {"node:brick666", "disk:brick9000"})
        self.assertIn("disk:brick2000", seen)  # seen and fine: an open disk page for it clears


class LampGesturesTest(unittest.TestCase):
    def run_events(self, events, until):
        """events: (t, "down"|"up"); polled every 10 ms up to `until`."""
        from brick_status.lamp import Gestures
        g, out, i, t = Gestures(0.4, 0.8), [], 0, 0.0
        events = sorted(events)
        while t <= until:
            while i < len(events) and events[i][0] <= t:
                at, kind = events[i]
                if kind == "down":
                    g.press(at)
                elif (r := g.release(at)):
                    out.append((round(at, 1), r))
                i += 1
            if (r := g.poll(t)):
                out.append((round(t, 1), r))  # within a 10 ms poll
            t = round(t + 0.01, 2)
        return out

    def test_single_waits_out_the_double_window(self):
        out = self.run_events([(1.0, "down"), (1.1, "up")], 3)
        self.assertEqual(out, [(1.5, "single")])

    def test_double_fires_on_the_second_release(self):
        out = self.run_events([(1.0, "down"), (1.1, "up"), (1.3, "down"), (1.4, "up")], 3)
        self.assertEqual(out, [(1.4, "double")])

    def test_long_fires_while_held_and_its_release_does_nothing(self):
        out = self.run_events([(1.0, "down"), (2.5, "up")], 4)
        self.assertEqual(out, [(1.8, "long")])

    def test_two_slow_presses_are_two_singles(self):
        out = self.run_events([(1.0, "down"), (1.1, "up"), (2.0, "down"), (2.1, "up")], 4)
        self.assertEqual(out, [(1.5, "single"), (2.5, "single")])

    def test_a_key_up_without_a_key_down_is_ignored(self):
        # The debouncer dropped a bounced key-down, but its key-up still comes.
        out = self.run_events([(1.0, "down"), (1.1, "up"), (1.15, "up")], 3)
        self.assertEqual(out, [(1.5, "single")])

    def test_a_quick_double_press_is_a_double(self):
        # 60 ms between them: inside the board Debouncer's window, which the
        # lamp key skips.
        out = self.run_events([(1.0, "down"), (1.1, "up"), (1.16, "down"), (1.26, "up")], 3)
        self.assertEqual(out, [(1.3, "double")])

    def test_bounce_after_a_release_is_ignored(self):
        # A phantom 10 ms key-down 40 ms after the release.
        out = self.run_events([(1.0, "down"), (1.1, "up"), (1.14, "down"), (1.15, "up")], 3)
        self.assertEqual(out, [(1.5, "single")])

    def test_bounce_after_a_long_press_is_ignored(self):
        out = self.run_events([(1.0, "down"), (2.0, "up"), (2.05, "down"), (2.06, "up")], 4)
        self.assertEqual(out, [(1.8, "long")])

    def test_it_keeps_the_last_press_for_the_log(self):
        from brick_status.lamp import Gestures
        g = Gestures()
        g.press(1.0)
        g.release(1.1)
        g.press(1.15)
        g.release(1.16)
        held, gap, bounce = g.last
        self.assertAlmostEqual(held, 0.01)
        self.assertAlmostEqual(gap, 0.05)
        self.assertTrue(bounce)


class LampTest(unittest.TestCase):
    """The lamp's actions against a fake Home Assistant."""

    def setUp(self):
        import http.server

        from brick_status import lamp

        calls = self.calls = []
        self.status, self.reply, self.delay = 200, [], 0.0
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, self.headers["Authorization"], body))
                pytime.sleep(test.delay)  # the bulb's Bluetooth write, or a reconnect
                data = json.dumps(test.reply).encode()
                self.send_response(test.status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.lamp = lamp
        self.config = lamp.LampConfig.load(lamp.DEFAULT_FILE)
        ha = lamp.HomeAssistant(f"http://127.0.0.1:{self.server.server_port}", "tok")
        self.it = lamp.Lamp(self.config, ha)

    def test_single_toggles_and_answers_with_the_new_state(self):
        self.reply = [{"entity_id": "light.lamp", "state": "on"}]
        self.assertEqual(self.it.do("single"), "lamp-on")
        self.assertEqual(self.calls, [("/api/services/light/toggle", "Bearer tok", {"entity_id": "light.lamp"})])
        self.reply = [{"entity_id": "light.lamp", "state": "off"}]
        self.assertEqual(self.it.do("single"), "lamp-off")

    def test_double_steps_through_the_scenes_and_wraps(self):
        n = len(self.config.scenes)
        answers = [self.it.do("double") for _ in range(n + 1)]
        self.assertEqual(answers, [f"lamp-scene-{i + 1}" for i in range(n)] + ["lamp-scene-1"])
        path, _, body = self.calls[-1]
        self.assertEqual(path, "/api/services/light/turn_on")
        self.assertNotIn("name", body)  # the name is for the logs, not Home Assistant
        self.assertEqual(body["entity_id"], "light.lamp")

    def test_long_is_the_dim_warm_night_light(self):
        self.assertEqual(self.it.do("long"), "lamp-night")
        body = self.calls[-1][2]
        self.assertLessEqual(body["brightness_pct"], 10)
        self.assertLessEqual(body["color_temp_kelvin"], 2200)

    def test_the_button_times_gestures_and_answers_off_its_thread(self):
        clock = FakeClock()
        done = threading.Event()
        answers = []

        def answered(gesture, answer):
            answers.append((gesture, answer))
            done.set()

        self.reply = [{"entity_id": "light.lamp", "state": "on"}]
        button = self.lamp.LampButton(self.it, self.lamp.Gestures(0.4, 0.8), answered, clock=clock)
        button.press()
        clock.now += 0.1
        button.release()
        button.tick()
        self.assertEqual(answers, [])  # still waiting for a second press
        clock.now += 0.5
        button.tick()
        self.assertTrue(done.wait(5))
        self.assertEqual(answers, [("single", "lamp-on")])

    def test_refused_or_unreachable_answers_amber(self):
        self.status = 401
        self.assertEqual(self.it.do("single"), "lamp-unreachable")
        down = self.lamp.Lamp(self.config, self.lamp.HomeAssistant("http://127.0.0.1:9", "tok", timeout=1))
        self.assertEqual(down.do("long"), "lamp-unreachable")
        none = self.lamp.Lamp(self.config, self.lamp.HomeAssistant("http://127.0.0.1:9", ""))
        self.assertEqual(none.do("single"), "lamp-unreachable")

    def test_a_slow_bluetooth_write_still_counts(self):
        # Home Assistant answers a call only once the bulb has it: ~10 s after a
        # dropped link. The call waits call_timeout (12 s), not the read's timeout.
        self.assertEqual(self.lamp.HomeAssistant("http://ha", "tok").call_timeout, 12.0)
        self.delay = 0.5
        self.reply = [{"entity_id": "light.lamp", "state": "on"}]
        url = f"http://127.0.0.1:{self.server.server_port}"
        slow = self.lamp.Lamp(self.config, self.lamp.HomeAssistant(url, "tok", timeout=0.2, call_timeout=2))
        self.assertEqual(slow.do("single"), "lamp-on")
        hasty = self.lamp.Lamp(self.config, self.lamp.HomeAssistant(url, "tok", timeout=2, call_timeout=0.2))
        self.assertEqual(hasty.do("single"), "lamp-unreachable")

    def test_every_answer_is_a_board_display(self):
        import yaml
        displays = yaml.safe_load((Path(__file__).resolve().parents[2] / "brick-plasma" / "displays" /
                                   "board.yaml").read_text())["displays"]
        answers = {"lamp-on", "lamp-off", "lamp-night", "lamp-unreachable"}
        answers |= {f"lamp-scene-{i + 1}" for i in range(len(self.config.scenes))}
        self.assertLessEqual(answers, set(displays))


class WifiWatchdogTest(unittest.TestCase):
    """brick9000/wifi-watchdog, run with stub nmcli, sudo, ping and ip on PATH."""

    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin, self.calls = self.dir / "bin", self.dir / "calls"
        self.bin.mkdir()
        stubs = {
            "nmcli": 'echo "nmcli $*" >>"$CALLS"; [ "$1" = -g ] && echo "home-wifi"; true',
            "sudo": '[ "$1" = -n ] && shift; "$@"',
            "systemctl": 'echo "systemctl $*" >>"$CALLS"',
            "ping": 'echo "ping $*" >>"$CALLS"; [ -z "$PING_FAILS" ]',
            "ip": 'echo "default via 192.0.2.1 dev wlan0"',
            "sleep": "true",
        }
        for name, body in stubs.items():
            (self.bin / name).write_text(f"#!/bin/sh\n{body}\n")
            (self.bin / name).chmod(0o755)
        self.stats = self.dir / "multicast"
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", CALLS=str(self.calls),
                        WIFI_WATCHDOG_STATS=str(self.stats), WIFI_WATCHDOG_STATE=str(self.dir / "state"),
                        WIFI_WATCHDOG_TEXTFILE=str(self.dir / "wifi.prom"), WIFI_WATCHDOG_SILENT="600")

    def run_at(self, now, multicast, **extra):
        self.stats.write_text(f"{multicast}\n")
        env = dict(self.env, WIFI_WATCHDOG_NOW=str(now), **extra)
        return subprocess.run([str(BRICK9000 / "wifi-watchdog")], env=env, capture_output=True, text=True, check=True)

    def metric(self, name):
        for line in (self.dir / "wifi.prom").read_text().splitlines():
            if line.startswith(name):
                return int(line.split()[-1])
        self.fail(f"metric {name} not in the textfile")

    def calls_made(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_broadcasts_arriving_leave_the_wifi_alone(self):
        for i, now in enumerate(range(1000, 4000, 300)):
            self.run_at(now, 100 + i)
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.metric("brick_wifi_watchdog_broadcast_silent_seconds"), 0)
        self.assertEqual(self.metric("brick_wifi_watchdog_reconnects_total"), 0)

    def test_ten_silent_minutes_reactivate_the_connection(self):
        self.run_at(1000, 500)
        self.run_at(1300, 500)  # five minutes: not yet
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.metric("brick_wifi_watchdog_broadcast_silent_seconds"), 300)
        out = self.run_at(1600, 500)
        self.assertIn("re-activating", out.stdout)
        self.assertIn("nmcli connection up home-wifi", self.calls_made())
        self.assertFalse(any(c.startswith("systemctl") for c in self.calls_made()))  # the router answered
        self.assertEqual(self.metric("brick_wifi_watchdog_reconnects_total"), 1)
        self.assertEqual(self.metric("brick_wifi_watchdog_broadcast_silent_seconds"), 0)
        self.run_at(1900, 500)  # the silence clock restarted: no second reconnect yet
        self.assertEqual(self.metric("brick_wifi_watchdog_reconnects_total"), 1)

    def test_no_router_after_reactivating_restarts_networkmanager(self):
        self.run_at(1000, 7)
        self.run_at(1700, 7, PING_FAILS="1")
        self.assertIn("systemctl restart NetworkManager", self.calls_made())
        self.assertEqual(self.metric("brick_wifi_watchdog_networkmanager_restarts_total"), 1)


class LampReadTest(unittest.TestCase):
    """lamp.HomeAssistant.read and LampWatch: the lamp's state for the board."""

    def answer(self, body):
        from unittest import mock
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(body).encode()
        return mock.patch("urllib.request.urlopen", return_value=response)

    def test_on_with_brightness_and_colour(self):
        from brick_status import lamp
        ha = lamp.HomeAssistant("http://ha", "token")
        with self.answer({"state": "on", "attributes": {"brightness": 128, "color_temp_kelvin": 2700}}):
            self.assertEqual(ha.read("light.lamp"), {"on": True, "brightness_pct": 50, "kelvin": 2700})

    def test_off_has_no_brightness(self):
        from brick_status import lamp
        with self.answer({"state": "off", "attributes": {}}):
            self.assertEqual(lamp.HomeAssistant("http://ha", "t").read("light.lamp"),
                             {"on": False, "brightness_pct": None, "kelvin": None})

    def test_unreachable_or_no_token_is_none(self):
        from unittest import mock
        from brick_status import lamp
        self.assertIsNone(lamp.HomeAssistant("http://ha", "").read("light.lamp"))
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertIsNone(lamp.HomeAssistant("http://ha", "t").read("light.lamp"))

    def test_watch_keeps_the_last_read(self):
        from unittest import mock
        from brick_status import lamp
        ha = mock.Mock()
        ha.read.return_value = {"on": True, "brightness_pct": 5, "kelvin": 2000}
        watch = lamp.LampWatch(ha, "light.lamp")
        self.assertIsNone(watch.get())
        watch.refresh()
        self.assertEqual(watch.get()["brightness_pct"], 5)
        ha.read.assert_called_once_with("light.lamp")


class RecentBuildsTest(unittest.TestCase):
    """checks.recent_builds: the board's recent-builds screen (button 2)."""

    NOW = 1_800_000_000.0

    def jenkins(self, jobs=None, fails=False):
        from unittest import mock
        j = mock.Mock()
        if fails:
            j.jobs.side_effect = OSError("down")
        else:
            j.jobs.return_value = jobs
        return j

    def build(self, number, minutes_ago, result="SUCCESS", seconds=60, building=False, name=None):
        return {"number": number, "result": None if building else result, "building": building,
                "timestamp": (self.NOW - minutes_ago * 60) * 1000, "duration": seconds * 1000,
                "displayName": name or f"#{number}", "description": None}

    def test_newest_first_without_the_pollers(self):
        jobs = [
            {"name": "build-branches", "builds": [self.build(n, n) for n in range(1, 30)]},
            {"name": "maintenance-refresh", "builds": [self.build(1, 2)]},
            {"name": "build-image", "builds": [self.build(91, 300, name="#91 overwatch:0.8.7"), self.build(90, 900)]},
            {"name": "check-all", "builds": [self.build(12, 60, result="FAILURE", seconds=362)]},
        ]
        recent = checks.recent_builds(self.jenkins(jobs), {"build-branches", "maintenance-refresh"}, now=self.NOW)
        self.assertEqual([(r["job"], r["number"]) for r in recent],
                         [("check-all", 12), ("build-image", 91), ("build-image", 90)])
        self.assertEqual(recent[0]["state"], checks.FAIL)
        self.assertTrue(recent[0]["what"].startswith("failed"), recent[0]["what"])
        self.assertEqual(recent[1]["label"], "overwatch:0.8.7")

    def test_at_most_fifteen(self):  # the page shows as many as fit
        jobs = [{"name": f"job{i}", "builds": [self.build(1, i)]} for i in range(20)]
        self.assertEqual(len(checks.recent_builds(self.jenkins(jobs), now=self.NOW)), 15)

    def test_a_running_build_says_so(self):
        jobs = [{"name": "test-all", "builds": [self.build(7, 3, building=True)]}]
        r = checks.recent_builds(self.jenkins(jobs), now=self.NOW)[0]
        self.assertEqual(r["state"], checks.ACTIVE)
        self.assertIn("running", r["what"])
        self.assertEqual(r["took"], "")

    def test_hidden_jobs_come_from_board_yaml(self):
        board = Path(self.enterContext(tempfile.TemporaryDirectory())) / "board.yaml"
        board.write_text("hidden_jobs:\n  - build-branches\n  - maintenance-refresh\n")
        self.assertEqual(checks.hidden_jobs(board), {"build-branches", "maintenance-refresh"})
        self.assertEqual(checks.hidden_jobs("/nonexistent/board.yaml"), set())
        # The checked-in file hides the two pollers.
        self.assertTrue({"build-branches", "maintenance-refresh"} <= checks.hidden_jobs())

    def test_jenkins_down_is_none(self):
        self.assertIsNone(checks.recent_builds(self.jenkins(fails=True), now=self.NOW))


class SinceTest(unittest.TestCase):
    """since.py: how long each anomaly has been there, kept across restarts."""

    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.file = self.dir / "anomalies-since.json"

    def test_keys_match_the_anomalies_page(self):
        from brick_status import since
        summary = {"groups": {"cluster": {"checks": [{"name": "workloads", "state": "warn"},
                                                       {"name": "brick420", "state": "ok"}]},
                              "builds": {"checks": [{"name": "build-branches", "state": "fail"}]}}}
        vitals = [{"node": "brick9000", "temp": 71.0, "disk": 65.0}, {"node": "brick2000", "temp": 50, "disk": 90}]
        keys = since.anomaly_keys(summary, vitals, {"internet": False, "sshd": True}, "brick9000")
        self.assertEqual(keys, {"cluster/workloads", "builds/build-branches", "brick9000/temp",
                                "brick2000/disk", "internet"})

    def test_temperature_warning_clears_only_below_68(self):
        from brick_status import since
        warm = since.TempWarnings()
        seen = []
        for t in (69.5, 70.0, 69.0, 68.0, 67.9, 69.9, None, 72.0):
            vitals = [{"node": "brick9000", "temp": t}]
            warm.mark(vitals)
            seen.append(vitals[0]["temp_warn"])
        self.assertEqual(seen, [False, True, True, True, False, False, False, True])

    def test_keys_follow_the_temperature_warning(self):
        from brick_status import since
        vitals = [{"node": "brick9000", "temp": 69.0, "temp_warn": True},
                  {"node": "brick666", "temp": 71.0, "temp_warn": False}]
        self.assertEqual(since.anomaly_keys({}, vitals, {}, "brick9000"), {"brick9000/temp"})

    def test_first_seen_is_kept_and_cleared(self):
        from brick_status import since
        clock = since.Since(self.file)
        self.assertEqual(clock.observe({"a"}, 100.0), {"a": 100.0})
        self.assertEqual(clock.observe({"a", "b"}, 200.0), {"a": 100.0, "b": 200.0})
        self.assertEqual(clock.observe({"b"}, 300.0), {"b": 200.0})  # a cleared: forgotten
        self.assertEqual(clock.observe({"a", "b"}, 400.0), {"a": 400.0, "b": 200.0})  # a again: a new clock

    def test_a_restart_keeps_the_clock(self):
        from brick_status import since
        since.Since(self.file).observe({"cluster/workloads"}, 1000.0)
        self.assertEqual(since.Since(self.file).observe({"cluster/workloads"}, 90000.0),
                         {"cluster/workloads": 1000.0})

    def test_a_bad_file_starts_over(self):
        from brick_status import since
        for junk in ("not json", "[1, 2]"):  # corrupt, and JSON that isn't a dict
            with self.subTest(junk):
                self.file.write_text(junk)
                self.assertEqual(since.Since(self.file).observe({"x"}, 5.0), {"x": 5.0})


class LogsTest(unittest.TestCase):
    """logs.configure: the services' journals are JSON lines in the agreed shape."""

    def tearDown(self):
        structlog.reset_defaults()
        logging.getLogger().handlers.clear()

    def lines(self, fmt: str, emit) -> list[str]:
        out = io.StringIO()
        logs.configure("brick-test", fmt=fmt, stream=out)
        emit()
        return out.getvalue().splitlines()

    def test_json_lines_carry_the_standard_fields_and_the_facts(self):
        (line,) = self.lines("json", lambda: structlog.get_logger("brick_status.check").warning(
            "Jenkins' webhook refused", job="check-all", status=404))
        entry = json.loads(line)
        self.assertEqual({k: entry[k] for k in ("event", "level", "logger", "service", "job", "status")},
                         {"event": "Jenkins' webhook refused", "level": "warning",
                          "logger": "brick_status.check", "service": "brick-test",
                          "job": "check-all", "status": 404})
        self.assertRegex(entry["timestamp"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z$")

    def test_stdlib_loggers_go_through_the_same_renderer(self):
        (line,) = self.lines("json", lambda: logging.getLogger("urllib3").warning(
            "retrying %s", "x", extra={"attempt": 2}))
        entry = json.loads(line)
        self.assertEqual((entry["event"], entry["logger"], entry["service"], entry["attempt"]),
                         ("retrying x", "urllib3", "brick-test", 2))

    def test_an_exception_stays_in_the_one_line(self):
        def emit():
            try:
                raise ValueError("boom")
            except ValueError:
                structlog.get_logger("brick-status").exception("poll failed")
        (line,) = self.lines("json", emit)
        self.assertIn("ValueError: boom", json.loads(line)["exception"])

    def test_info_and_up_only(self):
        self.assertEqual(self.lines("json", lambda: structlog.get_logger("x").debug("chatter")), [])

    def test_console_without_log_format(self):
        with mock.patch.dict(os.environ, {"LOG_FORMAT": ""}):
            (line,) = self.lines(None, lambda: structlog.get_logger("brick-status").info("up", port=8080))
        self.assertIn("up", line)
        self.assertIn("port=8080", line)
        self.assertFalse(line.startswith("{"))
