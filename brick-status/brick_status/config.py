"""Settings, read from BRICK_STATUS_* environment variables."""

import os
from dataclasses import dataclass, field
from datetime import time

from .board import QuietSchedule

# brick9000's schedule: off overnight until 2026-10-09, then off through the
# work week (Monday 00:00 to Friday 16:00) and on all weekend.
DEFAULT_QUIET = ("00:00-06:00; 2026-10-09: Mon 00:01-Mon 16:00, Tue 00:00-Tue 16:00, "
                 "Wed 00:00-Wed 16:00, Thu 00:00-Thu 16:00, Fri 00:00-Fri 16:00")

# The board's screens (the redesign, brick-cicd-config#45): home (everything on
# one screen), the recent builds, the cluster's lists (wigle-sync's checks among
# them), and the anomalies.
VIEWS = ["overview", "builds", "cluster", "anomalies"]
# What the six front-panel buttons do, in BUTTON_KEYS order: the top row picks
# a screen, and the first of the bottom row the anomalies' own page (the user,
# 2026-11-07). The fifth is free (not the volume: the dial does that); a press
# there only wakes the board. The sixth is the check button (KEY_CHECK).
BUTTON_ACTIONS = ["overview", "builds", "cluster", "anomalies"]  # shorter than BUTTON_KEYS: the rest are free

# Linux key codes the picade overlay's gpio_keys device sends for the six
# front-panel buttons (two rows of three), top row left to right, then the
# bottom row left to right. Checked on the hardware.
BUTTON_KEYS = [29, 56, 57, 42, 44, 45]
KEY_LEFT = 105
KEY_RIGHT = 106
# The button on the cabinet's left side (KEY_ESC): sleep and wake
# (board.Board.side_button). Checked on the hardware.
KEY_WAKE = 1
# The bottom row's right-hand button (button 6, KEY_X): pressed twice, it
# starts Jenkins' check-all job (check.py). It was 1UP's until 2026-11-08,
# when 1UP became the Ramona button (the user).
KEY_CHECK = 45
# The cabinet's front-right button (1UP, KEY_O): ten seconds of light show on
# the buttons, the screen and the lamp (ramona.py, #152). Checked on the hardware.
KEY_RAMONA = 24
# The cabinet's front-left button (COIN, KEY_I): the Hue lamp beside it, by
# single, double and long press (lamp.py).
KEY_LAMP = 23


def _ints(value: str) -> list[int]:
    return [int(v) for v in value.split(",") if v.strip()]


@dataclass
class Settings:
    prometheus_url: str = "https://prometheus.brick.nozdormu.cloud"
    # Jenkins runs on the cluster (jenkins/), through brick9000's own proxy;
    # read as its read-only brick-status user, whose password is in the env file.
    jenkins_url: str = "https://jenkins.brick.nozdormu.cloud"
    jenkins_user: str = "brick-status"
    jenkins_password: str = ""
    poll_seconds: float = 15.0
    http_host: str = "127.0.0.1"
    http_port: int = 8765
    # The plasma API (brick-plasma/), on this machine.
    plasma_url: str = "http://127.0.0.1:8766"
    # Seconds after the last button press before the board goes back to idle.
    active_seconds: float = 120.0
    cmatrix: str = "cmatrix"
    # The overview's CRT window plays these in turn (matrix.PROGRAMS names),
    # each for crt_minutes; ones that aren't installed are skipped. cmatrix only
    # (the user, 2026-11-08); the gallery is still there to name here.
    crt_programs: list[str] = field(default_factory=lambda: ["cmatrix"])
    crt_minutes: float = 360.0
    # When the screen and lights are off; see board.QuietSchedule for the format.
    quiet: QuietSchedule = field(default_factory=lambda: QuietSchedule.parse(DEFAULT_QUIET))
    # How long the side button turns everything on for, while it's quiet.
    wake_seconds: float = 1800.0
    # A side-button sleep ends by this time of day at the latest, so one on a
    # Friday evening doesn't last the weekend (nothing is quiet then).
    sleep_morning: time = time(8, 0)
    # The check button's job, and Jenkins' webhook secret (install.sh's
    # copy for brick9000's Jenkins jobs; never logged).
    check_job: str = "check-all"
    # The front-left button's lamp, through Home Assistant as its Board user
    # (the token is in the env file, put there by home-assistant's scripts/setup).
    ha_url: str = "https://homeassistant.brick.nozdormu.cloud"
    ha_token: str = ""
    lamp_file: str = ""
    board_file: str = ""  # board.yaml: hidden_jobs (checks.hidden_jobs)
    webhook_secret_file: str = os.path.expanduser("~/.config/brick-jenkins/secrets/webhook_secret")
    # Plasma LED slot (0-9, 4 LEDs each) for each of the six buttons, in
    # BUTTON_KEYS order. brick9000's chain runs right to left along the top
    # row, then left to right along the bottom.
    button_slots: list[int] = field(default_factory=lambda: [2, 1, 0, 3, 4, 5])
    # brick-retro keeps this file while RetroArch is open: the screen stays on.
    retro_flag: str = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "brick-retro.playing")
    input_device_name: str = "gpio_keys"
    wlopm: str = "wlopm"
    # The volume page: wpctl, and the loudest its 100% goes as a fraction of the
    # DAC's full volume. Keep it the same as BRICK_RETRO_MAX_VOLUME.
    wpctl: str = "wpctl"
    max_volume: float = 0.5
    # Phone pages (notify.py). The topic name is the only thing keeping others
    # from reading or sending pages, so it lives in its own file, made by
    # install.sh; with no file, nothing is sent.
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic_file: str = os.path.expanduser("~/.config/brick-status/ntfy_topic")
    # While this file exists, no page goes out, whatever the topic file says,
    # and pages made meanwhile are dropped rather than queued (the user's
    # pause on everything that reaches the phone, 2026-10-26).
    pages_paused_file: str = os.path.expanduser("~/.config/brick-status/pages-paused")
    # Where tapping a page goes: the board's mirror, which only opens at home
    # (the name points at a LAN address). Empty for nowhere.
    ntfy_click: str = "https://status.brick.nozdormu.cloud"
    # Open problems, unsent pages, and the incident log the weekly report reads.
    notify_dir: str = os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "brick-status")

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
        s.plasma_url = env("BRICK_STATUS_PLASMA_URL", s.plasma_url).rstrip("/")
        s.active_seconds = float(env("BRICK_STATUS_ACTIVE_SECONDS", s.active_seconds))
        s.input_device_name = env("BRICK_STATUS_INPUT_DEVICE", s.input_device_name)
        s.retro_flag = env("BRICK_STATUS_RETRO_FLAG", s.retro_flag)
        s.wlopm = env("BRICK_STATUS_WLOPM", s.wlopm)
        s.wpctl = env("BRICK_STATUS_WPCTL", s.wpctl)
        s.max_volume = float(env("BRICK_STATUS_MAX_VOLUME", s.max_volume))
        s.cmatrix = env("BRICK_STATUS_CMATRIX", s.cmatrix)
        if v := env("BRICK_STATUS_CRT_PROGRAMS"):
            s.crt_programs = [n.strip() for n in v.split(",") if n.strip()]
        s.crt_minutes = float(env("BRICK_STATUS_CRT_MINUTES", s.crt_minutes))
        if v := env("BRICK_STATUS_QUIET"):
            s.quiet = QuietSchedule.parse(v)
        elif env("BRICK_STATUS_QUIET_START") and env("BRICK_STATUS_QUIET_END"):
            # The older daily-only settings.
            s.quiet = QuietSchedule.parse(f'{env("BRICK_STATUS_QUIET_START")}-{env("BRICK_STATUS_QUIET_END")}')
        s.wake_seconds = float(env("BRICK_STATUS_WAKE_SECONDS", s.wake_seconds))
        s.check_job = env("BRICK_STATUS_CHECK_JOB", s.check_job)
        s.ha_url = env("BRICK_STATUS_HA_URL", s.ha_url).rstrip("/")
        s.ha_token = env("BRICK_STATUS_HA_TOKEN", s.ha_token)
        s.lamp_file = env("BRICK_STATUS_LAMP_FILE", s.lamp_file)
        s.board_file = env("BRICK_STATUS_BOARD_FILE", s.board_file)
        s.webhook_secret_file = env("BRICK_STATUS_WEBHOOK_SECRET_FILE", s.webhook_secret_file)
        if v := env("BRICK_STATUS_SLEEP_MORNING"):
            s.sleep_morning = time.fromisoformat(v)
        if v := env("BRICK_STATUS_BUTTON_SLOTS"):
            s.button_slots = _ints(v)
        s.ntfy_url = env("BRICK_STATUS_NTFY_URL", s.ntfy_url).rstrip("/")
        s.ntfy_topic_file = env("BRICK_STATUS_NTFY_TOPIC_FILE", s.ntfy_topic_file)
        s.pages_paused_file = env("BRICK_STATUS_PAGES_PAUSED_FILE", s.pages_paused_file)
        s.ntfy_click = env("BRICK_STATUS_NTFY_CLICK", s.ntfy_click)
        s.notify_dir = env("BRICK_STATUS_NOTIFY_DIR", s.notify_dir)
        return s

    @property
    def board_state(self) -> str:
        """The side button's sleep, kept across restarts (board.Board)."""
        return os.path.join(self.notify_dir, "board.json")
