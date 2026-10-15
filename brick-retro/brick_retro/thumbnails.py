"""Box art, title screens and screenshots for every game in the playlists,
from libretro's thumbnail server.

RetroArch looks a game's thumbnails up by its playlist label, and its own
updater asks the server for exactly that name. brick9000's labels are the ROM
file names (GoodTools tags like "7th Saga, The (U) [!]", or none at all), while
the server uses No-Intro names ("7th Saga, The (USA)"), so most would miss.
Instead this reads the server's list for each system, matches each game by its
bare title (preferring its own region, then USA), and saves the image under
the name RetroArch will look for.

Only missing images are fetched, at a capped rate (brick9000's Wi-Fi is
shared); a game with no match isn't asked about again for MISS_DAYS.

    python3 -m brick_retro thumbnails
"""

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

SERVER = "https://thumbnails.libretro.com"
KINDS = ("Named_Boxarts", "Named_Titles", "Named_Snaps")
MISS_DAYS = 30
# RetroArch replaces these in a label to make the thumbnail's file name.
UNSAFE = re.compile(r'[&*/:`<>?\\|"]')
# GoodTools region codes, as No-Intro region names.
GOODTOOLS = {"u": "USA", "e": "Europe", "j": "Japan", "w": "World", "ue": "USA", "ju": "USA",
             "f": "France", "g": "Germany", "s": "Spain", "i": "Italy", "k": "Korea", "a": "Australia"}
REGIONS = ("USA", "World", "Europe", "Japan")
# Server entries to pass over while a release version exists.
UNRELEASED = re.compile(r"\((Beta|Proto|Sample|Demo|Kiosk|Pirate|Unl)[^)]*\)|Virtual Console", re.I)


def file_name(label: str) -> str:
    """The thumbnail file RetroArch looks for, for a playlist label."""
    return UNSAFE.sub("_", label) + ".png"


def key(name: str) -> str:
    """A title without tags, articles or punctuation, to match across naming
    schemes: "7th Saga, The (U) [!]" and "7th Saga, The (USA)" agree. The
    server's own names have "_" for "&", ":" and the rest, so those and "and"
    all count the same (as nothing)."""
    bare = re.sub(r"\(.*?\)|\[.*?\]", " ", name).lower()
    bare = re.sub(r"[&_]|\b(the|and)\b", " ", bare)
    return re.sub(r"[^a-z0-9]+", "", bare)


def regions(name: str) -> list[str]:
    """The regions a name says it's from: No-Intro's "(USA, Europe)" or GoodTools' "(U)"."""
    found = []
    for tag in re.findall(r"\(([^)]*)\)", name):
        if tag.lower() in GOODTOOLS:
            found.append(GOODTOOLS[tag.lower()])
        else:
            found += [r.strip() for r in tag.split(",") if r.strip() in REGIONS + tuple(GOODTOOLS.values())]
    return found


def best(label: str, by_key: dict[str, list[str]]) -> str | None:
    """The server name to use for a game, or None."""
    names = by_key.get(key(label))
    if not names:
        return None
    wanted = regions(label) + [r for r in REGIONS if r not in regions(label)]

    def rank(name: str) -> tuple:
        theirs = regions(name)
        place = min((wanted.index(r) for r in theirs if r in wanted), default=len(wanted))
        # Released before unreleased, the wanted region first, the plainest name.
        return (bool(UNRELEASED.search(name)), place, len(name), name)

    return min(names, key=rank)


def index(system: str, kind: str, timeout: float = 60) -> list[str]:
    """Every name the server has for a system's thumbnails of one kind."""
    url = f"{SERVER}/{urllib.parse.quote(system)}/{kind}/"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        page = r.read().decode("utf-8", "replace")
    return [urllib.parse.unquote(h)[:-4] for h in re.findall(r'href="([^"/]+\.png)"', page)]


class Limiter:
    """Keeps downloads under `rate` bytes per second, on average."""

    def __init__(self, rate: float):
        self.rate, self.start, self.total = rate, time.monotonic(), 0

    def took(self, size: int) -> None:
        self.total += size
        ahead = self.total / self.rate - (time.monotonic() - self.start)
        if ahead > 0:
            time.sleep(ahead)


def fetch(system: str, kind: str, name: str, target: Path, limiter: Limiter, timeout: float = 60) -> None:
    url = f"{SERVER}/{urllib.parse.quote(system)}/{kind}/{urllib.parse.quote(name)}.png"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data = r.read()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    limiter.took(len(data))


def sync(playlist_dir: Path, thumb_dir: Path, state: Path, rate: float = 1_000_000,
         now: float | None = None, index_fn=index, fetch_fn=fetch) -> dict[str, dict[str, int]]:
    """Fetch every missing thumbnail for every playlist. Returns, per system,
    how many games have each kind and how many games there are."""
    now = time.time() if now is None else now
    try:
        misses = json.loads(state.read_text())
    except (OSError, ValueError):
        misses = {}
    misses = {k: t for k, t in misses.items() if now - t < MISS_DAYS * 86400}
    limiter = Limiter(rate)
    counts: dict[str, dict[str, int]] = {}
    for lpl in sorted(playlist_dir.glob("*.lpl")):
        system = lpl.stem
        labels = sorted({item["label"] for item in json.loads(lpl.read_text()).get("items", [])})
        have = counts.setdefault(system, {"games": len(labels)})
        for kind in KINDS:
            missing = [lb for lb in labels if not (thumb_dir / system / kind / file_name(lb)).is_file()
                       and f"{system}/{kind}/{lb}" not in misses]
            if missing:
                try:
                    names = index_fn(system, kind)
                except Exception as e:  # the server's down or we're offline: try tomorrow
                    log.warning("no thumbnail list for %s %s: %s", system, kind, e)
                    names = []
                by_key: dict[str, list[str]] = {}
                for n in names:
                    by_key.setdefault(key(n), []).append(n)
                fetched = 0
                for label in missing:
                    name = best(label, by_key) if names else None
                    if name is None:
                        if names:
                            misses[f"{system}/{kind}/{label}"] = now
                        continue
                    try:
                        fetch_fn(system, kind, name, thumb_dir / system / kind / file_name(label), limiter)
                        fetched += 1
                    except Exception as e:
                        log.warning("thumbnail not fetched (%s): %s", name, e)
                if fetched:
                    log.info("%s %s: fetched %d", system, kind, fetched)
            have[kind] = sum(1 for lb in labels if (thumb_dir / system / kind / file_name(lb)).is_file())
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(misses))
    return counts
