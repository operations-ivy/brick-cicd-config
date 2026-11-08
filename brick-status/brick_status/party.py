"""The party button (brick-cicd-config#152): the cabinet's front-right button
(1UP, KEY_O) starts twenty seconds of everything at once, for whoever loves
the lights most. The name it shows is BRICK_STATUS_PARTY_NAME (default PARTY).

- the button lights play brick-plasma's "party" display (it ends by itself);
- the board page lays a full-screen show over itself (/api/state's "party");
- the Hue lamp beside the cabinet runs through colours, then goes back to
  exactly how it was (on or off, brightness, colour).

A press during a show starts the twenty seconds again; it never stacks a second
show, and the lamp goes back to how it was before the first. It works any time,
quiet hours too (the user, 2026-11-08), but not while a game is open: 1UP is a
game's button then.
"""

import logging
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

SECONDS = 20.0  # the user, 2026-11-08 (10 was too short, 30 too long); brick-plasma's party display matches
# The lamp's colours, one every STEP seconds: a rainbow with white in it.
COLOURS = [[255, 0, 0], [255, 140, 0], [255, 255, 0], [0, 255, 0], [0, 200, 255],
           [40, 90, 255], [170, 60, 255], [255, 0, 180], [255, 255, 255]]
STEP = 0.7
COLOUR_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}


class Show:
    """When the show ends. Thread-safe: the button and the web server both ask."""

    def __init__(self, seconds: float = SECONDS, clock: Callable[[], float] = time.time):
        self.seconds = seconds
        self.clock = clock
        self._until = 0.0
        self._lock = threading.Lock()

    def start(self) -> bool:
        """Start the show, or start it again from the top. True if no show was on."""
        now = self.clock()
        with self._lock:
            fresh = now >= self._until
            self._until = now + self.seconds
        return fresh

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._until - self.clock())

    def on(self) -> bool:
        return self.remaining() > 0


def restore_call(state: dict) -> tuple[str, dict]:
    """The light service call that puts a lamp back as `state` (Home
    Assistant's /api/states answer) had it."""
    if state.get("state") != "on":
        return "turn_off", {"transition": 1}
    attrs = state.get("attributes") or {}
    data: dict = {"transition": 1}
    if isinstance(attrs.get("brightness"), (int, float)):
        data["brightness"] = round(attrs["brightness"])
    mode = attrs.get("color_mode")
    for wanted, key in (("color_temp", "color_temp_kelvin"), ("hs", "hs_color"), ("xy", "xy_color"),
                        ("rgb", "rgb_color"), ("rgbw", "rgbw_color"), ("rgbww", "rgbww_color")):
        if mode == wanted and attrs.get(key) is not None:
            data[key] = attrs[key]
            break
    return "turn_on", data


def party_call(state: dict, step: int) -> dict:
    """The lamp's turn_on for one step of the show: the next colour for a
    colour lamp, a brightness flicker for a white-only one."""
    modes = set((state.get("attributes") or {}).get("supported_color_modes") or [])
    if modes & COLOUR_MODES:
        return {"rgb_color": COLOURS[step % len(COLOURS)], "brightness_pct": 100, "transition": STEP / 2}
    return {"brightness_pct": 100 if step % 2 == 0 else 15, "transition": STEP / 2}


class LampParty:
    """Runs the lamp's part while a show is on, then puts the lamp back."""

    def __init__(self, ha, entity: str, show: Show, sleep: Callable[[float], None] = time.sleep):
        self.ha, self.entity, self.show, self.sleep = ha, entity, show, sleep

    def run(self) -> None:
        before = self.ha.state(self.entity)
        if before is None:
            log.info("party: the lamp can't be read, so it stays out of the show")
            return
        step = 0
        try:
            while self.show.on():
                self.ha.call("turn_on", self.entity, **party_call(before, step))
                step += 1
                self.sleep(STEP)
        finally:
            service, data = restore_call(before)
            ok, _ = self.ha.call(service, self.entity, **data)
            log.info("party: lamp %s after %d colours", "put back" if ok else "NOT put back", step)
