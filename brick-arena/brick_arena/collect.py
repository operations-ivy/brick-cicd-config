"""Turn Prometheus results into what brick1982's screen draws.

Three views share one snapshot:
- arena: every node with its vitals, and the pods running on it. A pod that
  comes back on another node (same workload, new pod) is a move, kept for a
  while so the page can animate it and list it.
- weather: the network as weather. Wind is the nodes' Wi-Fi throughput, rain
  is CoreDNS queries, and lightning is a pod restarting or a new pod starting.
- radar: wardriving contacts from a local JSON file (radar_contacts), plotted
  around home.
"""

import json
import math
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

# How long a move or a lightning strike stays in the snapshot.
EVENT_SECONDS = 600

VITALS_QUERIES = {
    "cpu": '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[2m])))',
    "mem": "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
    "temp": 'max by (instance) (node_thermal_zone_temp{type="cpu-thermal"})',
}
WIND_QUERY = ('sum by (instance) (rate(node_network_receive_bytes_total{device=~"wlan.*|eth.*|end.*"}[2m])'
              ' + rate(node_network_transmit_bytes_total{device=~"wlan.*|eth.*|end.*"}[2m]))')
RAIN_QUERY = "sum(rate(coredns_dns_requests_total[2m]))"

# Pod name suffixes: a ReplicaSet's hash, and the random tail every
# controller-made pod name ends with.
RS_HASH = re.compile(r"-[a-z0-9]{6,10}$")


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


@dataclass
class Pod:
    namespace: str
    name: str
    node: str
    workload: str
    kind: str
    ready: bool = True
    restarts: int = 0


@dataclass
class Event:
    kind: str  # "move", "restart" or "new"
    workload: str
    at: float
    src: str = ""
    dst: str = ""


def workload_of(metric: dict) -> tuple[str, str]:
    """(kind, name) of the controller behind a kube_pod_info series.

    A Deployment's pods are created by a ReplicaSet named <deployment>-<hash>,
    so the hash comes off to keep one name across rollouts. DaemonSet pods are
    one per node and never move, so the node is part of their name.
    """
    kind = metric.get("created_by_kind", "")
    owner = metric.get("created_by_name", "")
    ns = metric.get("namespace", "")
    if kind == "ReplicaSet":
        return "Deployment", f"{ns}/{RS_HASH.sub('', owner)}"
    if kind == "DaemonSet":
        return kind, f"{ns}/{owner}@{metric.get('node', '')}"
    if kind in ("StatefulSet", "Job"):
        # A StatefulSet keeps the pod name; a Job's pods are the Job's.
        return kind, f"{ns}/{metric['pod'] if kind == 'StatefulSet' else owner}"
    return kind or "Pod", f"{ns}/{metric.get('pod', '')}"


def node_order(name: str) -> tuple[int, str]:
    # brick420 before brick1982 before brick2000: by the number, not as text.
    digits = re.sub(r"\D", "", name)
    return (int(digits) if digits else 0, name)


def nodes(prom) -> list[dict]:
    ready = {m["node"]: v == 1 for m, v in
             prom.query('kube_node_status_condition{condition="Ready",status="true"}')}
    names = {m["instance"]: m.get("nodename", m["instance"]) for m, _ in prom.query("node_uname_info")}
    out = {n: {"node": n, "ready": r} for n, r in ready.items()}
    for key, promql in VITALS_QUERIES.items():
        for m, v in prom.query(promql):
            name = names.get(m.get("instance"), m.get("instance", "?"))
            if name in out:
                out[name][key] = round(v, 1)
    return sorted(out.values(), key=lambda n: node_order(n["node"]))


def pods(prom) -> list[Pod]:
    ready = {(m["namespace"], m["pod"]) for m, v in
             prom.query('kube_pod_status_ready{condition="true"}') if v == 1}
    restarts = {(m["namespace"], m["pod"]): int(v) for m, v in
                prom.query("sum by (namespace, pod) (kube_pod_container_status_restarts_total)")}
    # Succeeded and Failed pods (finished CronJob runs) aren't running anywhere.
    done = {(m["namespace"], m["pod"]) for m, v in
            prom.query('kube_pod_status_phase{phase=~"Succeeded|Failed"}') if v == 1}
    out = []
    for m, _ in prom.query("kube_pod_info"):
        key = (m.get("namespace", ""), m.get("pod", ""))
        if key in done or not m.get("node"):
            continue
        kind, workload = workload_of(m)
        out.append(Pod(key[0], key[1], m["node"], workload, kind,
                       ready=key in ready, restarts=restarts.get(key, 0)))
    return sorted(out, key=lambda p: (p.workload, p.name))


@dataclass
class Tracker:
    """Remembers the last snapshot to turn differences into events."""
    where: dict[str, str] = field(default_factory=dict)      # workload -> node
    restarts: dict[str, int] = field(default_factory=dict)   # pod name -> count
    seen: set[str] = field(default_factory=set)              # pod names
    events: list[Event] = field(default_factory=list)
    primed: bool = False

    def update(self, current: list[Pod], now: float) -> list[Event]:
        new_events = []
        names = set()
        for p in current:
            key = f"{p.namespace}/{p.name}"
            names.add(key)
            if not self.primed:
                continue
            # A workload's new pod landing on another node is a move, reported
            # as it lands (during a rollout the old pod is still running).
            old_node = self.where.get(p.workload)
            if old_node and old_node != p.node and key not in self.seen:
                new_events.append(Event("move", p.workload, now, old_node, p.node))
            elif key not in self.seen:
                new_events.append(Event("new", p.workload, now, dst=p.node))
            if p.restarts > self.restarts.get(key, p.restarts):
                new_events.append(Event("restart", p.workload, now, dst=p.node))
        for p in current:
            key = f"{p.namespace}/{p.name}"
            if key not in self.seen or p.workload not in self.where:
                self.where[p.workload] = p.node
            self.restarts[key] = p.restarts
        self.seen = names
        self.primed = True
        self.events = [e for e in self.events if now - e.at < EVENT_SECONDS] + new_events
        return new_events


def weather(prom) -> dict:
    wind = sum(v for _, v in prom.query(WIND_QUERY))
    rain = sum(v for _, v in prom.query(RAIN_QUERY))
    return {"wind_bps": round(wind), "rain_qps": round(rain, 3), "forecast": forecast(wind, rain)}


def forecast(wind_bps: float, rain_qps: float) -> str:
    """A one-line forecast, as the page's caption."""
    mbps = wind_bps * 8 / 1e6
    if mbps < 0.5:
        wind = "Calm"
    elif mbps < 5:
        wind = "Light breeze"
    elif mbps < 25:
        wind = "Windy"
    else:
        wind = "Gale"
    if rain_qps < 0.05:
        rain = "dry"
    elif rain_qps < 1:
        rain = "drizzle"
    elif rain_qps < 10:
        rain = "showers"
    else:
        rain = "downpour"
    return f"{wind}, {rain}"


def radar_contacts(path: Path, home: tuple[float, float] | None, now: float,
                   radius_m: float = 3000) -> dict:
    """Contacts from a JSON list of {"lat", "lon", "seen", "kind"}, as bearing
    (degrees from north) and distance (0..1 of radius_m) from home.

    Nothing about a contact but where and when is kept: the screen doesn't
    show network names. Without home or the file there's an empty scope.
    """
    if home is None or not path.is_file():
        return {"contacts": [], "total": 0, "feed": False}
    try:
        rows = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"contacts": [], "total": 0, "feed": False}
    lat0, lon0 = map(math.radians, home)
    contacts = []
    for r in rows:
        try:
            lat, lon = math.radians(float(r["lat"])), math.radians(float(r["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        # Equirectangular is plenty at a few km.
        x = (lon - lon0) * math.cos((lat + lat0) / 2) * 6371000
        y = (lat - lat0) * 6371000
        dist = math.hypot(x, y)
        if dist > radius_m:
            continue
        contacts.append({"bearing": round(math.degrees(math.atan2(x, y)) % 360, 1),
                         "range": round(dist / radius_m, 3),
                         "age": max(0, round(now - float(r.get("seen", now)))),
                         "kind": r.get("kind", "wifi")})
    return {"contacts": contacts, "total": len(rows), "feed": True}


def snapshot(prom, tracker: Tracker, radar_file: Path, home, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    current = pods(prom)
    tracker.update(current, now)
    return {
        "nodes": nodes(prom),
        "pods": [asdict(p) for p in current],
        "events": [asdict(e) for e in tracker.events],
        "weather": weather(prom),
        "radar": radar_contacts(radar_file, home, now),
    }
