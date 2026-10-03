"""Phone pages for problems that need a person, sent through ntfy.

The board shows everything; a page is only for what won't heal by itself and
has lasted long enough to matter. Each problem pages once when it has held for
its rule's time and once more when it clears, written by Ol' Brick, the
outfit's old prospector, with the board's pixel art attached.

Pages go to a third-party server (ntfy.sh), so they carry nothing sensitive:
only brick names, durations and states. The details stay on the board.

Every problem that comes and goes is also appended to incidents.jsonl, for
the weekly report, whether or not it paged.
"""

import base64
import json
import logging
import os
import random
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .checks import FAIL, UNKNOWN, WARN

log = logging.getLogger("brick-status.notify")

# How long each kind of problem must last before it pages. wigle-sync's check
# only turns red after 2h of failing runs (checks.WIGLE_ERROR_WINDOW), so its
# 10h here pages after 12h of failures: files are piling up on brick69 then.
HOLDS = {
    "unreachable": 30 * 60,   # the board can't reach Prometheus, though the internet is up
    "control-plane": 30 * 60,
    "node": 30 * 60,
    "wigle": 10 * 3600,
    "disk": 3600,
}
DISK_LIMIT = 85.0

ART = Path(__file__).resolve().parent / "static" / "notify"


@dataclass(frozen=True)
class Problem:
    kind: str
    subject: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.subject}" if self.subject else self.kind


def find_problems(summary: dict, internet: bool, vitals: list[dict]) -> tuple[list[Problem], set[str]]:
    """The problems in one poll, and what the poll could see.

    The second value holds the kinds (or "kind:subject" keys) the poll had data
    for. A problem only clears when its kind could be seen: a node that drops
    out of the data because Prometheus is unreachable hasn't recovered.
    """
    groups = summary.get("groups", {})
    cluster = groups.get("cluster", {}).get("checks", [])
    wigle = groups.get("wigle", {}).get("checks", [])
    found, seen = [], set()

    blind = any(c["name"] == "prometheus" and c["state"] == UNKNOWN for c in cluster)
    if internet:
        seen.add("unreachable")
        if blind:
            found.append(Problem("unreachable"))
    if blind:
        return found, seen

    seen.add("control-plane")
    if any(c["name"] == "control plane" and c["state"] == FAIL for c in cluster):
        found.append(Problem("control-plane"))
        # With the API down kube-state-metrics stops, so node readiness is
        # unknown, not fine.
    else:
        nodes = [c for c in cluster if c["detail"] in ("Ready", "NotReady")]
        if nodes:
            seen.add("node")
            found += [Problem("node", c["name"]) for c in nodes if c["detail"] == "NotReady"]

    last = next((c for c in wigle if c["name"] == "last sync"), None)
    if last and last["state"] != UNKNOWN:
        seen.add("wigle")
        if last["state"] == FAIL:
            found.append(Problem("wigle"))

    for v in vitals:
        if "disk" in v:
            seen.add(f"disk:{v['node']}")
            if v["disk"] > DISK_LIMIT:
                found.append(Problem("disk", v["node"]))
    return found, seen


def duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 90:
        return f"{minutes} min"
    hours = minutes / 60
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} days"


# What Ol' Brick says. {subject} is a brick name, {held} how long it's lasted.
# Title, message, ntfy tags, the art to attach.
PAGES = {
    "unreachable": ("The diggin's went dark", [
        "Can't raise a peep from the cluster for {held}, and the internet's fine. Somebody best go look.",
        "Ol' Brick's been hollerin' down the shaft for {held}. Nothin' hollers back.",
    ], "rotating_light,pick", "server-fire"),
    "control-plane": ("The control plane's down", [
        "The boss mule's been down {held}. The diggin's are runnin' blind.",
        "Control plane's been dead {held}. Nobody's givin' orders down here.",
    ], "fire,pick", "server-fire"),
    "node": ("{subject} is down", [
        "{subject} keeled over {held} ago and ain't got up. Might need a kick.",
        "Lost {subject} {held} back. The rest of the outfit's carryin' its load.",
    ], "fire,pick", "server-fire"),
    "wigle": ("wigle-sync is stuck", [
        "wigle-sync's been failin' for {held}. Them files are pilin' up on brick69 like tailings.",
        "No color in the pan for {held}: wigle-sync can't get its files out.",
    ], "electric_plug,warning", "plugs-disconnected"),
    "disk": ("{subject} is fillin' up", [
        "{subject}'s disk is near full and has been for {held}. Clear some ore out before she's stuffed.",
        "{subject} is runnin' out of room. Been that way {held}.",
    ], "warning,pick", "server-warn"),
}
CLEARS = {
    "unreachable": ("The cluster's talkin' again", "Back after {held}. False alarm or ghost, it's quiet now.", "server-ok"),
    "control-plane": ("Control plane's back", "The boss mule's up after {held}. Carry on.", "server-ok"),
    "node": ("{subject} is back", "{subject} got back on its feet after {held}.", "server-ok"),
    "wigle": ("wigle-sync is syncin'", "The plugs are back in after {held}. Files are movin'.", "plugs-connected"),
    "disk": ("{subject} has room again", "{subject}'s disk is back under the line after {held}.", "server-ok"),
}


# Pages go through a third-party server; nothing that looks like an address,
# a link or an account goes in one (see the README, "Privacy").
LEAKS = re.compile(r"\d+\.\d+\.\d+\.\d+|https?://|\.local\b|@")


def _host_line(v: dict) -> str:
    parts = []
    if "disk" in v:
        parts.append(f"Disk {v['disk']:.0f}%")
    if "mem" in v:
        parts.append(f"Mem {v['mem']:.0f}%")
    if "cpu" in v:
        parts.append(f"CPU {v['cpu']:.0f}%")
    if "temp" in v:
        parts.append(f"{v['temp'] * 9 / 5 + 32:.0f}°F")
    return " · ".join(parts)


def facts(problem: Problem, summary: dict, vitals: list[dict]) -> str:
    """A short line of numbers to go under a page: fixed labels, no log text.

    Empty when there's nothing to say, or when it could leak something.
    """
    hosts = {v["node"]: v for v in vitals if "node" in v}
    cluster = summary.get("groups", {}).get("cluster", {}).get("checks", [])
    wigle = {c["name"]: c["detail"] for c in summary.get("groups", {}).get("wigle", {}).get("checks", [])}
    short = sum(1 for c in cluster if "/" in c["name"] and c["state"] in (WARN, FAIL))
    if problem.kind == "disk" and problem.subject in hosts:
        line = _host_line(hosts[problem.subject])
    elif problem.kind in ("node", "control-plane"):
        up = [f"{name} CPU {v['cpu']:.0f}% Mem {v['mem']:.0f}%" for name, v in sorted(hosts.items())
              if name != problem.subject and name != "brick9000" and "cpu" in v and "mem" in v]
        line = "Still answering: " + ", ".join(up) if up else "No node answering"
        if problem.kind == "node":
            line += f" · Workloads short: {short}"
    elif problem.kind == "wigle" and "last sync" in wigle:
        line = f"Last sync: {wigle['last sync']} · Uploaded: {wigle.get('files uploaded', '—')}"
    else:
        line = ""
    return "" if LEAKS.search(line) else line


def page_message(problem: Problem, held: float, cleared: bool, rng: random.Random,
                 extra: str = "") -> dict:
    words = {"subject": problem.subject, "held": duration(held)}
    if cleared:
        title, text, art = CLEARS[problem.kind]
        tags, priority = "white_check_mark", 3
    else:
        title, texts, tags, art = PAGES[problem.kind]
        text, priority = rng.choice(texts), 4
    message = text.format(**words) + (f"\n{extra}" if extra else "")
    return {"title": title.format(**words), "message": message,
            "tags": tags, "priority": priority, "art": art}


def test_message(vitals: dict | None = None) -> dict:
    """A test page; with brick9000's own vitals, it shows what a facts line looks like."""
    line = f"{vitals['node']}: {_host_line(vitals)}" if vitals else ""
    message = "Just testin' the telegraph. Nothin's wrong." + (f"\n{line}" if line and not LEAKS.search(line) else "")
    return {"title": "Ol' Brick checkin' in", "message": message,
            "tags": "pick,wave", "priority": 3, "art": "server-ok"}


class Pager:
    """Turns each poll's problems into pages, remembering open problems and
    unsent pages across restarts in a small JSON file."""

    def __init__(self, state_path: Path, incidents_path: Path, holds: dict[str, float] = HOLDS,
                 rng: random.Random | None = None):
        self.state_path = state_path
        self.incidents_path = incidents_path
        self.holds = holds
        self.rng = rng or random.Random()
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {}
        self.open: dict[str, dict] = state.get("open", {})
        self.outbox: list[dict] = state.get("outbox", [])

    def observe(self, problems: list[Problem], seen: set[str], now: float,
                extra=lambda problem: "") -> None:
        """`extra(problem)` gives the facts line for a page or all-clear sent now."""
        current = {p.key: p for p in problems}
        changed = False
        for key, p in current.items():
            if key not in self.open:
                self.open[key] = {"kind": p.kind, "subject": p.subject, "since": now, "paged": False}
                changed = True
        for key, entry in list(self.open.items()):
            problem = Problem(entry["kind"], entry["subject"])
            held = now - entry["since"]
            if key in current:
                if not entry["paged"] and held >= self.holds[problem.kind]:
                    self.outbox.append(page_message(problem, held, False, self.rng, extra(problem)))
                    entry["paged"] = changed = True
            elif problem.kind in seen or key in seen:
                if entry["paged"]:
                    self.outbox.append(page_message(problem, held, True, self.rng, extra(problem)))
                self._record(entry, now)
                del self.open[key]
                changed = True
        if changed:
            self._save()

    def take(self) -> list[dict]:
        return list(self.outbox)

    def sent(self, message: dict) -> None:
        self.outbox.remove(message)
        self._save()

    def _record(self, entry: dict, now: float) -> None:
        line = {"kind": entry["kind"], "subject": entry["subject"], "start": entry["since"],
                "end": now, "paged": entry["paged"]}
        self.incidents_path.parent.mkdir(parents=True, exist_ok=True)
        with self.incidents_path.open("a") as f:
            f.write(json.dumps(line) + "\n")

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.state_path.parent, prefix=".notify-")
        with os.fdopen(fd, "w") as f:
            json.dump({"open": self.open, "outbox": self.outbox}, f)
        os.replace(tmp, self.state_path)


def _header(text: str) -> str:
    """HTTP headers are Latin-1 and one line; ntfy reads RFC 2047 encoded words
    for anything else."""
    if text.isascii() and "\n" not in text:
        return text
    return "=?UTF-8?B?" + base64.b64encode(text.encode()).decode() + "?="


class Ntfy:
    def __init__(self, url: str, topic: str, click: str = "", timeout: float = 15.0):
        self.endpoint = f"{url.rstrip('/')}/{topic}"
        self.click = click
        self.timeout = timeout

    def send(self, message: dict) -> None:
        headers = {"Title": _header(message["title"]), "Tags": message["tags"],
                   "Priority": str(message["priority"])}
        if self.click:
            headers["Click"] = self.click
        art = ART / f"{message['art']}.png"
        try:
            image = art.read_bytes()
        except OSError as e:
            log.warning("no art %s: %s", art.name, e)
            image = None
        if image is not None:
            # With a file as the body, the text goes in a header.
            try:
                self._put(image, {**headers, "Message": _header(message["message"]), "Filename": art.name})
                return
            except urllib.error.HTTPError as e:
                if not 400 <= e.code < 500:
                    raise
                log.warning("ntfy refused the attachment (%s); sending text only", e.code)
        self._put(message["message"].encode(), headers)

    def _put(self, body: bytes, headers: dict) -> None:
        req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="PUT")
        with urllib.request.urlopen(req, timeout=self.timeout):
            pass


def read_topic(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def deliver(pager: Pager, ntfy: Ntfy | None) -> None:
    """Send what's waiting. A failure leaves it queued for the next poll."""
    if ntfy is None:
        return
    for message in pager.take():
        try:
            ntfy.send(message)
        except Exception as e:  # offline, ntfy down: try again next poll
            log.warning("page not sent yet (%s): %s", message["title"], e)
            return
        log.info("paged: %s", message["title"])
        pager.sent(message)
