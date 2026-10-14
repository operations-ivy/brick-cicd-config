"""Record how a oneshot user unit's run went, for node-exporter's textfile
collector: run from the unit's ExecStopPost, which systemd gives the
result in $SERVICE_RESULT and $EXIT_STATUS.

    ExecStopPost=/usr/bin/env PYTHONPATH=%h/brick-cicd-config/brick-status \\
        python3 -m brick_status.jobs brick-roms

Writes <dir>/brick-job-<name>.prom with the last run's time, result and
duration, and the last success (kept from the previous file across failures).
With --git, also the checkout's branch and commit (the deploy units).
node-exporter's systemd collector can't see user units, so this is how their
runs reach Prometheus.
"""

import argparse
import os
import re
import subprocess
import sys
import time

from .metrics import Registry, write_textfile

TEXTFILE_DIR = "/var/lib/node-exporter-textfile"


def unit_seconds(unit: str) -> float | None:
    """How long the unit's main process ran, from systemd's own timestamps."""
    try:
        out = subprocess.run(["systemctl", "--user", "show", unit, "--value",
                              "-p", "ExecMainStartTimestampMonotonic",
                              "-p", "ExecMainExitTimestampMonotonic"],
                             capture_output=True, text=True, timeout=10).stdout.split()
        start, end = (int(v) for v in out[:2])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return (end - start) / 1e6 if start and end >= start else None


def previous_success(path: str, name: str) -> float:
    try:
        text = open(path).read()
    except OSError:
        return 0.0
    m = re.search(r'^brick_job_last_success_timestamp_seconds\{unit="%s"\} (\S+)$' % re.escape(name), text, re.M)
    return float(m.group(1)) if m else 0.0


def git_info(checkout: str) -> tuple[str, str] | None:
    def git(*args):
        return subprocess.run(["git", "-C", checkout, *args], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    try:
        return git("rev-parse", "--abbrev-ref", "HEAD"), git("rev-parse", "--short", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return None


def render(name: str, now: float, ok: bool, seconds: float | None, last_success: float,
           git: tuple[str, str] | None = None) -> str:
    r = Registry()
    r.gauge("brick_job_last_run_timestamp_seconds", "When the unit's last run ended.", ["unit"]).set(now, unit=name)
    r.gauge("brick_job_last_success", "1 if the unit's last run succeeded.", ["unit"]).set(int(ok), unit=name)
    r.gauge("brick_job_last_success_timestamp_seconds", "When the unit last succeeded (0: not yet).",
            ["unit"]).set(now if ok else last_success, unit=name)
    if seconds is not None:
        r.gauge("brick_job_last_duration_seconds", "How long the unit's last run took.",
                ["unit"]).set(round(seconds, 3), unit=name)
    if git and all(git):
        r.gauge("brick_job_checkout_info", "The branch and commit the host's checkout is on.",
                ["unit", "branch", "commit"]).set(1, unit=name, branch=git[0], commit=git[1])
    return r.render()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("name", help="the unit's name, without .service")
    p.add_argument("--dir", default=os.environ.get("BRICK_TEXTFILE_DIR", TEXTFILE_DIR))
    p.add_argument("--git", help="a checkout whose branch and commit to report")
    args = p.parse_args()
    if not os.path.isdir(args.dir):
        print(f"jobs: no {args.dir}; nothing recorded", file=sys.stderr)
        return
    path = os.path.join(args.dir, f"brick-job-{args.name}.prom")
    ok = os.environ.get("SERVICE_RESULT", "success") == "success" and os.environ.get("EXIT_STATUS", "0") in ("0", "")
    text = render(args.name, time.time(), ok, unit_seconds(f"{args.name}.service"),
                  previous_success(path, args.name), git_info(args.git) if args.git else None)
    write_textfile(path, text)


if __name__ == "__main__":
    main()
