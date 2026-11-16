"""NIGHT SHIFT, button 3's screen (brick-cicd-config#173): the homelab's
scheduled work, each with its last run, how long that took, the next run, and
progress where a metric for it exists.

- The cluster's CronJobs, from kube-state-metrics: the CronJob's own last
  schedule, last success and next schedule, and its newest Job's start,
  completion and failures (Jobs a TTL has removed fall back on the CronJob's
  times). Suspended CronJobs are left out.
- Jenkins' timers (board.yaml's night_shift.jenkins: job and how often it
  runs), from the builds recent_jobs already fetched: the next run is the last
  start plus the period (Jenkins' H spreads a job's minute, but keeps it).
- Extras per CronJob in board.yaml's night_shift.cronjobs: a PromQL `took` for
  a Job whose Job object is gone, and a `progress` (`done`, optional `total`,
  `label`) such as brick-games-scan's box art from its Pushgateway series.
"""

import re
import time
from collections import defaultdict

import structlog

from .checks import ACTIVE, FAIL, JENKINS_RESULTS, OK, QUERY_ERRORS, UNKNOWN, WARN, Prometheus, ago

log = structlog.get_logger(__name__)

# Two queries for every CronJob and Job kube-state-metrics knows about.
CRONJOB_SERIES = ('{__name__=~"kube_cronjob_(info|spec_suspend|status_active|status_last_schedule_time'
                  '|status_last_successful_time|next_schedule_time)"}')
JOB_SERIES = ('{__name__=~"kube_job_(owner|status_active|status_failed|status_start_time'
              '|status_completion_time)"}')

# Running first, then trouble, then the rest by their next run.
ORDER = {ACTIVE: 0, FAIL: 1, WARN: 2, UNKNOWN: 3, OK: 4}


def period_seconds(text: str | int | float) -> float | None:
    """Seconds in a period such as 24h, 2h, 30m, 1d or a plain number; None if it isn't one."""
    if isinstance(text, (int, float)):
        return float(text)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(text))
    if not m:
        return None
    return float(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]


def next_text(next_at: float | None, now: float, busy: bool) -> str:
    """When it runs next: in 13m, due (past, not started), or busy (still on the last one)."""
    if next_at is None:
        return ""
    if next_at < now:
        return "busy" if busy else "due"
    return f"in {ago(next_at - now)}"


def _ms(value: float | None) -> float:
    """Seconds from Jenkins' milliseconds."""
    return (value or 0) / 1000


def _value(prom: Prometheus, promql: str) -> float | None:
    """The sum of a query's values, or None for no data."""
    result = prom.query(promql)
    return sum(v for _, v in result) if result else None


def progress(prom: Prometheus, spec: dict) -> dict | None:
    """A CronJob's progress from its board.yaml `progress`: label, done, total and percent."""
    try:
        done = _value(prom, spec["done"])
        total = _value(prom, spec["total"]) if spec.get("total") else None
    except (*QUERY_ERRORS, TypeError) as e:
        log.warning("night shift progress query failed", error=str(e))
        return None
    if done is None:
        return None
    pct = round(100 * done / total) if total else None
    return {"label": str(spec.get("label", "")), "done": done, "total": total, "pct": pct}


def _row(name: str, kind: str, state: str, *, last: str, at: float | None = None, took: float | None = None,
         next_at: float | None = None, now: float, busy: bool = False) -> dict:
    return {"name": name, "kind": kind, "state": state, "last": last, "at": at,
            "took": "" if took is None else ago(took), "next": next_text(next_at, now, busy),
            "next_at": next_at, "progress": None}


def _cronjob_series(prom: Prometheus) -> dict[tuple[str, str], dict[str, float]]:
    """(namespace, cronjob) -> its kube_cronjob_* values, by the name after the prefix."""
    cronjobs: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for m, v in prom.query(CRONJOB_SERIES):
        if m.get("cronjob"):
            cronjobs[(m.get("namespace", ""), m["cronjob"])][m["__name__"].removeprefix("kube_cronjob_")] = v
    return cronjobs


def _job_series(prom: Prometheus) -> dict[tuple[str, str], list[dict[str, float]]]:
    """(namespace, cronjob) -> each of its Jobs' kube_job_status_* values."""
    owners: dict[tuple[str, str], str] = {}  # (namespace, job) -> the CronJob that made it
    values: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for m, v in prom.query(JOB_SERIES):
        if not m.get("job_name"):
            continue
        key = (m.get("namespace", ""), m["job_name"])
        if m["__name__"] == "kube_job_owner":
            if m.get("owner_kind") == "CronJob" and m.get("owner_name"):
                owners[key] = m["owner_name"]
        else:
            values[key][m["__name__"].removeprefix("kube_job_status_")] = v
    by_cronjob: dict[tuple[str, str], list[dict[str, float]]] = defaultdict(list)
    for (ns, job), owner in owners.items():
        if "start_time" in values[(ns, job)]:
            by_cronjob[(ns, owner)].append(values[(ns, job)])
    return by_cronjob


def _classify(c: dict[str, float], jobs: list[dict[str, float]], now: float) -> dict:
    """A CronJob's state, last run, when, how long it took, and whether it's running."""
    last_sched = c.get("status_last_schedule_time")
    last_ok = c.get("status_last_successful_time")
    newest = max(jobs, key=lambda j: j["start_time"], default=None)
    # The newest Job is the last run unless a later one has been and gone (a TTL).
    if newest and last_sched and newest["start_time"] < last_sched - 60:
        newest = None
    if c.get("status_active", 0) > 0:
        start = max((j["start_time"] for j in jobs if j.get("active", 0) > 0), default=last_sched or now)
        return {"state": ACTIVE, "last": f"run {ago(now - start)}", "at": start, "took": None, "busy": True}
    if newest and newest.get("failed", 0) > 0 and not newest.get("completion_time"):
        return {"state": FAIL, "last": f"failed {ago(now - newest['start_time'])}", "at": newest["start_time"],
                "took": None, "busy": False}
    if newest and newest.get("completion_time"):
        at = newest["completion_time"]
        return {"state": OK, "last": f"{ago(now - at)} ago", "at": at, "took": at - newest["start_time"],
                "busy": False}
    if last_ok and (not last_sched or last_ok >= last_sched):
        return {"state": OK, "last": f"{ago(now - last_ok)} ago", "at": last_ok, "took": None, "busy": False}
    if last_sched:
        # Run, not running, and no success since: its Job failed and is gone.
        return {"state": WARN, "last": f"no ok {ago(now - last_sched)}", "at": last_sched, "took": None,
                "busy": False}
    return {"state": OK, "last": "not yet", "at": None, "took": None, "busy": False}


def cronjob_rows(prom: Prometheus, extras: dict | None = None, now: float | None = None) -> list[dict]:
    """One row per CronJob that isn't suspended."""
    now = time.time() if now is None else now
    extras = extras or {}
    jobs = _job_series(prom)
    rows = []
    for (ns, name), c in _cronjob_series(prom).items():
        if c.get("spec_suspend") == 1:
            continue
        run = _classify(c, jobs.get((ns, name), []), now)
        extra = extras.get(name) or {}
        took = run["took"]
        if took is None and not run["busy"] and extra.get("took"):
            try:
                took = _value(prom, extra["took"])
            except QUERY_ERRORS as e:
                log.warning("night shift took query failed", cronjob=name, error=str(e))
        row = _row(name, "cronjob", run["state"], last=run["last"], at=run["at"], took=took,
                   next_at=c.get("next_schedule_time"), now=now, busy=run["busy"])
        if extra.get("progress"):
            row["progress"] = progress(prom, extra["progress"])
        rows.append(row)
    return rows


def jenkins_rows(jobs: list[dict] | None, timers: dict | None, now: float | None = None) -> list[dict]:
    """One row per Jenkins timer job named in board.yaml, from recent_jobs' builds."""
    now = time.time() if now is None else now
    if not timers:
        return []
    if jobs is None:
        return [_row("jenkins", "jenkins", UNKNOWN, last="no data", now=now)]
    by_name = {j["name"]: j for j in jobs}
    rows = []
    for name, every in timers.items():
        builds = (by_name.get(name) or {}).get("builds") or []
        if not builds:
            rows.append(_row(name, "jenkins", OK, last="not yet", now=now))
            continue
        first = builds[0]
        started = _ms(first.get("timestamp"))
        period = period_seconds(every)
        next_at = started + period if period else None
        if first.get("building"):
            rows.append(_row(name, "jenkins", ACTIVE, last=f"run {ago(now - started)}", at=started,
                             next_at=next_at, now=now, busy=True))
            continue
        done = next((b for b in builds if not b.get("building")), first)
        at = _ms(done.get("timestamp"))
        state, word = JENKINS_RESULTS.get(done.get("result"), (UNKNOWN, str(done.get("result")).lower()))
        last = f"{ago(now - at)} ago" if state == OK else f"{word} {ago(now - at)}"
        rows.append(_row(name, "jenkins", state, last=last, at=at, took=_ms(done.get("duration")),
                         next_at=next_at, now=now))
    return rows


def rows(prom: Prometheus, jobs: list[dict] | None, config: dict | None, now: float | None = None) -> list[dict]:
    """Every row, running and failing first, then by next run. A Prometheus
    outage is one unknown row; Jenkins' rows still show."""
    now = time.time() if now is None else now
    config = config or {}
    try:
        out = cronjob_rows(prom, config.get("cronjobs"), now)
    except QUERY_ERRORS as e:
        log.warning("night shift cronjobs unavailable", error=str(e))
        out = [_row("cronjobs", "cronjob", UNKNOWN, last="no data", now=now)]
    out += jenkins_rows(jobs, config.get("jenkins"), now)
    far = float("inf")
    return sorted(out, key=lambda r: (ORDER.get(r["state"], len(ORDER)),
                                      far if r["next_at"] is None else r["next_at"], r["name"]))
