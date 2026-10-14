"""Failed systemd units and timers that stopped firing, as board checks and
as Prometheus metrics.

brick-status checks brick9000's own units directly, so the board still shows
them when the cluster is down. Other hosts come through Prometheus:
node-exporter's systemd collector for the k3s nodes' system units, and this
module's textfile output for user units, which that collector can't see (run
on brick1982 by brick-units.timer):

    python3 -m brick_status.units --textfile /var/lib/node-exporter-textfile/brick-units.prom
"""

import argparse
import json
import os
import socket
import subprocess
import tempfile
import time

from .checks import OK, UNKNOWN, WARN, Check, _ago

GROUP = "services"
# A timer whose next run is this far in the past isn't firing.
OVERDUE_SECONDS = 15 * 60
# The textfile output older than this means its timer stopped (it runs every 2 minutes).
REPORT_STALE_SECONDS = 10 * 60

# Units known to fail, by host, and why: shown as ok so they don't hold the
# board yellow. brick2000's are hands off (homelab-context, hosts.md).
EXPECTED = {
    ("brick2000", "influxdb.service"): "expected: pironman5 runs its own influxd",
    ("brick2000", "nvmf-autoconnect.service"): "expected: no NVMe-oF targets",
    ("brick2000", "openipmi.service"): "expected: no IPMI hardware",
}


def systemctl(user: bool, *args: str):
    cmd = ["systemctl", *(["--user"] if user else []), *args, "-o", "json"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=True).stdout
    return json.loads(out or "[]")


def is_enabled(user: bool, unit: str) -> bool:
    cmd = ["systemctl", *(["--user"] if user else []), "is-enabled", unit]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip() == "enabled"


def is_running(user: bool, unit: str) -> bool:
    cmd = ["systemctl", *(["--user"] if user else []), "is-active", unit]
    state = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    return state in ("active", "activating", "reloading")


def read(user: bool) -> dict:
    """One scope's failed units and timers, straight from systemd."""
    failed = [u["unit"] for u in systemctl(user, "list-units", "--state=failed", "--plain")]
    timers = systemctl(user, "list-timers", "--all")
    for t in timers:
        if not t.get("next"):
            t["enabled"] = is_enabled(user, t["unit"])
            # An OnUnitActiveSec timer has no next run while its service runs
            # (brick-units.timer while it reports, brick-roms during a copy).
            t["running"] = bool(t.get("activates")) and is_running(user, t["activates"])
    return {"failed": failed, "timers": timers}


def problems(data: dict, now: float) -> list[tuple[str, str, str]]:
    """(unit, kind, detail) for each failed unit, and each timer that's overdue
    or enabled with no next run."""
    found = [(u, "failed", "failed") for u in data["failed"]]
    for t in data["timers"]:
        nxt = (t.get("next") or 0) / 1e6
        if not nxt:
            if t.get("enabled") and not t.get("running"):
                found.append((t["unit"], "stopped", "enabled, not scheduled"))
        elif now - nxt > OVERDUE_SECONDS:
            found.append((t["unit"], "overdue", f"overdue by {_ago(now - nxt)}"))
    return found


def check(host: str, unit: str, detail: str) -> Check:
    expected = EXPECTED.get((host, unit))
    if expected:
        return Check(GROUP, f"{host}: {unit}", OK, expected)
    return Check(GROUP, f"{host}: {unit}", WARN, detail)


def with_ok(host: str, checks: list[Check]) -> list[Check]:
    """A host's checks, plus one ok row when none of them is a problem."""
    if any(c.state != OK for c in checks):
        return checks
    return checks + [Check(GROUP, host, OK, "no failed units")]


def local_checks(host: str, now: float | None = None, reader=None) -> list[Check]:
    """This host's system and user units."""
    now = time.time() if now is None else now
    reader = reader or read
    found = []
    try:
        for user in (False, True):
            found += [check(host, unit, detail) for unit, _, detail in problems(reader(user), now)]
    except Exception as e:  # systemctl missing, timed out, bad output
        return [Check(GROUP, host, UNKNOWN, f"systemctl: {e}")]
    return with_ok(host, found)


def remote_checks(prom, skip_host: str, now: float | None = None) -> list[Check]:
    """Every other host's units, from Prometheus: the systemd collector's
    failed system units, and the textfile output's user-unit problems."""
    now = time.time() if now is None else now
    names = {m["instance"]: m.get("nodename", m["instance"]) for m, _ in prom.query("node_uname_info")}
    found = {host: [] for host in names.values() if host != skip_host}

    def add(m: dict, unit: str, detail: str) -> None:
        host = names.get(m.get("instance"), m.get("instance", "?"))
        if host != skip_host:
            found.setdefault(host, []).append(check(host, unit, detail))

    for m, _ in prom.query('node_systemd_unit_state{state="failed"} == 1'):
        add(m, m["name"], "failed")
    for m, _ in prom.query("brick_systemd_unit_problem == 1"):
        add(m, m["unit"], {"overdue": "overdue", "stopped": "enabled, not scheduled"}.get(m["problem"], "failed"))
    for m, v in prom.query("brick_systemd_report_timestamp_seconds"):
        if now - v > REPORT_STALE_SECONDS:
            add(m, "brick-units.timer", f"user units unreported for {_ago(now - v)}")

    return [c for host in sorted(found) for c in with_ok(host, found[host])]


def board_checks(prom, host: str, now: float | None = None, reader=None) -> list[Check]:
    """The board's services group: this host's units checked directly, so they
    still show when Prometheus can't be reached, then every other host's."""
    checks = local_checks(host, now, reader)
    try:
        checks += remote_checks(prom, host, now)
    except Exception as e:  # network errors, bad responses: show them, keep going
        checks.append(Check(GROUP, "other hosts", UNKNOWN, f"prometheus unreachable: {e}"))
    return checks


def metrics(scope: str, found: list[tuple[str, str, str]], now: float) -> str:
    """Prometheus text format for node-exporter's textfile collector."""
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    lines = ["# HELP brick_systemd_unit_problem A failed unit, or a timer overdue or stopped (1).",
             "# TYPE brick_systemd_unit_problem gauge"]
    lines += [f'brick_systemd_unit_problem{{scope="{scope}",unit="{esc(u)}",problem="{kind}"}} 1'
              for u, kind, _ in found]
    lines += ["# HELP brick_systemd_report_timestamp_seconds When brick_status.units last wrote this.",
              "# TYPE brick_systemd_report_timestamp_seconds gauge",
              f'brick_systemd_report_timestamp_seconds{{scope="{scope}"}} {now:.0f}']
    return "\n".join(lines) + "\n"


def write_textfile(path: str, text: str) -> None:
    """Replace the file in one step: node-exporter never reads half of it."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".brick-units.")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--textfile", help="write user-unit metrics here (else print this host's checks)")
    args = p.parse_args()
    now = time.time()
    if args.textfile:
        write_textfile(args.textfile, metrics("user", problems(read(True), now), now))
        return
    for c in local_checks(socket.gethostname(), now):
        print(f"{c.state:7} {c.name}  {c.detail}")


if __name__ == "__main__":
    main()
