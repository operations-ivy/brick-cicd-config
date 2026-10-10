"""Image builds and deploys on brick9000 itself, as reported by
brick9000/build-status.sh (sourced by build-image and deploy).

Each writes one JSON object to its status file: `state` is "building", then
"succeeded" or "failed" with a `finished` time. Image builds share one file;
deploy has its own, so the image builds inside a deploy don't replace it.
"""

import json
import os

BUILDING, SUCCEEDED, FAILED = "building", "succeeded", "failed"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def light_pattern(path: str, now: float, result_seconds: float, alive=_alive,
                  running: str = "image-building", min_running_seconds: float = 0.0) -> str | None:
    """The light pattern to show now, or None when nothing needs the lights.

    `running` is the pattern while the build (or deploy) is still going. It
    shows for at least `min_running_seconds` from the start, even once the
    result is in, so a quick deploy is still seen; the result then shows for
    `result_seconds` from the end of that.
    """
    try:
        with open(path) as f:
            status = json.load(f)
    except (OSError, ValueError):
        return None
    state = status.get("state")
    if state == BUILDING:
        # A build killed outright (SIGKILL, power cut) never writes its result.
        return running if alive(int(status.get("pid", 0))) else None
    if state not in (SUCCEEDED, FAILED):
        return None
    shown_from = status.get("finished", 0)
    if min_running_seconds and "started" in status:
        held_until = status["started"] + min_running_seconds
        if now < held_until:
            return running
        shown_from = max(shown_from, held_until)
    if now - shown_from < result_seconds:
        return "image-pushed" if state == SUCCEEDED else "image-failed"
    return None
