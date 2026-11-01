"""What Prometheus reads, from the job brick-plasma."""

import threading


class Stats:
    def __init__(self):
        self.frames = 0
        self.changes = 0
        self.frame_seconds = 0.0
        self.showing = "off"
        self.requests: dict[int, int] = {}
        self._lock = threading.Lock()

    def count(self, code: int) -> None:
        with self._lock:
            self.requests[code] = self.requests.get(code, 0) + 1

    def render(self, signals: int) -> str:
        with self._lock:
            requests = dict(self.requests)
        lines = [
            "# HELP brick_plasma_frames_total Frames written to the LEDs.",
            "# TYPE brick_plasma_frames_total counter",
            f"brick_plasma_frames_total {self.frames}",
            "# HELP brick_plasma_changes_total Times the lights changed to a new pattern.",
            "# TYPE brick_plasma_changes_total counter",
            f"brick_plasma_changes_total {self.changes}",
            "# HELP brick_plasma_frame_seconds How long the last frame took to write.",
            "# TYPE brick_plasma_frame_seconds gauge",
            f"brick_plasma_frame_seconds {self.frame_seconds:.6f}",
            "# HELP brick_plasma_signals Signals held now.",
            "# TYPE brick_plasma_signals gauge",
            f"brick_plasma_signals {signals}",
            "# HELP brick_plasma_showing 1 for the pattern on the lights.",
            "# TYPE brick_plasma_showing gauge",
            f'brick_plasma_showing{{pattern="{self.showing}"}} 1',
            "# HELP brick_plasma_requests_total API requests, by status code.",
            "# TYPE brick_plasma_requests_total counter",
        ]
        lines += [f'brick_plasma_requests_total{{code="{code}"}} {n}' for code, n in sorted(requests.items())]
        return "\n".join(lines) + "\n"
