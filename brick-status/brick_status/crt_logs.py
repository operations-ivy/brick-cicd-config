"""The running job's log, scrolling in the lower half of the CRT while
something runs (the upper half is crt_activity.py). Run in its own
pseudo-terminal by matrix.py ("activity-log"):

    python3 -m brick_status.crt_logs

It follows the newest running sender (when check-all starts test-all, test-all),
says which in a header line, and prints its new lines as they come; the
terminal scrolls them. A plasma sender that says what it's running can be
followed when it is:

- jenkins-<job>, whose "show" names the job and build: Jenkins' progressive
  console log, read as brick-status' read-only user (BRICK_STATUS_JENKINS_*).
- brick9000-deploy: brick9000's own deploy, from the system journal.

Each source hands back only the lines that are new since it was last asked.
Every line is cleaned before it reaches the screen (or the board's mirror):
colour codes and control characters go, and anything that looks like a secret
is blanked. Jenkins masks its own credentials already; this is the second net.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# The terminal's own escapes (colour, cursor moves) and other control characters.
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b.")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
HIDDEN = "[hidden]"
# A credential after its scheme: "Authorization: Basic x", "Bearer x".
SCHEME = re.compile(r"(?i)\b((?:authorization\s*:\s*(?:basic|bearer|token)?|bearer|basic)\s+)(\S+)")
# key=value and key: value (quoted or not, JSON too), for the names secrets go by.
KEYED = re.compile(r"(?i)\b([\w.-]*(?:token|secret|passw(?:or)?d|pwd|api[_-]?key|auth)[\w.-]*[\"']?)"
                   r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)")
URL_LOGIN = re.compile(r"(?i)(https?://[^/\s:@]+:)([^@\s]+)(@)")  # user:password@ in a URL
LONG = re.compile(r"[A-Za-z0-9+/_=-]{32,}")  # a key, token or hash-shaped blob


def clean(line: str) -> str:
    """One log line, safe to show: no escapes or controls, secrets blanked."""
    line = CONTROL.sub("", ANSI.sub("", line.replace("\t", "    "))).rstrip()
    line = SCHEME.sub(lambda m: m.group(1) + HIDDEN, line)
    line = KEYED.sub(lambda m: m.group(1) + m.group(2) + HIDDEN, line)
    line = URL_LOGIN.sub(lambda m: m.group(1) + HIDDEN + m.group(3), line)
    return LONG.sub(lambda m: m.group(0) if _digest(m) else HIDDEN, line)


def _digest(m: re.Match) -> bool:
    """An image digest (sha256:<64 hex>) is public and worth seeing. Everything
    else that long is hidden, commit hashes too: a token can look like one."""
    text = m.group(0)
    return (m.string[max(m.start() - 7, 0):m.start()] == "sha256:" and len(text) == 64
            and all(c in "0123456789abcdef" for c in text))


class _Lines:
    """Splits text arriving in pieces into whole lines."""

    def __init__(self):
        self.partial = ""

    def feed(self, text: str) -> list[str]:
        parts = (self.partial + text).split("\n")
        self.partial = parts.pop()
        return [clean(p) for p in parts]


class JenkinsLog:
    """A build's console, a piece at a time (Jenkins' progressiveText)."""

    def __init__(self, url: str, user: str, password: str, job: str, build: int, timeout: float = 3.0):
        self.url = (f"{url.rstrip('/')}/job/{urllib.parse.quote(job, safe='')}/{build}"
                    f"/logText/progressiveText")
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}"}
        self.timeout = timeout
        self.offset = 0
        self.lines = _Lines()

    def poll(self) -> list[str]:
        request = urllib.request.Request(f"{self.url}?start={self.offset}", headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as reply:
                text = reply.read().decode("utf-8", "replace")
                self.offset = int(reply.headers.get("X-Text-Size") or self.offset + len(text.encode()))
        except (OSError, ValueError):
            return []  # try again next time; the screen keeps what it has
        return self.lines.feed(text)


class JournalLog:
    """A systemd user unit's journal on this machine, from the first poll on.
    (journalctl --user finds nothing on brick9000, so it's matched by field.)"""

    def __init__(self, unit: str, journalctl: str = "journalctl", backlog: int = 50):
        self.args = [journalctl, f"_SYSTEMD_USER_UNIT={unit}", "-o", "cat", "--no-pager", "--show-cursor"]
        self.cursor: str | None = None
        self.backlog = backlog

    def poll(self) -> list[str]:
        args = self.args + (["--after-cursor", self.cursor] if self.cursor else ["-n", str(self.backlog)])
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        lines = out.splitlines()
        if lines and lines[-1].startswith("-- cursor: "):
            self.cursor = lines.pop().removeprefix("-- cursor: ")
        return [clean(line) for line in lines]


def for_item(item: dict, env=os.environ):
    """The log to follow for an /activity item, or None."""
    source, details = item.get("source", ""), item.get("details") or {}
    if source.startswith("jenkins-") and details.get("job") and details.get("build"):
        try:
            build = int(str(details["build"]).lstrip("#"))
        except ValueError:
            return None
        return JenkinsLog(env.get("BRICK_STATUS_JENKINS_URL", "https://jenkins.brick.nozdormu.cloud"),
                          env.get("BRICK_STATUS_JENKINS_USER", "brick-status"),
                          env.get("BRICK_STATUS_JENKINS_PASSWORD", ""), details["job"], build)
    if source == "brick9000-deploy":
        return JournalLog("brick-deploy.service")
    return None


class Follower:
    """Prints the newest running item's log lines, cut to the terminal's width."""

    def __init__(self, cols: int, write, log_for=for_item, clock=time.monotonic):
        self.cols, self.write, self.log_for, self.clock = cols, write, log_for, clock
        self.seen: dict[str, float] = {}  # source to when it first appeared
        self.source: str | None = None
        self.log = None

    def update(self, items: list[dict]) -> None:
        now = self.clock()
        running = {i.get("source"): i for i in items if i.get("source")}
        for source in running:
            self.seen.setdefault(source, now)
        for source in [s for s in self.seen if s not in running]:
            del self.seen[source]
        newest = max(self.seen, key=lambda s: (self.seen[s], s), default=None)
        if newest != self.source:
            self.source = newest
            item = running.get(newest)
            self.log = self.log_for(item) if item else None
            if item:
                title = clean(str(item.get("title") or newest))
                self._line(f"-- {title} " + "-" * self.cols, bold=True)
        if self.log is not None:
            for line in self.log.poll():
                if line.strip():
                    self._line(line)

    def _line(self, text: str, bold: bool = False) -> None:
        """One line, cut (not wrapped) to the width; the terminal scrolls."""
        text = text[:max(self.cols - 1, 1)]
        self.write("\r\n" + (f"\x1b[1m{text}\x1b[0m" if bold else text))


def fetch() -> list[dict]:
    url = os.environ.get("BRICK_STATUS_PLASMA_URL", "http://127.0.0.1:8766").rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/activity", timeout=1) as reply:
            return json.load(reply)["activity"]
    except (OSError, ValueError, KeyError):
        return []


def main() -> None:
    def write(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    write("\x1b[2J\x1b[?25l\x1b[999;1H")  # start at the bottom, so lines scroll up
    follower = Follower(shutil.get_terminal_size().columns, write)
    while True:
        follower.update(fetch())
        time.sleep(1.0)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
