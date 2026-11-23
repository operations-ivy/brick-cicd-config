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
  it (the reflog). brick1982: the deploy-brick1982 (LCARS) and deploy-brick-panel (neon) jobs'
  successful builds.

StatefulSets (Postgres, Jenkins) and brick1982's own 2-hourly timer aren't
covered: neither leaves a timestamp these sources can read.
"""

import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import structlog

from .checks import ARGO_LAST_SYNCS, OK, QUERY_ERRORS, WARN, Prometheus, ago, build_label

log = structlog.get_logger(__name__)

ROLLOUTS = ('kube_replicaset_created * on (namespace, replicaset) group_left (owner_name) '
            'kube_replicaset_owner{owner_kind="Deployment"}')
POD_IMAGES = ('kube_pod_container_info * on (namespace, pod) group_left (owner_name) '
              'kube_pod_owner{owner_kind="ReplicaSet"}')
# Rollouts of the same image this close together are one release.
TOGETHER_SECONDS = 15 * 60
# An Argo CD sync this close to a rollout is the rollout's.
SYNC_NEAR_SECONDS = 10 * 60
# brick1982 runs one of two panels; each has its own deploy job.
DEVICE_JOBS = {"deploy-brick1982": "brick1982 lcars", "deploy-brick-panel": "brick1982 neon"}
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


def _newest_replicasets(prom: Prometheus) -> dict[tuple[str, str], tuple[float, str]]:
    """(namespace, deployment) -> (created, replicaset) of each Deployment's newest ReplicaSet."""
    newest: dict[tuple[str, str], tuple[float, str]] = {}
    for m, created in prom.query(ROLLOUTS):
        key = (m.get("namespace", ""), m.get("owner_name", ""))
        if key[1] and (key not in newest or created > newest[key][0]):
            newest[key] = (created, m.get("replicaset", ""))
    return newest


def _pod_images(prom: Prometheus) -> dict[tuple[str, str], dict[str, str]]:
    """(namespace, replicaset) -> container -> image its pods run."""
    images: dict[tuple[str, str], dict[str, str]] = {}
    for m, _ in prom.query(POD_IMAGES):
        spec = m.get("image_spec") or m.get("image", "")
        if m.get("owner_name") and spec:
            images.setdefault((m.get("namespace", ""), m["owner_name"]), {})[m.get("container", "")] = spec
    return images


def _same_release(group: dict, rollout: dict) -> bool:
    """One image, in one namespace, rolled out to both within TOGETHER_SECONDS."""
    return (bool(rollout["image"])
            and (group["namespace"], group["image"], group["version"])
            == (rollout["namespace"], rollout["image"], rollout["version"])
            and abs(group["at"] - rollout["at"]) <= TOGETHER_SECONDS)


def _group_rollouts(found: list[dict]) -> list[dict]:
    """Rollouts (name, namespace, image, version, at), newest first, releases of
    one image as one row named by what the Deployments' names share."""
    groups: list[dict] = []
    for r in sorted(found, key=lambda r: r["at"], reverse=True):
        group = next((g for g in groups if _same_release(g, r)), None)
        if group:
            group["names"].append(r["name"])
            group["name"] = _common_name(group["names"]) or r["namespace"]
        else:
            groups.append({**r, "names": [r["name"]]})
    return [{"name": g["name"], "version": g["version"], "at": g["at"], "kind": "rollout", "state": OK}
            for g in groups]


def rollouts(prom: Prometheus) -> list[dict]:
    """The newest rollout of each Deployment, releases of one image as one row."""
    images = _pod_images(prom)
    found = []
    for (ns, deployment), (created, replicaset) in _newest_replicasets(prom).items():
        containers = images.get((ns, replicaset), {})
        # The main container: the one the Deployment is named after, else the first.
        # No pods (scaled to zero): no version.
        main = next((c for c in sorted(containers) if c and c in deployment), None) or min(containers, default=None)
        image, tag = image_tag(containers[main]) if main is not None else ("", "")
        found.append({"name": deployment, "namespace": ns, "image": image, "version": tag, "at": created})
    return _group_rollouts(found)


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
        if not host:
            continue
        build = next((b for b in job.get("builds") or [] if b.get("result") == "SUCCESS"), None)
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
    fields = out.split("\t")
    if len(fields) != 3 or not fields[0]:
        return None
    commit, moved, committed = fields
    if at := re.search(r"\{(\d+)\}", moved):  # HEAD@{<unix time>}: when HEAD moved here
        when = float(at[1])
    elif committed.isdigit():  # no reflog: the commit's own time
        when = float(committed)
    else:
        return None
    return {"name": host, "version": commit, "at": when, "kind": "device", "state": OK}


def _safely(label: str, fn: Callable[..., list[dict]], *args) -> list[dict]:
    """fn's rows, or none (logged) when its source can't be read."""
    try:
        return fn(*args)
    except QUERY_ERRORS as e:
        log.warning("shipped source unavailable", source=label, error=str(e))
        return []


def shipped(prom: Prometheus, jobs: list[dict] | None, own: dict | None = None, now: float | None = None,
            limit: int = LIMIT) -> list[dict]:
    """The newest `limit` arrivals from every source, each with its age. A
    source that can't be read is left out (the other tiles say why)."""
    now = time.time() if now is None else now
    rows = _safely("rollouts", rollouts, prom)
    rollout_times = [r["at"] for r in rows]
    rows += [s for s in _safely("syncs", argo_syncs, prom)
             if not any(abs(s["at"] - at) <= SYNC_NEAR_SECONDS for at in rollout_times)]
    rows += device_deploys(jobs)
    if own:
        rows.append(dict(own))
    rows.sort(key=lambda r: r["at"], reverse=True)
    return [{**r, "age": ago(now - r["at"])} for r in rows[:limit]]
