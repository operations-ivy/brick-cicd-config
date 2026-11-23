"""RetroArch playlists for the movies and shows picked for brick9000.

presto's picker (presto.brick.nozdormu.cloud/media/devices) is one list for
every device; presto's GET /api/media/sync?device=brick9000 says which of
them brick9000 holds (the 480p copies: devices.yaml's `video_480p`). They
stay on brick2000 and play off the movies share, mounted read-only at
/mnt/brick/movies (install.sh), in RetroArch's built-in video player
(the "ffmpeg" core compiled into Debian's RetroArch), so a controller picks
them like games. Two playlists: Movies, and TV (each show's episodes in
order).

If presto can't be reached, the playlists stay as they were: the board and
the games never wait on the cluster.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path, PurePosixPath

MOVIES = "Movies"
TV = "TV"
PLAYLISTS = (MOVIES, TV)
CORE_PATH, CORE_NAME = "builtin", "ffmpeg"  # RetroArch's own video player
SORT_OFF = 2  # PLAYLIST_SORT_MODE_OFF: keep each show's episodes in order


def fetch(url: str, timeout: float = 20) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def entry(path: str) -> tuple[str, str] | None:
    """Which playlist a manifest path goes in, and its label: 480p/movies/X.mp4 is
    Movies "X"; 480p/tv/Show/Show - 01.mp4 is TV "Show - 01" (the show's name
    put in front when the episode's own name lacks it)."""
    parts = PurePosixPath(path).parts
    if len(parts) < 3:
        return None
    stem = PurePosixPath(path).stem
    if parts[1] == "movies":
        return MOVIES, stem
    if parts[1] == "tv" and len(parts) >= 4:
        show = parts[2]
        return TV, stem if stem.casefold().startswith(show.casefold()) else f"{show} - {stem}"
    return None


def playlists(manifest: dict, mount: Path) -> dict[str, dict]:
    """RetroArch's playlist documents, by name, for the manifest's movies share files."""
    items: dict[str, list[dict]] = {name: [] for name in PLAYLISTS}
    for f in sorted(manifest.get("files", []), key=lambda f: f["path"].casefold()):
        if f.get("share") != "movies" or not (where := entry(f["path"])):
            continue
        name, label = where
        items[name].append(
            {
                "path": str(mount / f["path"]),
                "label": label,
                "core_path": CORE_PATH,
                "core_name": CORE_NAME,
                "crc32": "DETECT",
                "db_name": f"{name}.lpl",
            }
        )
    return {
        name: {
            "version": "1.5",
            "default_core_path": CORE_PATH,
            "default_core_name": CORE_NAME,
            "label_display_mode": 0,
            "right_thumbnail_mode": 0,
            "left_thumbnail_mode": 0,
            "sort_mode": SORT_OFF,
            "items": entries,
        }
        for name, entries in items.items()
    }


def write(docs: dict[str, dict], playlist_dir: Path) -> dict[str, int]:
    """Rewrite the video playlists (an empty one is removed); returns their sizes."""
    playlist_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name, doc in docs.items():
        target = playlist_dir / f"{name}.lpl"
        counts[name] = len(doc["items"])
        if not doc["items"]:
            target.unlink(missing_ok=True)
            continue
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2))
        tmp.replace(target)
    return counts


def sync(url: str, mount: Path, playlist_dir: Path) -> dict[str, int]:
    """Fetch brick9000's list from presto and rewrite the playlists."""
    return write(playlists(fetch(url), mount), playlist_dir)
