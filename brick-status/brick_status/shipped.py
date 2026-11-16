"""SHIPPED, the overview's tile (brick-cicd-config#173): what reached the
homelab lately, newest first.

Sources, all cheap and already reachable from brick9000 (no Argo CD login):

- Deployment rollouts, from kube-state-metrics: every ReplicaSet a Deployment
  owns, the newest per Deployment (a rollout makes a new one), with the image
  tag its pods run. Rollouts of one image at once (overwatch's web, scanner and
  upnp) are one row, named by what their names share.
- Argo CD syncs from the last day, from the controller's sync counter
  (checks.ARGO_LAST_SYNCS): a merge to brick-k8s-config that changed no image.
  A sync next to a rollout is that rollout, so it's left out.
- brick9000 itself: its checkout's commit, and when brick9000/deploy moved to
  it (the reflog). brick1982: the deploy-brick1982 job's successful builds.

StatefulSets (Postgres, Jenkins) and brick1982's own 2-hourly timer aren't
covered: neither leaves a timestamp these sources can read.
"""

import re
import subprocess
import time
from pathlib import Path

import structlog

from .checks import ARGO_LAST_SYNCS, OK, WARN, Prometheus, _ago, build_label

log = structlog.get_logger(__name__)

ROLLOUTS = ('kube_replicaset_created * on (namespace, replicaset) group_left (owner_name) '
            'kube_replicaset_owner{owner_kind="Deployment"}')
POD_IMAGES = ('kube_pod_container_info * on (namespace, pod) group_left (owner_name) '
              'kube_pod_owner{owner_kind="ReplicaSet"}')
# Rollouts of the same image this close together are one release.
TOGETHER_SECONDS = 15 * 60
# An Argo CD sync this close to a rollout is the rollout's.
SYNC_NEAR_SECONDS = 10 * 60
DEVICE_JOBS = {"deploy-brick1982": "brick1982"}
LIMIT = 5
REPO = Path(__file__).resolve().parents[2]


def image_tag(spec: str) -> tuple[str, str]:
    """("overwatch", "0.8.7") from "docker.io/whitepatrick/overwatch:0.8.7@sha256:..."."""
    ref = spec.split("@", 1)[0]
    name, _, tag = ref.rpartition("/")[2].partition(":")
    return name, tag or "latest"


def _common_name(names: list[str]) -> str:
    """What dash-separated names share: "overwatch" from overwatch-web and overwatch-upnp."""
    parts = [n.split("-") for n in names]
    shared = []
    for words in zip(*parts):
        if len(set(words)) != 1:
            break
        shared.append(words[0])
    return "-".join(shared)


def rollouts(prom: Prometheus) -> list[dict]:
    """The newest rollout of each Deployment, releases of one image as one row."""
    newest: dict[tuple[str, str], tuple[float, str]] = {}  # (namespace, deployment) -> (created, replicaset)
    for m, created in prom.query(ROLLOUTS):
        key = (m.get("namespace", ""), m.get("owner_name", ""))
        if key[1] and (key not in newest or created > newest[key][0]):
            newest[key] = (created, m.get("replicaset", ""))
    images: dict[tuple[str, str], dict[str, str]] = {}  # (namespace, replicaset) -> container -> image
    for m, _ in prom.query(POD_IMAGES):
        spec = m.get("image_spec") or m.get("image", "")
        if m.get("owner_name") and spec:
            images.setdefault((m.get("namespace", ""), m["owner_name"]), {})[m.get("container", "")] = spec

    found = []
    for (ns, deployment), (created, replicaset) in newest.items():
        containers = images.get((ns, replicaset), {})
        # The main container: the one the Deployment is named after, else the first.
        # No pods (scaled to zero): no version.
        main = next((c for c in sorted(containers) if c and c in deployment), None) or min(containers, default=None)
        image, tag = image_tag(containers[main]) if main is not None else ("", "")
        found.append({"name": deployment, "namespace": ns, "image": image, "version": tag, "at": created})

    found.sort(key=lambda r: r["at"], reverse=True)
    rows: list[dict] = []
    for r in found:
        same = next((g for g in rows if g["namespace"] == r["namespace"] and g["image"] == r["image"]
                     and g["version"] == r["version"] and r["image"]
                     and abs(g["at"] - r["at"]) <= TOGETHER_SECONDS), None)
        if same:
            same["names"].append(r["name"])
            same["name"] = _common_name(same["names"]) or r["namespace"]
        else:
            rows.append({**r, "names": [r["name"]], "kind": "rollout"})
    return [{"name": r["name"], "version": r["version"], "at": r["at"], "kind": "rollout", "state": OK}
            for r in rows]


def argo_syncs(prom: Prometheus) -> list[dict]:
    """Each Argo CD app's last sync of the day."""
    last: dict[str, tuple[float, str]] = {}
    for m, ts in prom.query(ARGO_LAST_SYNCS):
        name = m.get("name")
        if name and (name not in last or ts > last[name][0]):
            last[name] = (ts, m.get("phase", ""))
    return [{"name": name, "version": "sync", "at": ts, "kind": "sync",
             "state": WARN if phase in ("Failed", "Error") else OK} for name, (ts, phase) in last.items()]


def device_deploys(jobs: list[dict] | None) -> list[dict]:
    """brick1982's newest successful deploy through Jenkins."""
    rows = []
    for job in jobs or []:
        host = DEVICE_JOBS.get(job.get("name"))
        build = next((b for b in job.get("builds") or [] if b.get("result") == "SUCCESS"), None) if host else None
        if build:
            rows.append({"name": host, "version": build_label(build.get("displayName")) or f"#{build.get('number')}",
                         "at": build.get("timestamp", 0) / 1000, "kind": "device", "state": OK})
    return rows


def self_deploy(repo: Path = REPO, host: str = "brick9000") -> dict | None:
    """This checkout's commit and when HEAD last moved to it (brick9000/deploy
    checks commits out), read once at start: every deploy restarts brick-status."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "log", "-g", "-1", "--format=%h%x09%gd%x09%ct", "--date=unix"],
                             capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("own deploy unknown", error=str(e))
        return None
    commit, _, rest = out.partition("\t")
    moved, _, committed = rest.partition("\t")
    at = re.search(r"\{(\d+)\}", moved)
    when = float(at[1]) if at else float(committed) if committed.isdigit() else None
    if not commit or when is None:
        return None
    return {"name": host, "version": commit, "at": when, "kind": "device", "state": OK}


def shipped(prom: Prometheus, jobs: list[dict] | None, own: dict | None = None, now: float | None = None,
            limit: int = LIMIT) -> list[dict]:
    """The newest `limit` arrivals from every source, each with its age. A
    source that can't be read is left out (the other tiles say why)."""
    now = time.time() if now is None else now
    rows: list[dict] = []
    try:
        rows += rollouts(prom)
    except (OSError, RuntimeError, ValueError, KeyError) as e:
        log.warning("shipped rollouts unavailable", error=str(e))
    try:
        rolled = [r["at"] for r in rows]
        rows += [s for s in argo_syncs(prom) if not any(abs(s["at"] - at) <= SYNC_NEAR_SECONDS for at in rolled)]
    except (OSError, RuntimeError, ValueError, KeyError) as e:
        log.warning("shipped syncs unavailable", error=str(e))
    rows += device_deploys(jobs)
    if own:
        rows.append(dict(own))
    rows.sort(key=lambda r: r["at"], reverse=True)
    return [{**r, "age": _ago(now - r["at"])} for r in rows[:limit]]
