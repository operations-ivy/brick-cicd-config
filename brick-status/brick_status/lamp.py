"""The cabinet's front-left button (COIN, KEY_I): the Hue lamp beside brick9000.

One press toggles it, two press within `double_seconds` step to the next scene,
and holding it `long_seconds` sets the night light. The lamp is a Bluetooth
Hue bulb that Home Assistant drives through brick2000's radio, so every action
is a call to Home Assistant's API, as its Board user (BRICK_STATUS_HA_TOKEN,
put in the env file by home-assistant's scripts/setup). The scenes and timings
are in lamp.yaml. While the cluster is down the lamp can't be reached at all;
the lights then answer amber.
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

import yaml

log = logging.getLogger(__name__)

SINGLE, DOUBLE, LONG = "single", "double", "long"
UNREACHABLE = "lamp-unreachable"  # the lights' answer when Home Assistant can't be reached
DEFAULT_FILE = Path(__file__).resolve().parent.parent / "lamp.yaml"


class Gestures:
    """Turns one button's key-downs and key-ups into single, double and long
    presses.

    A long press fires while the button is still held, once it has been down
    `long_seconds`; its release is then ignored. A single press waits
    `double_seconds` after its release for a second one; the second press's
    release makes it a double.

    The arcade buttons bounce: a release can be followed by a phantom
    key-down and key-up. The board's Debouncer drops any key-down within
    150 ms of a release, which would also drop a quick double press's second
    press, so this key skips it: a press held under `bounce_seconds` is
    bounce, and ignored. Key-ups without a key-down are ignored too.
    """

    def __init__(self, double_seconds: float = 0.4, long_seconds: float = 0.8,
                 bounce_seconds: float = 0.03):
        self.double_seconds = double_seconds
        self.long_seconds = long_seconds
        self.bounce_seconds = bounce_seconds
        self._down_at: float | None = None
        self._long_fired = False
        self._released_at: float | None = None  # a first press, waiting for a second
        self._last_up: float | None = None
        # The last press, for the log: (seconds held, seconds since the
        # release before it or None, whether it counted as bounce).
        self.last: tuple[float, float | None, bool] | None = None

    def press(self, now: float) -> None:
        self._down_at = now
        self._long_fired = False

    def release(self, now: float) -> str | None:
        if self._down_at is None:
            return None
        down_at, self._down_at = self._down_at, None
        gap = None if self._last_up is None else down_at - self._last_up
        self._last_up = now
        bounce = now - down_at < self.bounce_seconds
        self.last = (now - down_at, gap, bounce)
        if bounce or self._long_fired:
            return None
        if self._released_at is not None:
            self._released_at = None
            return DOUBLE
        self._released_at = now
        return None

    def poll(self, now: float) -> str | None:
        """The gesture that has come due by `now`, if any."""
        if self._down_at is not None:
            if not self._long_fired and now - self._down_at >= self.long_seconds:
                self._long_fired = True
                self._released_at = None
                return LONG
            return None
        if self._released_at is not None and now - self._released_at >= self.double_seconds:
            self._released_at = None
            return SINGLE
        return None


@dataclass
class LampConfig:
    entity: str = "light.lamp"
    double_seconds: float = 0.4
    long_seconds: float = 0.8
    # Each scene is light.turn_on's data (brightness_pct, color_temp_kelvin,
    # rgb_color, transition...) plus a name for the logs.
    scenes: list[dict] = field(default_factory=lambda: [{"name": "bright", "brightness_pct": 100}])
    night: dict = field(default_factory=lambda: {"brightness_pct": 5, "color_temp_kelvin": 2000})

    @classmethod
    def load(cls, path: str | Path) -> Self:
        data = yaml.safe_load(Path(path).read_text()) or {}
        defaults = cls()
        return cls(
            entity=data.get("entity", defaults.entity),
            double_seconds=float(data.get("double_press_seconds", defaults.double_seconds)),
            long_seconds=float(data.get("long_press_seconds", defaults.long_seconds)),
            scenes=[dict(s) for s in data.get("scenes") or defaults.scenes],
            night=dict(data.get("night") or defaults.night),
        )


class HomeAssistant:
    """Light service calls on Home Assistant's REST API."""

    def __init__(self, url: str, token: str, timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, service: str, entity: str, **data) -> tuple[bool, str]:
        """Call light.<service> on `entity`: (whether Home Assistant took it,
        the entity's new state: "on", "off", or "" when it didn't say)."""
        if not self.token:
            log.warning("lamp: no BRICK_STATUS_HA_TOKEN; run home-assistant's scripts/setup")
            return False, ""
        body = json.dumps({"entity_id": entity, **data}).encode()
        request = urllib.request.Request(
            f"{self.url}/api/services/light/{service}", data=body, method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                changed = json.loads(response.read() or b"[]")
        except urllib.error.HTTPError as e:
            log.warning("lamp: Home Assistant answered %d to light.%s", e.code, service)
            return False, ""
        except (OSError, ValueError) as e:
            log.warning("lamp: couldn't reach Home Assistant (%s)", getattr(e, "reason", type(e).__name__))
            return False, ""
        states = changed if isinstance(changed, list) else []
        return True, next((str(s.get("state", "")) for s in states
                           if isinstance(s, dict) and s.get("entity_id") == entity), "")

    def state(self, entity: str) -> dict | None:
        """The entity's whole state as Home Assistant has it ("state" and
        "attributes"), or None when it can't be asked."""
        if not self.token:
            return None
        request = urllib.request.Request(f"{self.url}/api/states/{entity}",
                                         headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read() or b"{}")
        except (OSError, ValueError) as e:
            log.warning("lamp: couldn't read %s (%s)", entity, e)
            return None
        return data if isinstance(data, dict) else None

    def read(self, entity: str) -> dict | None:
        """The light's state for the board: on, brightness (%) and colour
        temperature (K), or None when Home Assistant can't be asked."""
        if (data := self.state(entity)) is None:
            return None
        attrs = data.get("attributes") or {}
        brightness = attrs.get("brightness")
        return {"on": data.get("state") == "on",
                "brightness_pct": round(brightness / 255 * 100) if isinstance(brightness, (int, float)) else None,
                "kelvin": attrs.get("color_temp_kelvin")}


class LampWatch:
    """The lamp's last known state, read every `every` seconds (and `settle`
    seconds after a press), so the board's once-a-second poll never waits on
    Home Assistant. run() loops for ever: start it as a daemon thread."""

    def __init__(self, ha: HomeAssistant, entity: str, every: float = 15.0, settle: float = 1.0):
        self.ha, self.entity, self.every, self.settle = ha, entity, every, settle
        # Read by the web thread, written by run(): one reference assignment,
        # atomic in CPython, so no lock.
        self._state: dict | None = None
        self._wake = threading.Event()

    def get(self) -> dict | None:
        return self._state

    def refresh(self) -> None:
        self._state = self.ha.read(self.entity)

    def poke(self) -> None:
        """Read again now (after a press changed it)."""
        self._wake.set()

    def run(self) -> None:
        while True:
            self.refresh()
            self._wake.wait(self.every)
            if self._wake.is_set():
                self._wake.clear()
                time.sleep(self.settle)  # let Home Assistant report the new state first


class Lamp:
    """What each gesture does, and the button lights' answer (a board.yaml
    display name)."""

    def __init__(self, config: LampConfig, ha: HomeAssistant):
        self.config = config
        self.ha = ha
        self._scene = -1

    def do(self, gesture: str) -> str:
        entity = self.config.entity
        if gesture == SINGLE:
            ok, state = self.ha.call("toggle", entity)
            if not ok:
                return UNREACHABLE
            return "lamp-off" if state == "off" else "lamp-on"
        if gesture == DOUBLE:
            self._scene = (self._scene + 1) % len(self.config.scenes)
            scene = dict(self.config.scenes[self._scene])
            name = scene.pop("name", str(self._scene + 1))
            log.info("lamp: scene %s", name)
            ok, _ = self.ha.call("turn_on", entity, **scene)
            return f"lamp-scene-{self._scene + 1}" if ok else UNREACHABLE
        if gesture == LONG:
            log.info("lamp: night light")
            ok, _ = self.ha.call("turn_on", entity, **self.config.night)
            return "lamp-night" if ok else UNREACHABLE
        raise ValueError(f"unknown gesture {gesture!r}")


class LampButton:
    """The button itself, thread-safe: key-downs and key-ups from the input
    thread, gestures timed out by run(), and each gesture's action on a
    thread of its own (Home Assistant can be slow), its answer to `answered`."""

    def __init__(self, lamp: Lamp, gestures: Gestures, answered: Callable[[str, str], None],
                 clock: Callable[[], float] = time.monotonic):
        self.lamp = lamp
        self.gestures = gestures
        self.answered = answered
        self.clock = clock
        self._lock = threading.Lock()

    def press(self) -> None:
        with self._lock:
            self.gestures.press(self.clock())

    def release(self) -> None:
        with self._lock:
            gesture = self.gestures.release(self.clock())
            last = self.gestures.last
        if last is not None:
            held, gap, bounce = last
            log.info("lamp: key held %d ms%s%s", held * 1000,
                     "" if gap is None or gap > 2 else f", {gap * 1000:.0f} ms after the last release",
                     " (bounce, ignored)" if bounce else "")
        self._dispatch(gesture)

    def tick(self) -> None:
        with self._lock:
            gesture = self.gestures.poll(self.clock())
        self._dispatch(gesture)

    def run(self, every: float = 0.02) -> None:
        while True:
            time.sleep(every)
            self.tick()

    def _dispatch(self, gesture: str | None) -> None:
        if gesture is not None:
            threading.Thread(target=self._do, args=(gesture,), daemon=True).start()

    def _do(self, gesture: str) -> None:
        self.answered(gesture, self.lamp.do(gesture))
