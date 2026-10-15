"""Prometheus metrics without a dependency: counters and gauges with labels,
rendered in the text format, served at /metrics or written for
node-exporter's textfile collector.

The hosts run these daemons on the system Python with no pip packages, so
brick-arena and brick-retro import this from the same checkout.

    REQUESTS = registry.counter("brick_x_requests_total", "Requests.", ["path"])
    REQUESTS.inc(path="/")
    registry.render()
"""

import math
import os
import tempfile
import threading


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(v: float) -> str:
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    return repr(float(v)) if v != int(v) else str(int(v))


class Metric:
    def __init__(self, name: str, help_text: str, kind: str, labels: list[str], lock: threading.Lock):
        self.name, self.help, self.kind, self.labels = name, help_text, kind, tuple(labels)
        self._values: dict[tuple, float] = {}
        self._lock = lock

    def _key(self, labels: dict) -> tuple:
        if set(labels) != set(self.labels):
            raise ValueError(f"{self.name} takes labels {self.labels}, got {tuple(labels)}")
        return tuple(str(labels[k]) for k in self.labels)

    def set(self, value: float, **labels) -> None:
        with self._lock:
            self._values[self._key(labels)] = float(value)

    def inc(self, amount: float = 1, **labels) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def get(self, **labels) -> float:
        with self._lock:
            return self._values.get(self._key(labels), 0.0)

    def clear(self) -> None:
        """Forget every labelled series (for gauges whose label sets come and go)."""
        with self._lock:
            self._values.clear()

    def replace(self, values: dict[tuple, float]) -> None:
        """Swap in a whole new set of series at once, keyed by label values in order."""
        with self._lock:
            self._values = {tuple(str(v) for v in k): float(x) for k, x in values.items()}

    def lines(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]
        with self._lock:
            items = sorted(self._values.items())
        for key, value in items:
            if self.labels:
                pairs = ",".join(f'{k}="{_escape(v)}"' for k, v in zip(self.labels, key))
                out.append(f"{self.name}{{{pairs}}} {_number(value)}")
            else:
                out.append(f"{self.name} {_number(value)}")
        return out


class Registry:
    def __init__(self):
        self._metrics: list[Metric] = []
        self._lock = threading.Lock()

    def _add(self, name: str, help_text: str, kind: str, labels) -> Metric:
        m = Metric(name, help_text, kind, list(labels or []), self._lock)
        self._metrics.append(m)
        return m

    def counter(self, name: str, help_text: str, labels=None) -> Metric:
        return self._add(name, help_text, "counter", labels)

    def gauge(self, name: str, help_text: str, labels=None) -> Metric:
        return self._add(name, help_text, "gauge", labels)

    def render(self) -> str:
        return "\n".join(line for m in self._metrics for line in m.lines()) + "\n"


CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def write_textfile(path: str, text: str) -> None:
    """Replace the file in one step, so node-exporter never reads half of it."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".metrics-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
