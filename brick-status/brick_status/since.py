"""How long each anomaly has been there, so the board can say when one has been
left for more than a day (the user, 2026-11-07: "if it stays around for more
than 24h something should indicate that I need to address it").

The keys are the names the board's Anomalies page shows (static/index.html,
findAnomalies): "<group>/<check>" for a check that isn't fine, "<node>/temp"
and "<node>/disk" past their warning levels, "internet", "brick9000/sshd".
First-seen times are kept in a small JSON file, so a restart or a deploy
doesn't reset the clock; an anomaly that clears is forgotten.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

NOT_FINE = ("fail", "warn", "unknown")
TEMP_WARN_C = 70  # the page's TMP warning level
DISK_WARN_PCT = 85  # and DSK's


def anomaly_keys(summary: dict, vitals: list[dict], host_state: dict, hostname: str) -> set[str]:
    """Every anomaly the board would show now, by the name it shows it under."""
    keys = {f"{group}/{c['name']}" for group, g in (summary.get("groups") or {}).items()
            for c in g.get("checks", []) if c.get("state") in NOT_FINE}
    for n in vitals:
        if (n.get("temp") or 0) >= TEMP_WARN_C:
            keys.add(f"{n['node']}/temp")
        if (n.get("disk") or 0) >= DISK_WARN_PCT:
            keys.add(f"{n['node']}/disk")
    if host_state.get("internet") is False:
        keys.add("internet")
    if host_state.get("sshd") is False:
        keys.add(f"{hostname}/sshd")
    return keys


class Since:
    """First-seen times of the anomalies present now, kept in `path`."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        try:
            self.first = {str(k): float(v) for k, v in json.loads(self.path.read_text()).items()}
        except (OSError, ValueError, AttributeError):  # missing, corrupt, or not a dict: start over
            self.first = {}

    def observe(self, keys: set[str], now: float) -> dict[str, float]:
        """Remember when each of `keys` was first seen; forget the rest."""
        first = {k: self.first.get(k, now) for k in keys}
        if first != self.first:
            self.first = first
            self._save()
        return dict(first)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.first))
            tmp.replace(self.path)
        except OSError as e:
            log.warning("anomaly times: %s", e)
