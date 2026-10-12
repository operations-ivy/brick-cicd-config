"""Sentry: watch Bluetooth LE adverts around brick1982 for trackers and
surveillance gadgets, for the arena's sentry view.

A proof of concept (wigle-sync#35). Signatures come from the Bluetooth SIG's
assigned numbers and published protocol notes; which ones to look for follows
SquachWatch-CYD's list, but no data is copied from it. Each has a confidence:
only high and medium ones raise alerts.

Privacy: device addresses never leave this process. Each one is hashed with a
per-start salt, and only kinds, signal strengths and ages reach /api/state.
Nothing is written to disk.

Households are full of trackers, so the first `learn_seconds` learn the
normal count of each kind; an alert is a kind above its normal count for
`linger_seconds`. Alerts count only devices heard in the last
`present_seconds`; the radar keeps showing one for `gone_seconds`.
"""

import asyncio
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


def uuid16(n: int) -> str:
    return f"0000{n:04x}-0000-1000-8000-00805f9b34fb"


@dataclass(frozen=True)
class Kind:
    key: str
    label: str
    confidence: str  # high, medium, low


KINDS = {k.key: k for k in (
    Kind("findmy", "Apple Find My tag", "high"),
    Kind("smarttag", "Samsung SmartTag", "high"),
    Kind("tile", "Tile tracker", "high"),
    Kind("googletag", "Google Find My tag", "medium"),
    Kind("meta", "Meta device (glasses?)", "medium"),
    Kind("flipper", "Flipper Zero", "high"),
    Kind("flipperish", "Flipper-like service", "low"),
    Kind("skimmer", "Serial BT module (skimmer?)", "low"),
)}

APPLE, FINDMY_TYPE = 0x004C, 0x12
SKIMMER_NAMES = ("HC-05", "HC-06", "HC-08", "BT04-A", "RNBT-")


def classify(name: str | None, manufacturer: dict[int, bytes], services: list[str],
             service_data: dict[str, bytes]) -> str | None:
    """The kind of one advert, or None for everything else."""
    name = name or ""
    services = [s.lower() for s in services] + [s.lower() for s in service_data]
    apple = manufacturer.get(APPLE)
    if apple and apple[0] == FINDMY_TYPE:
        return "findmy"  # Find My offline finding: an AirTag, or an Apple device away from its owner
    if uuid16(0xFD5A) in services:
        return "smarttag"
    if uuid16(0xFEED) in services or uuid16(0xFEEC) in services:
        return "tile"
    fmdn = service_data.get(uuid16(0xFEAA)) or b""
    if fmdn[:1] in (b"\x40", b"\x41"):
        return "googletag"  # Eddystone frame types Google's Find My Device network uses
    if uuid16(0xFD5F) in services:
        return "meta"
    if name.startswith("Flipper "):
        return "flipper"
    if any(uuid16(n) in services for n in (0x3081, 0x3082, 0x3083)):
        return "flipperish"
    if name.startswith(SKIMMER_NAMES):
        return "skimmer"
    return None


@dataclass
class Sighting:
    kind: str
    first: float
    last: float
    rssi: int


class Watch:
    def __init__(self, learn_seconds: float = 600, linger_seconds: float = 120, gone_seconds: float = 120,
                 present_seconds: float = 30, started: float | None = None):
        self.learn_seconds, self.linger_seconds, self.gone_seconds = learn_seconds, linger_seconds, gone_seconds
        self.present_seconds = present_seconds
        self.started = time.time() if started is None else started
        self.salt = os.urandom(16)
        self.devices: dict[str, Sighting] = {}
        self.normal: dict[str, int] = {}
        self.over_since: dict[str, float] = {}
        self.alerts: list[dict] = []  # newest last, kept 30 minutes
        self.error = ""
        self._lock = threading.Lock()

    def _key(self, address: str) -> str:
        return hashlib.sha256(self.salt + address.encode()).hexdigest()[:10]

    def advert(self, address: str, kind: str, rssi: int, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            key = self._key(address)
            s = self.devices.get(key)
            if s is None or now - s.last > self.gone_seconds:
                self.devices[key] = Sighting(kind, now, now, rssi)
            else:
                s.last, s.rssi = now, rssi

    def _nearby(self, now: float) -> dict[str, list[tuple[str, Sighting]]]:
        by_kind: dict[str, list[tuple[str, Sighting]]] = {}
        for key, s in list(self.devices.items()):
            if now - s.last > self.gone_seconds:
                del self.devices[key]
                continue
            by_kind.setdefault(s.kind, []).append((key, s))
        return by_kind

    def tick(self, now: float | None = None) -> None:
        """Learn, or compare each kind with its normal count."""
        now = time.time() if now is None else now
        with self._lock:
            nearby = {k: [d for d in v if now - d[1].last <= self.present_seconds]
                      for k, v in self._nearby(now).items()}
            nearby = {k: v for k, v in nearby.items() if v}
            learning = now - self.started < self.learn_seconds
            for kind, devs in nearby.items():
                if learning:
                    self.normal[kind] = max(self.normal.get(kind, 0), len(devs))
                    continue
                if KINDS[kind].confidence == "low" or len(devs) <= self.normal.get(kind, 0):
                    self.over_since.pop(kind, None)
                    continue
                since = self.over_since.setdefault(kind, now)
                if now - since >= self.linger_seconds and not any(
                        a["kind"] == kind and a["at"] >= since for a in self.alerts):
                    newest = max(devs, key=lambda d: d[1].first)[1]
                    self.alerts.append({"kind": kind, "label": KINDS[kind].label,
                                        "confidence": KINDS[kind].confidence, "at": now,
                                        "count": len(devs), "normal": self.normal.get(kind, 0),
                                        "rssi": newest.rssi})
                    log.info("sentry alert: %s (%d nearby, normally %d)", kind, len(devs),
                             self.normal.get(kind, 0))
            for kind in list(self.over_since):
                if kind not in nearby:
                    del self.over_since[kind]
            self.alerts = [a for a in self.alerts if now - a["at"] < 1800]

    def get(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            nearby = self._nearby(now)
            return {
                "learning": max(0, round(self.learn_seconds - (now - self.started))),
                "error": self.error,
                "kinds": [{"kind": k, "label": KINDS[k].label, "confidence": KINDS[k].confidence,
                           "count": len(v), "normal": self.normal.get(k, 0)}
                          for k, v in sorted(nearby.items())],
                "blips": [{"id": key, "kind": s.kind, "rssi": s.rssi, "age": round(now - s.first)}
                          for devs in nearby.values() for key, s in devs],
                "alerts": list(self.alerts),
            }


def run(watch: Watch) -> None:
    """Scan forever (a daemon thread). Without bleak or an adapter, say so in
    the view and keep retrying; the rest of the arena carries on."""
    try:
        from bleak import BleakScanner  # apt: python3-bleak
    except ImportError:
        watch.error = "python3-bleak not installed"
        log.warning("sentry off: %s", watch.error)
        return

    def on_advert(device, adv) -> None:
        kind = classify(adv.local_name, dict(adv.manufacturer_data), list(adv.service_uuids),
                        dict(adv.service_data))
        if kind:
            watch.advert(device.address, kind, adv.rssi)

    async def scan() -> None:
        async with BleakScanner(on_advert):
            watch.error = ""
            while True:
                watch.tick()
                await asyncio.sleep(2)

    while True:
        try:
            asyncio.run(scan())
        except Exception as e:  # adapter blocked or gone, bluetoothd restarting
            watch.error = "no Bluetooth"
            log.warning("sentry scan failed: %s", e)
        time.sleep(30)
