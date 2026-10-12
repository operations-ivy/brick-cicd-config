"""Which input devices are game controllers, and the launcher's rules: open
RetroArch when a pad turns up, survive Bluetooth drops, close it only when
the pad has been gone a long time."""

import time
from collections.abc import Callable

BTN_JOYSTICK, BTN_GAMEPAD, BTN_MODE = 0x120, 0x130, 0x13C
BTN_DPAD_UP, BTN_DPAD_DOWN = 0x220, 0x221
ABS_HAT0Y = 0x11
# The cabinet's own buttons belong to brick-status (it grabs them).
NOT_PADS = ("gpio_keys",)


def is_pad(name: str, keys: set[int]) -> bool:
    if name in NOT_PADS or name.startswith("vc4-hdmi"):
        return False
    return BTN_GAMEPAD in keys or BTN_JOYSTICK in keys


class Volume:
    """Home held + D-pad up/down steps the volume; Home alone opens the games;
    Home held for hold_seconds closes RetroArch, whatever state it's in (the
    way out when RetroArch doesn't see the pad)."""

    def __init__(self, change: Callable[[int], None], hold_seconds: float = 3):
        self.change, self.hold_seconds = change, hold_seconds
        self.home_down = False
        self.home_since = 0.0
        self.used = False  # Home was used for volume or a hold, so its release isn't a launch

    def key(self, code: int, value: int, now: float | None = None) -> bool:
        """A key event; True when it's a bare Home press-and-release."""
        if code == BTN_MODE:
            if value == 1:
                self.home_down, self.used = True, False
                self.home_since = time.time() if now is None else now
                return False
            if value == 0:
                bare = self.home_down and not self.used
                self.home_down = False
                return bare
            return False
        if self.home_down and value == 1 and code in (BTN_DPAD_UP, BTN_DPAD_DOWN):
            self.used = True
            self.change(1 if code == BTN_DPAD_UP else -1)
        return False

    def held(self, now: float) -> bool:
        """True once when Home has been held alone for hold_seconds."""
        if self.home_down and not self.used and now - self.home_since >= self.hold_seconds:
            self.used = True
            return True
        return False

    def hat(self, value: int) -> None:
        """D-pads that report as a hat: -1 is up."""
        if self.home_down and value != 0:
            self.used = True
            self.change(1 if value < 0 else -1)


class Launcher:
    """One tick a second with the pads connected now.

    - A pad connecting, or a bare Home press, opens RetroArch if it's closed.
      After a connect it waits settle_seconds first: udev is still setting the
      new device up, and RetroArch started too early never sees the pad.
    - A pad dropping while RetroArch is open changes nothing (RetroArch pauses
      the game); every redial_seconds the trusted pads are asked to reconnect.
    - No pad for idle_exit_seconds: RetroArch is told to quit (it saves a
      state on the way out), so the board isn't hidden behind a dead game.
    """

    def __init__(self, running: Callable[[], bool], start: Callable[[], None], stop: Callable[[], None],
                 redial: Callable[[], None], idle_exit_seconds: float = 900, redial_seconds: float = 30,
                 known: set[str] | None = None, settle_seconds: float = 3):
        self.running, self.start, self.stop, self.redial = running, start, stop, redial
        self.idle_exit_seconds, self.redial_seconds = idle_exit_seconds, redial_seconds
        self.known = set(known or ())  # pads already there at startup don't launch anything
        self.settle_seconds = settle_seconds
        self.lost_since: float | None = None
        self.last_redial = 0.0
        self.start_at: float | None = None  # a pad arrived: start RetroArch then

    def tick(self, pads: set[str], home: bool = False, now: float | None = None) -> None:
        now = time.time() if now is None else now
        arrived = bool(pads - self.known)
        self.known = set(pads)
        if not self.running():
            self.lost_since = None
            if home:
                self.start_at = now
            elif arrived:
                self.start_at = now + self.settle_seconds
            if self.start_at is not None and now >= self.start_at and pads:
                self.start_at = None
                self.start()
            elif not pads:
                self.start_at = None
            return
        self.start_at = None
        if pads:
            self.lost_since = None
            return
        if self.lost_since is None:
            self.lost_since = now
        if now - self.lost_since >= self.idle_exit_seconds:
            self.stop()
            self.lost_since = None
        elif now - self.last_redial >= self.redial_seconds:
            self.last_redial = now
            self.redial()
