"""Image builds on brick9000 itself, as reported by brick9000/build-image.

build-image writes one JSON object to its status file: `state` is
"building", then "succeeded" or "failed" with a `finished` time.
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


def light_pattern(path: str, now: float, result_seconds: float, alive=_alive) -> str | None:
    """The build light pattern to show now, or None when no build needs the lights."""
    try:
        with open(path) as f:
            status = json.load(f)
    except (OSError, ValueError):
        return None
    state = status.get("state")
    if state == BUILDING:
        # A build killed outright (SIGKILL, power cut) never writes its result.
        return "image-building" if alive(int(status.get("pid", 0))) else None
    if state in (SUCCEEDED, FAILED) and now - status.get("finished", 0) < result_seconds:
        return "image-pushed" if state == SUCCEEDED else "image-failed"
    return None
