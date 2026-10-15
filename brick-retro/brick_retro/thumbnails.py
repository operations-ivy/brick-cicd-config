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

import difflib
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


# Roman numerals in titles, as digits ("Mega Man VII" and "Mega Man 7"). Not
# "I" or "X": too many titles use them as letters ("Mega Man X", "X-Men").
ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}
SKIP_WORDS = {"the", "a", "an", "and"}
FUZZY = 0.9  # difflib ratio for the last-resort match


def _words(name: str) -> list[str]:
    bare = re.sub(r"\(.*?\)|\[.*?\]", " ", name).lower().replace("'", "")
    words = re.findall(r"[a-z0-9]+", re.sub(r"[&_]", " ", bare))
    return [ROMAN.get(w, w) for w in words if w not in SKIP_WORDS]


def key(name: str) -> str:
    """A title without tags, articles, numbering style or punctuation, to
    match across naming schemes: "7th Saga, The (U) [!]" and "7th Saga, The
    (USA)" agree, as do "A Nightmare on Elm Street" and "Nightmare on Elm
    Street, A", and "Mega Man VII" and "Mega Man 7". The server's own names
    have "_" for "&", ":" and the rest, so those and "and" count as nothing."""
    return "".join(_words(name))


def base(name: str) -> str:
    """The key of the title before any " - subtitle"."""
    return key(re.split(r" - ", re.sub(r"_", " ", name), maxsplit=1)[0])


class Index:
    """A system's server names for one kind, looked up several ways."""

    def __init__(self, names: list[str]):
        self.names = names
        self.by_key: dict[str, list[str]] = {}
        self.by_base: dict[str, set[str]] = {}
        self.base_of: dict[str, str] = {}
        for n in names:
            self.by_key.setdefault(key(n), []).append(n)
            self.by_base.setdefault(base(n), set()).add(key(n))
            self.base_of[key(n)] = base(n)

    def candidates(self, name: str) -> list[str]:
        """Server names for one game, from the surest way that finds any:
        the same title; the same title where only one side has a subtitle;
        the title without a "Disney's"-style prefix; then a near spelling.
        Only when exactly one title fits, so a guess never picks between
        games."""
        k = key(name)
        if not k:
            return []
        if k in self.by_key:
            return self.by_key[k]
        # One side has a subtitle, the other none ("Street Fighter II - The
        # World Warrior" and "Street Fighter II"; "super_mario_advance_2" and
        # "Super Mario Advance 2 - Super Mario World"). Two different
        # subtitles are two different games.
        mine_has_sub = base(name) != k
        for keys in (self.by_base.get(base(name), set()) if mine_has_sub else set(),
                     self.by_base.get(k, set()) if not mine_has_sub else set()):
            keys = {sk for sk in keys if mine_has_sub != (self.base_of[sk] != sk)}
            # One game under longer and shorter names ("Super Mario World"
            # and "Super Mario World + Mario Brothers") is still one game.
            shortest = min(keys, key=len, default="")
            if keys and all(sk.startswith(shortest) for sk in keys):
                return [n for sk in sorted(keys) for n in self.by_key[sk]]
        words = _words(name)
        if len(words) > 1 and re.search(r"\w's\b", name.split(" ")[0]):
            rest = "".join(words[1:])
            if rest in self.by_key:
                return self.by_key[rest]
        # A near spelling, for typos ("Daedalean"/"Daedalian"): long enough to
        # mean something, and never a different number ("NHL Hockey 96" is
        # not "95", "Super Mario Advance 2" not "Super Mario Advance").
        if len(k) < 8:
            return []
        digits = re.findall(r"\d+", k)
        close = [c for c in difflib.get_close_matches(k, list(self.by_key), n=5, cutoff=FUZZY)
                 if re.findall(r"\d+", c) == digits][:2]
        if len(close) == 1 or (len(close) == 2 and difflib.SequenceMatcher(None, k, close[0]).ratio()
                               > difflib.SequenceMatcher(None, k, close[1]).ratio() + 0.03):
            return self.by_key[close[0]]
        return []


def regions(name: str) -> list[str]:
    """The regions a name says it's from: No-Intro's "(USA, Europe)" or GoodTools' "(U)"."""
    found = []
    for tag in re.findall(r"\(([^)]*)\)", name):
        if tag.lower() in GOODTOOLS:
            found.append(GOODTOOLS[tag.lower()])
        else:
            found += [r.strip() for r in tag.split(",") if r.strip() in REGIONS + tuple(GOODTOOLS.values())]
    return found


def best(name: str, index: "Index") -> str | None:
    """The server name to use for a game (its ROM's file name), or None."""
    names = index.candidates(name)
    if not names:
        return None
    wanted = regions(name) + [r for r in REGIONS if r not in regions(name)]

    def rank(n: str) -> tuple:
        theirs = regions(n)
        place = min((wanted.index(r) for r in theirs if r in wanted), default=len(wanted))
        # Released before unreleased, the wanted region first, the plainest name.
        return (bool(UNRELEASED.search(n)), place, len(n), n)

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


# Bump when matching changes: the remembered misses are tried again.
MATCHER = 2


def playlist_games(lpl: Path) -> dict[str, str]:
    """Each game's label and the file name it's matched by (its ROM's, with
    the region and other tags games.label() takes off the label)."""
    out: dict[str, str] = {}
    for item in json.loads(lpl.read_text()).get("items", []):
        out.setdefault(item["label"], Path(item.get("path", item["label"])).stem)
    return out


def adopt(folder: Path, label: str, source: str) -> None:
    """Rename a thumbnail fetched under the game's old label (its file name,
    before games.label() cleaned labels up) to the new one, rather than
    downloading it again."""
    target = folder / file_name(label)
    if target.is_file():
        return
    for old in (source, source.replace("_", " ")):
        if old != label and (folder / file_name(old)).is_file():
            (folder / file_name(old)).rename(target)
            return


def sync(playlist_dir: Path, thumb_dir: Path, state: Path, rate: float = 1_000_000,
         now: float | None = None, index_fn=index, fetch_fn=fetch) -> dict[str, dict[str, int]]:
    """Fetch every missing thumbnail for every playlist. Returns, per system,
    how many games have each kind and how many games there are."""
    now = time.time() if now is None else now
    try:
        saved = json.loads(state.read_text())
    except (OSError, ValueError):
        saved = {}
    misses = saved.get("misses", {}) if saved.get("matcher") == MATCHER else {}
    misses = {k: t for k, t in misses.items() if now - t < MISS_DAYS * 86400}
    limiter = Limiter(rate)
    counts: dict[str, dict[str, int]] = {}
    for lpl in sorted(playlist_dir.glob("*.lpl")):
        system = lpl.stem
        games = playlist_games(lpl)
        have = counts.setdefault(system, {"games": len(games)})
        for kind in KINDS:
            folder = thumb_dir / system / kind
            for label, source in games.items():
                adopt(folder, label, source)
            missing = [lb for lb in sorted(games) if not (folder / file_name(lb)).is_file()
                       and f"{system}/{kind}/{lb}" not in misses]
            if missing:
                try:
                    found = Index(index_fn(system, kind))
                except Exception as e:  # the server's down or we're offline: try tomorrow
                    log.warning("no thumbnail list for %s %s: %s", system, kind, e)
                    found = None
                fetched = 0
                for label in missing:
                    name = best(games[label], found) if found else None
                    if name is None:
                        if found:
                            misses[f"{system}/{kind}/{label}"] = now
                        continue
                    try:
                        fetch_fn(system, kind, name, folder / file_name(label), limiter)
                        fetched += 1
                    except Exception as e:
                        log.warning("thumbnail not fetched (%s): %s", name, e)
                if fetched:
                    log.info("%s %s: fetched %d", system, kind, fetched)
            have[kind] = sum(1 for lb in games if (folder / file_name(lb)).is_file())
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"matcher": MATCHER, "misses": misses}))
    return counts
