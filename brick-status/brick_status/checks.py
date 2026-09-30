"""Turn Prometheus query results into board checks.

Each check has a state: ok, warn, fail, active (something in progress, like a
build or a wigle-sync upload), or unknown (no data).
"""

import json
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime

OK, WARN, FAIL, ACTIVE, UNKNOWN = "ok", "warn", "fail", "active", "unknown"
# Worst first, for rolling a group up into one state.
SEVERITY = [FAIL, WARN, UNKNOWN, ACTIVE, OK]

# Jenkins prometheus plugin's last_build_result_ordinal values.
JENKINS_RESULTS = {0: (OK, "success"), 1: (WARN, "unstable"), 2: (FAIL, "failure"),
                   3: (UNKNOWN, "not built"), 4: (WARN, "aborted")}

# wigle-sync runs hourly and does nothing while the Pi is away, which is fine.
# Only errors that every run has hit for this long are worth flagging.
WIGLE_ERROR_WINDOW = "2h"

# Waiting reasons that only mean "couldn't reach the registry". While the
# internet is down that's the ISP, not the cluster, so they're degraded, not down.
IMAGE_PULL_REASONS = {"ImagePullBackOff", "ErrImagePull"}


@dataclass
class Check:
    group: str
    name: str
    state: str
    detail: str = ""


def worst(states: list[str]) -> str:
    if not states:
        return UNKNOWN
    return min(states, key=SEVERITY.index)


class Prometheus:
    def __init__(self, url: str, timeout: float = 5.0):
        self.url = url
        self.timeout = timeout

    def query(self, promql: str) -> list[tuple[dict, float]]:
        qs = urllib.parse.urlencode({"query": promql})
        with urllib.request.urlopen(f"{self.url}/api/v1/query?{qs}", timeout=self.timeout) as resp:
            body = json.load(resp)
        if body.get("status") != "success":
            raise RuntimeError(body.get("error", "query failed"))
        return [(r["metric"], float(r["value"][1])) for r in body["data"]["result"]]


def cluster_checks(prom: Prometheus, internet: bool = True) -> list[Check]:
    checks = []
    for m, v in prom.query('kube_node_status_condition{condition="Ready",status="true"}'):
        checks.append(Check("cluster", m["node"], OK if v == 1 else FAIL,
                            "Ready" if v == 1 else "NotReady"))

    stuck = prom.query('sum by (namespace, pod, reason) (kube_pod_container_status_waiting_reason'
                       '{reason=~"CrashLoopBackOff|ImagePullBackOff|ErrImagePull|CreateContainerConfigError"}) > 0')
    # Pods that can't start only because the registry is out of reach.
    offline = [] if internet else [(m["namespace"], m["pod"]) for m, _ in stuck
                                   if m["reason"] in IMAGE_PULL_REASONS]

    unavailable = prom.query("kube_deployment_status_replicas_unavailable > 0")
    unready_sts = prom.query(
        "kube_statefulset_replicas - kube_statefulset_status_replicas_ready > 0")
    for m, v in unavailable + unready_sts:
        name = m.get("deployment") or m.get("statefulset")
        # A workload's pods are named <workload>-<suffix>.
        if any(ns == m["namespace"] and pod.startswith(f"{name}-") for ns, pod in offline):
            checks.append(Check("cluster", f"{m['namespace']}/{name}", WARN,
                                f"{int(v)} not ready, internet down"))
        else:
            checks.append(Check("cluster", f"{m['namespace']}/{name}", FAIL, f"{int(v)} not ready"))
    if not unavailable and not unready_sts:
        checks.append(Check("cluster", "workloads", OK, "all replicas ready"))

    for m, _ in stuck:
        if (m["namespace"], m["pod"]) in offline:
            checks.append(Check("cluster", f"{m['namespace']}/{m['pod']}", WARN, f"{m['reason']}, internet down"))
        else:
            checks.append(Check("cluster", f"{m['namespace']}/{m['pod']}", FAIL, m["reason"]))

    for m, _ in prom.query("up == 0"):
        checks.append(Check("cluster", f"scrape {m.get('job', '?')}", WARN,
                            f"{m.get('instance', '')} down"))
    return checks


def build_checks(prom: Prometheus) -> list[Check]:
    up = prom.query('up{job="jenkins"}')
    if not up or up[0][1] != 1:
        return [Check("builds", "jenkins", FAIL, "controller not scraped")]

    checks = []
    busy = prom.query("default_jenkins_executors_busy")
    if busy and busy[0][1] > 0:
        checks.append(Check("builds", "running", ACTIVE, f"{int(busy[0][1])} building"))

    for m, v in sorted(prom.query("default_jenkins_builds_last_build_result_ordinal"),
                       key=lambda r: r[0].get("jenkins_job", "")):
        state, word = JENKINS_RESULTS.get(int(v), (UNKNOWN, f"result {int(v)}"))
        checks.append(Check("builds", m.get("jenkins_job", "?"), state, word))

    if not checks:
        checks.append(Check("builds", "jenkins", OK, "up, no jobs yet"))
    return checks


def wigle_checks(prom: Prometheus, now: float | None = None) -> list[Check]:
    """Three rows: how the last sync with the Pi went, what it uploaded, and when the next one runs."""
    now = time.time() if now is None else now
    v = {m["__name__"].removeprefix("wigle_sync_"): val
         for m, val in prom.query('{__name__=~"wigle_sync_.+"}')}
    # Runs while the Pi is away don't touch last_sync_*; before wigle-sync 0.1.4
    # pushed them, the last run's own counts are the closest there is.
    uploaded = v.get("last_sync_files_uploaded", v.get("files_uploaded"))
    failed = v.get("last_sync_files_failed", v.get("files_failed", 0))
    deferred = v.get("last_sync_files_deferred", 0)
    last_at = v.get("last_pi_online_timestamp_seconds")
    when = f"{_ago(now - last_at)} ago" if last_at else ""

    running = prom.query('sum(kube_job_status_active{namespace="wigle"})')
    # files_failed is 0 on runs where the Pi is away or the internet is down, so
    # this only fires on real errors that every run for the window has hit.
    stuck = prom.query(f"min_over_time(wigle_sync_files_failed[{WIGLE_ERROR_WINDOW}])")
    if running and running[0][1] > 0:
        last = Check("wigle", "last sync", ACTIVE, "uploading now")
    elif stuck and stuck[0][1] > 0:
        last = Check("wigle", "last sync", FAIL, f"failing for {WIGLE_ERROR_WINDOW}+")
    elif not last_at:
        last = Check("wigle", "last sync", OK, "none yet")
    elif failed:
        last = Check("wigle", "last sync", WARN, f"{int(failed)} failed, {when}")
    elif deferred:
        # The internet was down; the files wait on the Pi for the next run. Not a fault.
        last = Check("wigle", "last sync", OK, f"waiting for internet, {int(deferred)} held")
    else:
        last = Check("wigle", "last sync", OK, f"OK, {when}")

    checks = [last, Check("wigle", "files uploaded", OK, "—" if uploaded is None else str(int(uploaded)))]
    nxt = prom.query('kube_cronjob_next_schedule_time{namespace="wigle",cronjob="wigle-sync"}')
    if nxt:
        checks.append(Check("wigle", "next sync", OK, _clock(nxt[0][1], now)))
    return checks


def collect(prom: Prometheus, internet: bool = True) -> list[Check]:
    """Run every check. A group whose queries fail becomes one unknown check."""
    checks = []
    for group, fn in [("builds", build_checks),
                      ("cluster", lambda p: cluster_checks(p, internet)),
                      ("wigle", wigle_checks)]:
        try:
            checks.extend(fn(prom))
        except Exception as e:  # network errors, bad responses: show them, keep going
            checks.append(Check(group, "prometheus", UNKNOWN, f"unreachable: {e}"))
    return checks


def summarize(checks: list[Check]) -> dict:
    groups = {}
    for c in checks:
        groups.setdefault(c.group, []).append(c)
    return {
        "overall": worst([c.state for c in checks]),
        "groups": {g: {"state": worst([c.state for c in cs]), "checks": [asdict(c) for c in cs]}
                   for g, cs in groups.items()},
    }


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _clock(ts: float, now: float) -> str:
    """6:00 PM today, or Thu 6:00 PM on another day, in brick9000's local time."""
    at, today = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
    hm = at.strftime("%I:%M %p").lstrip("0")
    return hm if at.date() == today.date() else f"{at:%a} {hm}"


VITALS_QUERIES = {
    "cpu": '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[2m])))',
    "mem": "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
    "temp": 'max by (instance) (node_thermal_zone_temp{type="cpu-thermal"})',
    "disk": '100 * (1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})',
}


def cluster_vitals(prom: Prometheus) -> list[dict]:
    """CPU %, memory %, CPU temperature and root disk % for each node_exporter host."""
    names = {m["instance"]: m.get("nodename", m["instance"]) for m, _ in prom.query("node_uname_info")}
    nodes: dict[str, dict] = {}
    for key, promql in VITALS_QUERIES.items():
        for m, v in prom.query(promql):
            name = names.get(m.get("instance"), m.get("instance", "?"))
            nodes.setdefault(name, {"node": name})[key] = round(v, 1)
    # brick420 before brick2000: order by the number in the name, not as text.
    return sorted(nodes.values(), key=lambda n: (len(n["node"]), n["node"]))
