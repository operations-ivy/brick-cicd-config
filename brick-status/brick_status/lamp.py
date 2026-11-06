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
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

SINGLE, DOUBLE, LONG = "single", "double", "long"
DEFAULT_FILE = Path(__file__).resolve().parent.parent / "lamp.yaml"


class Gestures:
    """Turns one button's key-downs and key-ups into single, double and long
    presses.

    A long press fires while the button is still held, once it has been down
    `long_seconds`; its release is then ignored. A single press waits
    `double_seconds` after its release for a second one; the second press's
    release makes it a double. Key-ups without a key-down (the debouncer
    dropped a bounced key-down) are ignored.
    """

    def __init__(self, double_seconds: float = 0.4, long_seconds: float = 0.8):
        self.double_seconds = double_seconds
        self.long_seconds = long_seconds
        self._down_at: float | None = None
        self._long_fired = False
        self._released_at: float | None = None  # a first press, waiting for a second

    def press(self, now: float) -> None:
        self._down_at = now
        self._long_fired = False

    def release(self, now: float) -> str | None:
        if self._down_at is None:
            return None
        self._down_at = None
        if self._long_fired:
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
    def load(cls, path: str | Path) -> "LampConfig":
        data = yaml.safe_load(Path(path).read_text()) or {}
        c = cls()
        c.entity = data.get("entity", c.entity)
        c.double_seconds = float(data.get("double_press_seconds", c.double_seconds))
        c.long_seconds = float(data.get("long_press_seconds", c.long_seconds))
        if scenes := data.get("scenes"):
            c.scenes = [dict(s) for s in scenes]
        if night := data.get("night"):
            c.night = dict(night)
        return c


class HomeAssistant:
    """Light service calls on Home Assistant's REST API."""

    def __init__(self, url: str, token: str, timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, service: str, entity: str, **data) -> str | None:
        """Call light.<service> on `entity`. The entity's new state ("on",
        "off") when Home Assistant says, "" when it took the call but didn't
        say, None when it couldn't be reached or refused."""
        if not self.token:
            log.warning("lamp: no BRICK_STATUS_HA_TOKEN; run home-assistant's scripts/setup")
            return None
        body = json.dumps({"entity_id": entity, **data}).encode()
        request = urllib.request.Request(
            f"{self.url}/api/services/light/{service}", data=body, method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                changed = json.loads(response.read() or b"[]")
        except urllib.error.HTTPError as e:
            log.warning("lamp: Home Assistant answered %d to light.%s", e.code, service)
            return None
        except (OSError, ValueError) as e:
            log.warning("lamp: couldn't reach Home Assistant (%s)", getattr(e, "reason", type(e).__name__))
            return None
        for s in changed if isinstance(changed, list) else []:
            if isinstance(s, dict) and s.get("entity_id") == entity:
                return str(s.get("state", ""))
        return ""


class Lamp:
    """What each gesture does, and the button lights' answer (a board.yaml
    display name), or None for no answer."""

    def __init__(self, config: LampConfig, ha: HomeAssistant):
        self.config = config
        self.ha = ha
        self._scene = -1

    def do(self, gesture: str) -> str:
        entity = self.config.entity
        if gesture == SINGLE:
            state = self.ha.call("toggle", entity)
            if state is None:
                return "lamp-unreachable"
            return "lamp-off" if state == "off" else "lamp-on"
        if gesture == DOUBLE:
            self._scene = (self._scene + 1) % len(self.config.scenes)
            scene = dict(self.config.scenes[self._scene])
            name = scene.pop("name", str(self._scene + 1))
            log.info("lamp: scene %s", name)
            ok = self.ha.call("turn_on", entity, **scene) is not None
            return f"lamp-scene-{self._scene + 1}" if ok else "lamp-unreachable"
        if gesture == LONG:
            log.info("lamp: night light")
            return "lamp-night" if self.ha.call("turn_on", entity, **self.config.night) is not None \
                else "lamp-unreachable"
        raise ValueError(f"unknown gesture {gesture!r}")
