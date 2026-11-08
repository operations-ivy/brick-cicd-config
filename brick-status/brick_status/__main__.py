"""brick-status: the status board daemon on brick9000.

Polls Prometheus, serves the board page Chromium shows in kiosk mode, reads
the front-panel buttons, drives the plasma button lights and the screen, and
pages a phone about problems that need a person (notify.py).

    python3 -m brick_status                # the daemon
    python3 -m brick_status notify --test  # send a test page
"""

import argparse
import logging
import os
import socket
import threading
import time
from pathlib import Path

from . import board_metrics as bm
from . import check, checks, host, lamp, lights, notify, units, web
from .board import ACTIVE, QUIET, Board
from .config import BUTTON_ACTIONS, BUTTON_KEYS, KEY_CHECK, KEY_LAMP, KEY_LEFT, KEY_RIGHT, KEY_WAKE, VIEWS, Settings
from .hardware import Screen, watch_buttons

log = logging.getLogger("brick-status")


class Status:
    """The latest check results, shared between the poller and the web server."""

    def __init__(self):
        self._lock = threading.Lock()
        self._summary = checks.summarize([])
        self._host = {"sshd": None, "internet": None}
        self._vitals: list[dict] = []
        self.updated = 0.0

    def set(self, summary: dict, host_state: dict, vitals: list[dict]) -> None:
        with self._lock:
            self._summary = summary
            self._host = host_state
            self._vitals = vitals
            self.updated = time.time()

    def get(self) -> dict:
        with self._lock:
            return {**self._summary, "host": self._host, "vitals": self._vitals}


def ntfy_from(settings: Settings) -> notify.Ntfy | None:
    topic = notify.read_topic(settings.ntfy_topic_file)
    if not topic:
        log.warning("no ntfy topic in %s: pages are not sent", settings.ntfy_topic_file)
        return None
    return notify.Ntfy(settings.ntfy_url, topic, settings.ntfy_click)


def poll(settings: Settings, status: Status, wake: threading.Event) -> None:
    prom = checks.Prometheus(settings.prometheus_url)
    jenkins = checks.Jenkins(settings.jenkins_url, settings.jenkins_user, settings.jenkins_password)
    local = host.LocalVitals(socket.gethostname())
    state = Path(settings.notify_dir)
    pager = notify.Pager(state / "notify.json", state / "incidents.jsonl")
    ntfy = ntfy_from(settings)
    while True:
        started = time.monotonic()
        try:
            try:
                vitals = checks.cluster_vitals(prom)
            except Exception as e:  # shown as missing rows; the checks report the outage
                log.warning("vitals query failed: %s", e)
                vitals = []
            vitals.append(local.read())
            internet = host.internet_up()
            summary = checks.summarize(checks.collect(prom, jenkins, internet)
                                       + units.board_checks(prom, socket.gethostname()))
            hidden = checks.hidden_jobs(settings.board_file)
            summary["recent"] = checks.recent_builds(jenkins, hidden)
            summary["hidden_jobs"] = sorted(hidden)
            sshd = host.sshd_running()
            status.set(summary, {"sshd": sshd, "internet": internet}, vitals)
            wake.set()
            pager.observe(*notify.find_problems(summary, internet, vitals), time.time(),
                          lambda problem: notify.facts(problem, summary, vitals))
            if notify.paused(settings.pages_paused_file):
                bm.PAGES.inc(notify.hold(pager), result="paused")
            elif internet:  # otherwise each send would stall the poll until it timed out
                sent, failed = notify.deliver(pager, ntfy)
                bm.PAGES.inc(sent, result="sent")
                bm.PAGES.inc(failed, result="failed")
            bm.record_summary(summary)
            bm.INTERNET.set(1 if internet else 0)
            bm.SSHD.set(1 if sshd else 0)
            bm.OPEN.set(len(pager.open))
            bm.QUEUED.set(len(pager.outbox))
            bm.POLLS.inc(result="ok")
        except Exception:
            # Keep polling; a dead poller would leave the board showing stale data.
            log.exception("poll failed")
            bm.POLLS.inc(result="error")
        bm.POLL_SECONDS.set(time.monotonic() - started)
        bm.POLLED_AT.set(time.time())
        time.sleep(settings.poll_seconds)


def button_name(code: int) -> str:
    """A cabinet key as a metric label: the view a button picks, or what it does."""
    if code in BUTTON_KEYS:
        i = BUTTON_KEYS.index(code)
        return BUTTON_ACTIONS[i] if i < len(BUTTON_ACTIONS) else f"button{i + 1}"
    return {KEY_WAKE: "side", KEY_CHECK: "check", KEY_LAMP: "lamp", KEY_LEFT: "left",
            KEY_RIGHT: "right"}.get(code, "other")


def send_test_page(settings: Settings) -> None:
    ntfy = ntfy_from(settings)
    if ntfy is None:
        raise SystemExit(f"no ntfy topic in {settings.ntfy_topic_file}; run brick9000/install.sh")
    local = host.LocalVitals(socket.gethostname())
    time.sleep(1)  # CPU % is measured between two reads
    ntfy.send(notify.test_message(local.read()))
    print("Test page sent.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(prog="brick_status")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("notify", help="phone pages").add_argument(
        "--test", action="store_true", required=True, help="send a test page")
    args = parser.parse_args()
    if args.command == "notify":
        send_test_page(settings)
        return

    board = Board(VIEWS, settings.active_seconds, settings.quiet, settings.wake_seconds,
                  settings.sleep_morning, settings.board_state)
    status = Status()
    plasma = lights.Plasma(settings.plasma_url)
    screen = Screen(settings.wlopm)
    wake = threading.Event()  # set when anything changed, so lights react at once
    check_press = check.DoublePress()

    def start_check() -> None:
        started = check.start_job(settings.jenkins_url, settings.check_job, settings.webhook_secret_file)
        plasma.answer("check-started" if started else "check-unreachable")

    def lamp_answered(gesture: str, answer: str) -> None:
        bm.LAMP.inc(gesture=gesture, result="unreachable" if answer == lamp.UNREACHABLE else "ok")
        # The lamp works in quiet hours too (that's when the night light is
        # for), but the button lights stay dark then.
        if board.mode(time.time()) != QUIET:
            plasma.answer(answer)
        lamp_watch.poke()  # the board shows the new state within a couple of seconds

    lamp_config = lamp.LampConfig.load(settings.lamp_file or lamp.DEFAULT_FILE)
    home_assistant = lamp.HomeAssistant(settings.ha_url, settings.ha_token)
    lamp_watch = lamp.LampWatch(home_assistant, lamp_config.entity)
    lamp_button = lamp.LampButton(
        lamp.Lamp(lamp_config, home_assistant),
        lamp.Gestures(lamp_config.double_seconds, lamp_config.long_seconds), lamp_answered)

    def on_release(code: int) -> None:
        if code == KEY_LAMP:
            lamp_button.release()

    def on_key(code: int) -> None:
        now = time.time()
        bm.BUTTONS.inc(button=button_name(code))
        if code == KEY_LAMP:
            # Not while a game is open (COIN is a game's button then). It
            # doesn't wake the board: the lamp is for the room, not the screen.
            if not os.path.exists(settings.retro_flag):
                lamp_button.press()
            return
        if code == KEY_WAKE:
            if os.path.exists(settings.retro_flag):
                log.info("side button: ignored, a game is open")
            else:
                did = board.side_button(now)
                if did == "asleep":
                    log.info("side button: asleep until %s",
                             time.strftime("%a %H:%M", time.localtime(board.asleep_until(now))))
                elif did == "awake":
                    log.info("side button: on for %d minutes", settings.wake_seconds // 60)
                else:
                    log.info("side button: on until the schedule turns it off")
        elif code == KEY_CHECK:
            if board.mode(now) != QUIET and not os.path.exists(settings.retro_flag):
                board.wake(now)
                if check_press.press(now):
                    threading.Thread(target=start_check, daemon=True).start()
                else:
                    plasma.answer("check-armed")
        elif code in BUTTON_KEYS:
            i = BUTTON_KEYS.index(code)
            if i < len(BUTTON_ACTIONS):
                board.press_view(VIEWS.index(BUTTON_ACTIONS[i]), now)
            else:  # a free button: input, nothing more
                board.wake(now)
        elif code in (KEY_LEFT, KEY_RIGHT):
            board.step_view(1 if code == KEY_RIGHT else -1, now)
        else:  # any other cabinet key counts as input (ignored while it's off)
            board.wake(now)
        wake.set()

    threading.Thread(target=poll, args=(settings, status, wake), daemon=True).start()
    threading.Thread(target=watch_buttons,
                     args=(settings.input_device_name, on_key, on_release, {KEY_LAMP}),
                     daemon=True).start()
    threading.Thread(target=lamp_button.run, daemon=True).start()
    threading.Thread(target=lamp_watch.run, daemon=True).start()
    threading.Thread(target=web.serve, args=(settings, board, status, lamp_watch),
                     daemon=True).start()
    log.info("brick-status up: prometheus=%s board=http://%s:%d",
             settings.prometheus_url, settings.http_host, settings.http_port)

    while True:
        wake.wait(timeout=1.0)
        wake.clear()
        now = time.time()
        if os.path.exists(settings.retro_flag):  # a game is open over the board
            board.keep_awake(now, 60)
        mode = board.mode(now)
        bm.one_hot(bm.MODE, "mode", mode, ("idle", "active", "quiet"))
        bm.one_hot(bm.VIEW, "view", board.views[board.view(now)], board.views)
        summary = status.get()
        screen.set(mode != QUIET)

        lights_state = "off" if mode == QUIET else "active" if mode == ACTIVE else "idle"
        bm.one_hot(bm.LIGHTS, "state", lights_state, ("off", "active", "idle"))
        if mode == QUIET:
            plasma.off()
        elif mode == ACTIVE:
            states = {g: v["state"] for g, v in summary["groups"].items()}
            states["overview"] = summary["overall"]
            # The Services view shows wigle-sync's checks too, so its button does.
            states["services"] = checks.worst([s for s in (states.get("services"), states.get("wigle")) if s])
            plasma.buttons(states, VIEWS, settings.button_slots, board.view(now))
        else:  # idle: the lights show the most important event
            all_checks = [c for g in summary["groups"].values() for c in g["checks"]]
            plasma.show(lights.idle_pattern(all_checks))


if __name__ == "__main__":
    main()
