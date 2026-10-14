"""What brick-status measures about itself, served at /metrics (README,
"Metrics"). Prometheus scrapes it through the proxy, at
status.brick.nozdormu.cloud/metrics.
"""

import time

from .checks import ACTIVE, FAIL, OK, UNKNOWN, WARN
from .metrics import Registry

REGISTRY = Registry()

# A check's state as a number, worst highest, for graphs and alerts.
STATE_VALUE = {OK: 0, ACTIVE: 1, UNKNOWN: 2, WARN: 3, FAIL: 4}

START = REGISTRY.gauge("brick_status_start_time_seconds", "When brick-status started.")
OVERALL = REGISTRY.gauge("brick_status_overall_state",
                         "The board's overall state: 0 ok, 1 active, 2 unknown, 3 warn, 4 fail.")
GROUP = REGISTRY.gauge("brick_status_group_state", "Each view's state (as overall_state).", ["group"])
CHECK = REGISTRY.gauge("brick_status_check_state", "Each check's state (as overall_state).", ["group", "check"])
POLLS = REGISTRY.counter("brick_status_polls_total", "Polls of Prometheus, Jenkins and the host.", ["result"])
POLL_SECONDS = REGISTRY.gauge("brick_status_poll_duration_seconds", "How long the last poll took.")
POLLED_AT = REGISTRY.gauge("brick_status_last_poll_timestamp_seconds", "When the last poll finished.")
INTERNET = REGISTRY.gauge("brick_status_internet_up", "1 when brick9000 reaches the internet.")
SSHD = REGISTRY.gauge("brick_status_sshd_up", "1 when sshd runs on brick9000.")
MODE = REGISTRY.gauge("brick_status_mode", "1 for the board's current mode (idle, active, quiet).", ["mode"])
VIEW = REGISTRY.gauge("brick_status_view", "1 for the view on screen.", ["view"])
BUTTONS = REGISTRY.counter("brick_status_button_presses_total", "Cabinet buttons pressed.", ["button"])
LIGHTS = REGISTRY.gauge("brick_status_lights_state",
                        "1 for what the plasma lights show: off, build, active or idle.", ["state"])
OPEN = REGISTRY.gauge("brick_status_open_problems", "Problems the pager is holding or has paged.")
QUEUED = REGISTRY.gauge("brick_status_pages_queued", "Pages waiting to be sent.")
PAGES = REGISTRY.counter("brick_status_pages_total", "Pages to the phone, by result.", ["result"])
CRT_STREAMS = REGISTRY.gauge("brick_status_crt_streams", "Terminal programs streaming to a page now.")
CRT_STARTS = REGISTRY.counter("brick_status_crt_starts_total", "Terminal programs started, by program.",
                              ["program"])
KIOSK_SEEN = REGISTRY.gauge("brick_status_kiosk_last_seen_timestamp_seconds",
                            "When the board's own browser last fetched the state (it polls every second).")
HTTP = REGISTRY.counter("brick_status_http_requests_total", "HTTP requests served.", ["path", "code"])

START.set(time.time())
for m in ("idle", "active", "quiet"):
    MODE.set(0, mode=m)
for s in ("off", "build", "active", "idle"):
    LIGHTS.set(0, state=s)
for r in ("ok", "error"):
    POLLS.inc(0, result=r)
for r in ("sent", "failed"):
    PAGES.inc(0, result=r)


def record_summary(summary: dict) -> None:
    OVERALL.set(STATE_VALUE.get(summary["overall"], 2))
    GROUP.replace({(g,): STATE_VALUE.get(v["state"], 2) for g, v in summary["groups"].items()})
    CHECK.replace({(g, c["name"]): STATE_VALUE.get(c["state"], 2)
                   for g, v in summary["groups"].items() for c in v["checks"]})


def one_hot(metric, label: str, current: str, choices) -> None:
    for choice in choices:
        metric.set(1 if choice == current else 0, **{label: choice})


# Only these paths get their own series; anything else counts as "other".
HTTP_PATHS = {"/", "/index.html", "/api/state", "/api/matrix", "/metrics"}


def http_path(path: str) -> str:
    if path.startswith("/static/"):
        return "/static/"
    return path if path in HTTP_PATHS else "other"
