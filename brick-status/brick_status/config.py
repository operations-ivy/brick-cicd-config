"""Settings, read from BRICK_STATUS_* environment variables."""

import os
from dataclasses import dataclass, field
from datetime import time

# The four board views, in button order.
VIEWS = ["overview", "builds", "cluster", "wigle"]

# Linux key codes the picade overlay's gpio_keys device sends for the six
# front-panel buttons (two rows of three), top row left to right, then the
# bottom row left to right. Checked on the hardware.
BUTTON_KEYS = [29, 56, 57, 42, 44, 45]
KEY_LEFT = 105
KEY_RIGHT = 106


def _time(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _ints(value: str) -> list[int]:
    return [int(v) for v in value.split(",") if v.strip()]


@dataclass
class Settings:
    prometheus_url: str = "http://prometheus.local"
    # Jenkins runs on brick9000 itself (brick9000/jenkins); read as its
    # read-only brick-status user, whose password setup puts in the env file.
    jenkins_url: str = "http://127.0.0.1:8080"
    jenkins_user: str = "brick-status"
    jenkins_password: str = ""
    poll_seconds: float = 15.0
    http_host: str = "127.0.0.1"
    http_port: int = 8765
    plasma_fifo: str = "/tmp/plasma"
    # Pattern names are relative to the plasma daemon's /etc/plasma/.
    pattern_dir: str = "/etc/plasma/brick-status"
    pattern_prefix: str = "brick-status"
    # Seconds after the last button press before the board goes back to idle.
    active_seconds: float = 120.0
    cmatrix: str = "cmatrix"
    quiet_start: time = time(0, 0)
    quiet_end: time = time(6, 0)
    # Plasma LED slot (0-9, 4 LEDs each) for each of the six buttons, in
    # BUTTON_KEYS order. brick9000's chain runs right to left along the top
    # row, then left to right along the bottom.
    button_slots: list[int] = field(default_factory=lambda: [2, 1, 0, 3, 4, 5])
    # Written by brick9000/build-image; drives the build lights.
    build_status: str = os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "brick-build/status.json")
    # How long the lights show a finished build's result.
    build_result_seconds: float = 60.0
    input_device_name: str = "gpio_keys"
    wlopm: str = "wlopm"

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        env = os.environ.get
        s.prometheus_url = env("BRICK_STATUS_PROMETHEUS_URL", s.prometheus_url).rstrip("/")
        s.poll_seconds = float(env("BRICK_STATUS_POLL_SECONDS", s.poll_seconds))
        s.jenkins_url = env("BRICK_STATUS_JENKINS_URL", s.jenkins_url).rstrip("/")
        s.jenkins_user = env("BRICK_STATUS_JENKINS_USER", s.jenkins_user)
        s.jenkins_password = env("BRICK_STATUS_JENKINS_PASSWORD", s.jenkins_password)
        s.http_host = env("BRICK_STATUS_HTTP_HOST", s.http_host)
        s.http_port = int(env("BRICK_STATUS_HTTP_PORT", s.http_port))
        s.plasma_fifo = env("BRICK_STATUS_PLASMA_FIFO", s.plasma_fifo)
        s.pattern_dir = env("BRICK_STATUS_PATTERN_DIR", s.pattern_dir)
        s.active_seconds = float(env("BRICK_STATUS_ACTIVE_SECONDS", s.active_seconds))
        s.build_status = env("BRICK_STATUS_BUILD_STATUS", s.build_status)
        s.build_result_seconds = float(env("BRICK_STATUS_BUILD_RESULT_SECONDS", s.build_result_seconds))
        s.input_device_name = env("BRICK_STATUS_INPUT_DEVICE", s.input_device_name)
        s.wlopm = env("BRICK_STATUS_WLOPM", s.wlopm)
        s.cmatrix = env("BRICK_STATUS_CMATRIX", s.cmatrix)
        if v := env("BRICK_STATUS_QUIET_START"):
            s.quiet_start = _time(v)
        if v := env("BRICK_STATUS_QUIET_END"):
            s.quiet_end = _time(v)
        if v := env("BRICK_STATUS_BUTTON_SLOTS"):
            s.button_slots = _ints(v)
        return s
