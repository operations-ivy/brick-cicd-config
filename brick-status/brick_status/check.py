"""The cabinet's front-right button: press it twice to check the whole homelab.

Two presses within `window` seconds start Jenkins' check-all job
(jenkins/jobs/check-all.groovy: scripts/smoke, then test-all) through its
webhook. One press only arms it, so a knock can't start an hour of tests. The
lights answer at once (armed, started, or Jenkins couldn't be reached); the
job then shows its own stages and its result.
"""

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)


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
        log.warning("%s: no webhook secret in %s (%s)", job, secret_file, e.strerror)
        return False
    if not secret:
        log.warning("%s: the webhook secret file %s is empty", job, secret_file)
        return False
    url = f"{jenkins_url}/generic-webhook-trigger/invoke?token={quote(f'{job}-{secret}', safe='')}"
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as e:
        log.warning("%s: Jenkins' webhook answered %d", job, e.code)
        return False
    except (OSError, ValueError) as e:
        log.warning("%s: couldn't reach Jenkins (%s)", job, getattr(e, "reason", type(e).__name__))
        return False
    try:
        jobs = json.loads(body).get("jobs") or {}
    except (ValueError, AttributeError):
        jobs = {}
    if not any(isinstance(j, dict) and j.get("triggered") for j in jobs.values()):
        log.warning("%s: Jenkins answered but started nothing", job)
        return False
    log.info("%s: started", job)
    return True
