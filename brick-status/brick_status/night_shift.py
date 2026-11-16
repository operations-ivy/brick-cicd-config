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

import structlog

from .checks import ACTIVE, FAIL, JENKINS_RESULTS, OK, UNKNOWN, WARN, Prometheus, _ago

log = structlog.get_logger(__name__)

# Two queries for every CronJob and Job kube-state-metrics knows about.
CRONJOB_SERIES = ('{__name__=~"kube_cronjob_(info|spec_suspend|status_active|status_last_schedule_time'
                  '|status_last_successful_time|next_schedule_time)"}')
JOB_SERIES = ('{__name__=~"kube_job_(owner|status_active|status_failed|status_start_time'
              '|status_completion_time)"}')

# Running first, then trouble, then the rest by their next run.
ORDER = [ACTIVE, FAIL, WARN, UNKNOWN, OK]


def period_seconds(text: str | int | float) -> float | None:
    """"24h", "2h", "30m", "1d" or plain seconds; None if it isn't one."""
    if isinstance(text, (int, float)):
        return float(text)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(text))
    if not m:
        return None
    return float(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]


def next_text(next_at: float | None, now: float, busy: bool) -> str:
    """When it runs next: "in 13m", "due" (past, not started), "busy" (still on the last one)."""
    if next_at is None:
        return ""
    if next_at < now:
        return "busy" if busy else "due"
    return f"in {_ago(next_at - now)}"


def _value(prom: Prometheus, promql: str) -> float | None:
    """The sum of a query's values, or None for no data."""
    result = prom.query(promql)
    return sum(v for _, v in result) if result else None


def progress(prom: Prometheus, spec: dict) -> dict | None:
    """A CronJob's progress from its board.yaml `progress`: label, done, total and percent."""
    try:
        done = _value(prom, spec["done"])
        total = _value(prom, spec["total"]) if spec.get("total") else None
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as e:
        log.warning("night shift progress query failed", error=str(e))
        return None
    if done is None:
        return None
    pct = round(100 * done / total) if total else None
    return {"label": str(spec.get("label", "")), "done": done, "total": total, "pct": pct}


def _row(name: str, kind: str, state: str, last: str, at: float | None, took: float | None,
         next_at: float | None, now: float, busy: bool = False) -> dict:
    return {"name": name, "kind": kind, "state": state, "last": last, "at": at,
            "took": "" if took is None else _ago(took), "next": next_text(next_at, now, busy),
            "next_at": next_at, "progress": None}


def cronjob_rows(prom: Prometheus, extras: dict | None = None, now: float | None = None) -> list[dict]:
    """One row per CronJob that isn't suspended."""
    now = time.time() if now is None else now
    extras = extras or {}
    cronjobs: dict[tuple[str, str], dict[str, float]] = {}
    for m, v in prom.query(CRONJOB_SERIES):
        if m.get("cronjob"):
            key = (m.get("namespace", ""), m["cronjob"])
            cronjobs.setdefault(key, {})[m["__name__"].removeprefix("kube_cronjob_")] = v
    jobs: dict[tuple[str, str], dict] = {}
    for m, v in prom.query(JOB_SERIES):
        if not m.get("job_name"):
            continue
        job = jobs.setdefault((m.get("namespace", ""), m["job_name"]), {})
        if m["__name__"] == "kube_job_owner":
            if m.get("owner_kind") == "CronJob":
                job["owner"] = m.get("owner_name")
        else:
            job[m["__name__"].removeprefix("kube_job_status_")] = v

    rows = []
    for (ns, name), c in sorted(cronjobs.items(), key=lambda item: item[0][1]):
        if c.get("spec_suspend") == 1:
            continue
        own = [j for (jns, _), j in jobs.items() if jns == ns and j.get("owner") == name and "start_time" in j]
        newest = max(own, key=lambda j: j["start_time"], default=None)
        last_sched = c.get("status_last_schedule_time")
        last_ok = c.get("status_last_successful_time")
        next_at = c.get("next_schedule_time")
        # The newest Job is the last run unless a later one has been and gone (a TTL).
        if newest and last_sched and newest["start_time"] < last_sched - 60:
            newest = None
        busy = c.get("status_active", 0) > 0
        took = None
        if busy:
            running = [j["start_time"] for j in own if j.get("active", 0) > 0]
            start = max(running, default=last_sched or now)
            state, last, at = ACTIVE, f"run {_ago(now - start)}", start
        elif newest and newest.get("failed", 0) > 0 and not newest.get("completion_time"):
            state, last, at = FAIL, f"failed {_ago(now - newest['start_time'])}", newest["start_time"]
        elif newest and newest.get("completion_time"):
            at = newest["completion_time"]
            state, last, took = OK, f"{_ago(now - at)} ago", at - newest["start_time"]
        elif last_ok and (not last_sched or last_ok >= last_sched):
            state, last, at = OK, f"{_ago(now - last_ok)} ago", last_ok
        elif last_sched:
            # Run, not running, and no success since: its Job failed and is gone.
            state, last, at = WARN, f"no ok {_ago(now - last_sched)}", last_sched
        else:
            state, last, at = OK, "not yet", None
        extra = extras.get(name) or {}
        if took is None and not busy and extra.get("took"):
            try:
                took = _value(prom, extra["took"])
            except (OSError, RuntimeError, ValueError) as e:
                log.warning("night shift took query failed", cronjob=name, error=str(e))
        row = _row(name, "cronjob", state, last, at, took, next_at, now, busy)
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
        return [_row("jenkins", "jenkins", UNKNOWN, "no data", None, None, None, now)]
    by_name = {j["name"]: j for j in jobs}
    rows = []
    for name, every in timers.items():
        builds = (by_name.get(name) or {}).get("builds") or []
        period = period_seconds(every)
        if not builds:
            rows.append(_row(name, "jenkins", OK, "not yet", None, None, None, now))
            continue
        first = builds[0]
        started = first.get("timestamp", 0) / 1000
        next_at = started + period if period else None
        if first.get("building"):
            rows.append(_row(name, "jenkins", ACTIVE, f"run {_ago(now - started)}", started, None,
                             next_at, now, busy=True))
            continue
        done = next((b for b in builds if not b.get("building")), first)
        at = done.get("timestamp", 0) / 1000
        state, word = JENKINS_RESULTS.get(done.get("result"), (UNKNOWN, str(done.get("result")).lower()))
        last = f"{_ago(now - at)} ago" if state == OK else f"{word} {_ago(now - at)}"
        rows.append(_row(name, "jenkins", state, last, at, (done.get("duration") or 0) / 1000, next_at, now))
    return rows


def rows(prom: Prometheus, jobs: list[dict] | None, config: dict | None, now: float | None = None) -> list[dict]:
    """Every row, running and failing first, then by next run. A Prometheus
    outage is one unknown row; Jenkins' rows still show."""
    now = time.time() if now is None else now
    config = config or {}
    try:
        out = cronjob_rows(prom, config.get("cronjobs"), now)
    except (OSError, RuntimeError, ValueError, KeyError) as e:
        log.warning("night shift cronjobs unavailable", error=str(e))
        out = [_row("cronjobs", "cronjob", UNKNOWN, "no data", None, None, None, now)]
    out += jenkins_rows(jobs, config.get("jenkins"), now)
    far = float("inf")
    return sorted(out, key=lambda r: (ORDER.index(r["state"]) if r["state"] in ORDER else len(ORDER),
                                      r["next_at"] if r["next_at"] is not None else far, r["name"]))
