"""The board's say on the button lights, through the plasma API (brick-plasma/).

The board is one sender, "board": it holds one signal, changing it with the
mode, and repeats it so the API's time limit only runs out if brick-status
dies. Other senders (Jenkins, deploys) outrank it by priority.
"""

import http.client
import json
import time
from urllib.parse import urlsplit

import structlog

from .checks import ACTIVE, FAIL, OK, UNKNOWN, WARN

log = structlog.get_logger(__name__)

# A display's priority is in its file (brick-plasma/displays/board.yaml). The
# view buttons aren't a named display: over a build, under a deploy.
PRIORITY_ACTIVE = 70

GREEN, AMBER, RED, GREY = [0, 255, 0], [255, 140, 0], [255, 0, 0], [90, 90, 110]
STATE_COLOURS = {OK: GREEN, WARN: AMBER, FAIL: RED, ACTIVE: GREEN, UNKNOWN: GREY}


def idle_pattern(checks: list[dict]) -> str:
    """Pick the idle light pattern for the most important current event."""
    states = {c["state"] for c in checks}
    running = {c["group"] for c in checks if c["state"] == ACTIVE}
    # The nightly test run gets the show, even over a failure: it lasts minutes.
    if any(c["group"] == "builds" and c["name"] == "test-all" and c["state"] == ACTIVE for c in checks):
        return "nightly"
    if FAIL in states:
        return "alert"
    if "builds" in running:
        return "building"
    if "wigle" in running:
        return "uploading"
    if WARN in states:
        return "warn"
    if UNKNOWN in states or not checks:
        return "unknown"
    return "calm"


class Activity:
    """Whether a sender has said what it's running, for the CRT. /api/state asks
    every second for the kiosk and for each mirror, so it's remembered briefly."""
    TIMEOUT = 0.3
    SECONDS = 1.0

    def __init__(self, url: str, clock=time.monotonic):
        parts = urlsplit(url)
        self.host, self.port = parts.hostname, parts.port or 80
        self.clock = clock
        self.answer, self.asked_at = False, None

    def running(self) -> bool:
        now = self.clock()
        if self.asked_at is None or now - self.asked_at >= self.SECONDS:
            self.answer, self.asked_at = self._ask(), now
        return self.answer

    def _ask(self) -> bool:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.TIMEOUT)
        try:
            connection.request("GET", "/activity")
            return bool(json.load(connection.getresponse())["activity"])
        except (OSError, ValueError, KeyError, http.client.HTTPException):
            return False  # the playlist carries on
        finally:
            connection.close()


class Plasma:
    SOURCE = "board"
    TTL = 30.0
    HEARTBEAT = 10.0
    TIMEOUT = 0.5  # the board's main loop waits on this

    def __init__(self, url: str, clock=time.monotonic):
        parts = urlsplit(url)
        self.host, self.port = parts.hostname, parts.port or 80
        self.clock = clock
        self.sent, self.sent_at = None, 0.0
        self.failing = False

    def show(self, pattern: str) -> None:
        self._put({"pattern": pattern})

    def answer(self, pattern: str) -> None:
        """A button's answer, once, from its own sender so the board's signal
        stays put underneath; the display's own priority and time limit apply."""
        self._send({"pattern": pattern}, "board-button")

    def party(self) -> None:
        """The party button's show (party.py), from its own sender so a
        lamp or check answer can't cut it short. The display sets its
        length (party.SECONDS); sending it again starts it again."""
        self._send({"pattern": "party"}, "party")

    def off(self) -> None:
        self.show("off")

    def buttons(self, group_states: dict[str, str], views: list[str], slots: list[int],
                selected_view: int) -> None:
        """Each view's button in its group's health colour."""
        colours, busy = {}, []
        for i, view in enumerate(views):
            if i >= len(slots):
                break
            state = group_states.get(view, OK if view == "overview" else UNKNOWN)
            colours[str(slots[i])] = STATE_COLOURS[state]
            if state == ACTIVE:
                busy.append(slots[i])
        selected = slots[selected_view] if selected_view < len(slots) else None
        self._put({"buttons": {"colors": colours, "selected": selected, "busy": busy},
                   "priority": PRIORITY_ACTIVE})

    def _put(self, signal: dict) -> None:
        wanted = {**signal, "ttl": self.TTL}
        now = self.clock()
        if wanted == self.sent and now - self.sent_at < self.HEARTBEAT:
            return
        # Only remember what was actually sent, so a missed one is retried.
        self.sent = wanted if self._send(wanted) else None
        self.sent_at = now

    def _send(self, body: dict, source: str = SOURCE) -> bool:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.TIMEOUT)
        try:
            connection.request("PUT", f"/signals/{source}", json.dumps(body),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            response.read()
            ok = response.status == 200
            problem = response.status
        except (OSError, http.client.HTTPException) as e:
            ok, problem = False, e
        finally:
            connection.close()
        if not ok and not self.failing:  # once, not every second
            log.warning("plasma API failing", error=problem)
        self.failing = not ok
        return ok
