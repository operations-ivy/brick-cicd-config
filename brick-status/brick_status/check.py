"""The cabinet's check button (button 6, the bottom row's right-hand one): press it twice to check the whole homelab.

Two presses within `window` seconds start Jenkins' check-all job
(jenkins/jobs/check-all.groovy: scripts/smoke, then test-all) through its
webhook. One press only arms it, so a knock can't start an hour of tests. The
lights answer at once (armed, started, or Jenkins couldn't be reached); the
job then shows its own stages and its result.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import structlog

log = structlog.get_logger(__name__)


class DoublePress:
    """True on the second press within `window` seconds of the first."""

    def __init__(self, window: float = 1.5):
        self.window = window
        self._armed: float | None = None

    def press(self, now: float) -> bool:
        if self._armed is not None and now - self._armed <= self.window:
            self._armed = None
            return True
        self._armed = now
        return False


def start_job(jenkins_url: str, job: str, secret_file: str, timeout: float = 10.0) -> bool:
    """Start a Jenkins job through its generic webhook. True if Jenkins says it
    started (or queued) it. Never logs the URL: its token is the secret."""
    try:
        secret = Path(secret_file).read_text().strip()
    except OSError as e:
        log.warning("no webhook secret", job=job, path=secret_file, error=e.strerror)
        return False
    if not secret:
        log.warning("webhook secret file empty", job=job, path=secret_file)
        return False
    url = f"{jenkins_url}/generic-webhook-trigger/invoke?token={quote(f'{job}-{secret}', safe='')}"
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as e:
        log.warning("Jenkins' webhook refused", job=job, status=e.code)
        return False
    except (OSError, ValueError) as e:
        log.warning("Jenkins unreachable", job=job, error=str(getattr(e, "reason", type(e).__name__)))
        return False
    try:
        jobs = json.loads(body).get("jobs") or {}
    except (ValueError, AttributeError):
        jobs = {}
    if not any(isinstance(j, dict) and j.get("triggered") for j in jobs.values()):
        log.warning("Jenkins answered but started nothing", job=job)
        return False
    log.info("job started", job=job)
    return True
