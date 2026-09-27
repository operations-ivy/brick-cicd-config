"""Turn Prometheus query results into board checks.

Each check has a state: ok, warn, fail, active (something in progress, like a
build or a wigle-sync upload), or unknown (no data).
"""

import json
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

OK, WARN, FAIL, ACTIVE, UNKNOWN = "ok", "warn", "fail", "active", "unknown"
# Worst first, for rolling a group up into one state.
SEVERITY = [FAIL, WARN, UNKNOWN, ACTIVE, OK]

# Jenkins prometheus plugin's last_build_result_ordinal values.
JENKINS_RESULTS = {0: (OK, "success"), 1: (WARN, "unstable"), 2: (FAIL, "failure"),
                   3: (UNKNOWN, "not built"), 4: (WARN, "aborted")}

# wigle-sync runs hourly; two missed runs is worth flagging.
WIGLE_STALE_SECONDS = 2 * 3600 + 600


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


def cluster_checks(prom: Prometheus) -> list[Check]:
    checks = []
    for m, v in prom.query('kube_node_status_condition{condition="Ready",status="true"}'):
        checks.append(Check("cluster", m["node"], OK if v == 1 else FAIL,
                            "Ready" if v == 1 else "NotReady"))

    unavailable = prom.query("kube_deployment_status_replicas_unavailable > 0")
    unready_sts = prom.query(
        "kube_statefulset_replicas - kube_statefulset_status_replicas_ready > 0")
    for m, v in unavailable + unready_sts:
        name = m.get("deployment") or m.get("statefulset")
        checks.append(Check("cluster", f"{m['namespace']}/{name}", FAIL, f"{int(v)} not ready"))
    if not unavailable and not unready_sts:
        checks.append(Check("cluster", "workloads", OK, "all replicas ready"))

    stuck = prom.query('sum by (namespace, pod, reason) (kube_pod_container_status_waiting_reason'
                       '{reason=~"CrashLoopBackOff|ImagePullBackOff|ErrImagePull|CreateContainerConfigError"}) > 0')
    for m, _ in stuck:
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
    now = time.time() if now is None else now
    checks = []

    running = prom.query('sum(kube_job_status_active{namespace="wigle"})')
    if running and running[0][1] > 0:
        checks.append(Check("wigle", "sync", ACTIVE, "uploading"))

    last_ok = prom.query("wigle_sync_last_success_timestamp_seconds")
    if not last_ok:
        checks.append(Check("wigle", "last success", UNKNOWN, "no runs recorded"))
    else:
        age = now - last_ok[0][1]
        state = WARN if age > WIGLE_STALE_SECONDS else OK
        checks.append(Check("wigle", "last success", state, f"{_ago(age)} ago"))

    online = prom.query("wigle_sync_pi_online")
    if online:
        checks.append(Check("wigle", "pwnagotchi", OK if online[0][1] == 1 else WARN,
                            "online" if online[0][1] == 1 else "offline"))

    failed = prom.query("wigle_sync_files_failed")
    uploaded = prom.query("wigle_sync_files_uploaded")
    if failed and failed[0][1] > 0:
        checks.append(Check("wigle", "last run", WARN, f"{int(failed[0][1])} files failed"))
    elif uploaded:
        checks.append(Check("wigle", "last run", OK, f"{int(uploaded[0][1])} files uploaded"))
    return checks


def collect(prom: Prometheus) -> list[Check]:
    """Run every check. A group whose queries fail becomes one unknown check."""
    checks = []
    for group, fn in [("builds", build_checks), ("cluster", cluster_checks), ("wigle", wigle_checks)]:
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
