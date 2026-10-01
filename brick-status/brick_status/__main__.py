"""brick-status: the status board daemon on brick9000.

Polls Prometheus, serves the board page Chromium shows in kiosk mode, reads
the front-panel buttons, and drives the plasma button lights and the screen.
"""

import logging
import socket
import threading
import time

from . import build, checks, host, lights, web
from .board import ACTIVE, QUIET, Board
from .config import BUTTON_KEYS, KEY_LEFT, KEY_RIGHT, KEY_WAKE, VIEWS, Settings
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


def poll(settings: Settings, status: Status, wake: threading.Event) -> None:
    prom = checks.Prometheus(settings.prometheus_url)
    jenkins = checks.Jenkins(settings.jenkins_url, settings.jenkins_user, settings.jenkins_password)
    local = host.LocalVitals(socket.gethostname())
    while True:
        try:
            try:
                vitals = checks.cluster_vitals(prom)
            except Exception as e:  # shown as missing rows; the checks report the outage
                log.warning("vitals query failed: %s", e)
                vitals = []
            vitals.append(local.read())
            internet = host.internet_up()
            status.set(checks.summarize(checks.collect(prom, jenkins, internet)),
                       {"sshd": host.sshd_running(), "internet": internet}, vitals)
            wake.set()
        except Exception:
            # Keep polling; a dead poller would leave the board showing stale data.
            log.exception("poll failed")
        time.sleep(settings.poll_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    board = Board(VIEWS, settings.active_seconds, settings.quiet, settings.wake_seconds)
    status = Status()
    plasma = lights.Plasma(settings.plasma_fifo, settings.pattern_dir, settings.pattern_prefix)
    screen = Screen(settings.wlopm)
    wake = threading.Event()  # set when anything changed, so lights react at once

    plasma.install_patterns()

    def on_key(code: int) -> None:
        now = time.time()
        if code == KEY_WAKE:
            if board.wake_button(now):
                log.info("side button: on for %d minutes", settings.wake_seconds // 60)
        elif code in BUTTON_KEYS:
            board.press_view(BUTTON_KEYS.index(code), now)
        elif code in (KEY_LEFT, KEY_RIGHT):
            board.step_view(1 if code == KEY_RIGHT else -1, now)
        else:  # any other cabinet key counts as input (ignored while it's off)
            board.wake(now)
        wake.set()

    threading.Thread(target=poll, args=(settings, status, wake), daemon=True).start()
    threading.Thread(target=watch_buttons, args=(settings.input_device_name, on_key),
                     daemon=True).start()
    threading.Thread(target=web.serve, args=(settings, board, status),
                     daemon=True).start()
    log.info("brick-status up: prometheus=%s board=http://%s:%d",
             settings.prometheus_url, settings.http_host, settings.http_port)

    last_active = None
    while True:
        wake.wait(timeout=1.0)
        wake.clear()
        now = time.time()
        mode = board.mode(now)
        summary = status.get()
        screen.set(mode != QUIET)

        building = build.light_pattern(settings.build_status, now, settings.build_result_seconds)

        if mode == QUIET:
            plasma.off()
            last_active = None
        elif building:  # a build on brick9000 takes over the lights
            plasma.show(building)
            last_active = None
        elif mode == ACTIVE:
            states = {g: v["state"] for g, v in summary["groups"].items()}
            states["overview"] = summary["overall"]
            key = (board.view(now), tuple(sorted(states.items())))
            if key != last_active:  # only rewrite the PNG when something changed
                plasma.show("active", lights.active_frames(states, VIEWS, settings.button_slots, key[0]))
                last_active = key
        else:  # idle: the lights show the most important event
            all_checks = [c for g in summary["groups"].values() for c in g["checks"]]
            plasma.show(lights.idle_pattern(all_checks))
            last_active = None


if __name__ == "__main__":
    main()
