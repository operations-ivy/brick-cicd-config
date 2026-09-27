"""The front-panel buttons and the HDMI screen."""

import logging
import subprocess
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


class Debouncer:
    """Drop key-downs that are really contact bounce.

    The arcade buttons bounce: on brick9000 a release was sometimes followed
    by a second key-down 16-120ms later, even after holding the button for
    half a second. So the window runs from the last key-up, not the last
    key-down.
    """

    def __init__(self, window: float = 0.15):
        self.window = window
        self._released: dict[int, float] = {}

    def press(self, code: int, t: float) -> bool:
        released = self._released.get(code)
        return released is None or t - released >= self.window

    def release(self, code: int, t: float) -> None:
        self._released[code] = t


def watch_buttons(device_name: str, on_key: Callable[[int], None]) -> None:
    """Call on_key(code) for each key press on the named input device, forever.

    The device is grabbed so presses only reach brick-status, not Chromium
    (Space would scroll the page, arrows would move around it).
    """
    import evdev  # apt: python3-evdev; imported here so tests don't need it

    debounce = Debouncer()
    while True:
        device = None
        for path in evdev.list_devices():
            d = evdev.InputDevice(path)
            if device is None and d.name == device_name:
                device = d
            else:
                d.close()
        if device is None:
            log.warning("input device %r not found, retrying", device_name)
            time.sleep(10)
            continue
        try:
            device.grab()
            log.info("reading buttons from %s", device.path)
            for event in device.read_loop():
                if event.type != evdev.ecodes.EV_KEY:
                    continue
                if event.value == 1 and debounce.press(event.code, event.timestamp()):
                    on_key(event.code)
                elif event.value == 0:
                    debounce.release(event.code, event.timestamp())
        except OSError as e:
            log.warning("lost input device: %s", e)
            time.sleep(5)


class Screen:
    """Turns the HDMI output on and off with wlopm (labwc supports its protocol)."""

    def __init__(self, wlopm: str = "wlopm"):
        self.wlopm = wlopm
        self.on: bool | None = None

    def set(self, on: bool) -> None:
        if on == self.on:
            return
        try:
            subprocess.run([self.wlopm, "--on" if on else "--off", "*"],
                           check=True, capture_output=True, timeout=10)
            self.on = on
        except (OSError, subprocess.SubprocessError) as e:
            # Usually the compositor isn't up yet; the next tick retries.
            log.warning("wlopm failed: %s", e)
