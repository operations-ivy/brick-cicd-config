"""RetroArch playlists for the synced ROMs, and the one-time import of the
old RetroPie saves."""

import json
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

# Built from source by install.sh: Debian packages no PlayStation core.
PCSX_REARMED = "/usr/local/lib/libretro/pcsx_rearmed_libretro.so"


@dataclass(frozen=True)
class System:
    folder: str  # under the ROM directory, as on brick2000's /extra/games/roms
    playlist: str  # RetroArch's name for the system (its database name)
    core: str  # libretro core file, in the core directory unless absolute
    core_name: str
    extensions: tuple[str, ...]
    discs: bool = False  # CD images: cue sheets, multi-disc sets (see disc_games)


SYSTEMS = (
    System("nes", "Nintendo - Nintendo Entertainment System", "nestopia_libretro.so", "Nestopia",
           (".nes", ".zip")),
    System("snes", "Nintendo - Super Nintendo Entertainment System", "snes9x_libretro.so", "Snes9x",
           (".smc", ".sfc", ".zip")),
    System("gb", "Nintendo - Game Boy", "gambatte_libretro.so", "Gambatte", (".gb", ".zip")),
    System("gbc", "Nintendo - Game Boy Color", "gambatte_libretro.so", "Gambatte", (".gbc", ".zip")),
    System("gba", "Nintendo - Game Boy Advance", "mgba_libretro.so", "mGBA", (".gba", ".zip")),
    System("megadrive", "Sega - Mega Drive - Genesis", "genesis_plus_gx_libretro.so", "Genesis Plus GX",
           (".md", ".gen", ".smd", ".bin", ".zip")),
    System("mastersystem", "Sega - Master System - Mark III", "genesis_plus_gx_libretro.so",
           "Genesis Plus GX", (".sms", ".zip")),
    System("psx", "Sony - PlayStation", PCSX_REARMED, "PCSX-ReARMed",
           (".chd", ".cue", ".ccd", ".pbp", ".bin"), discs=True),
)


def roms(folder: Path, extensions: tuple[str, ...]) -> list[Path]:
    """Every ROM under folder (RetroPie keeps NES in A-Z subfolders), by name."""
    if not folder.is_dir():
        return []
    found = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in extensions]
    return sorted(found, key=lambda p: p.stem.lower())


DISC = re.compile(r"\s*\(disc (\d+)\)", re.IGNORECASE)
BIOS = re.compile(r"scph\d+", re.IGNORECASE)  # a PlayStation BIOS left beside the games


def title(stem: str) -> str:
    """A game's name without tags, to spot the same game in two formats:
    "metal_gear_solid_(v1.1)_(disc_1)" and "Metal Gear Solid (USA) (Disc 1)"."""
    bare = re.sub(r"\(.*?\)|\[.*?\]", " ", stem.replace("_", " "))
    return re.sub(r"[^a-z0-9]+", "", bare.lower())


def cue_files(cue: Path) -> list[Path]:
    """The track files a cue sheet names."""
    names = re.findall(r'^\s*FILE\s+"(.+?)"', cue.read_text(errors="replace"), re.MULTILINE)
    return [cue.parent / n for n in names]


def disc_games(folder: Path, extensions: tuple[str, ...], m3u_dir: Path) -> list[tuple[Path, str]]:
    """CD games, one entry per game: a .chd wins over a .bin/.cue copy of the
    same game, a .cue over a .ccd of the same image, tracks a cue sheet names
    aren't games themselves (nor is a BIOS), an image whose
    tracks are missing (still .ecm-packed, say) is left out, and a game on
    several discs is one .m3u (written to m3u_dir), so it keeps one memory card
    and RetroArch can swap discs."""
    files = roms(folder, extensions)
    tracks = set()
    playable = []
    for p in files:
        suffix = p.suffix.lower()
        if suffix == ".cue":
            named = cue_files(p)
            tracks.update(named)
            if named and all(t.is_file() for t in named):
                playable.append(p)
        elif suffix == ".ccd":
            if p.with_suffix(".img").is_file():
                playable.append(p)
        elif suffix != ".bin":
            playable.append(p)
    cues = {p.with_suffix("") for p in playable if p.suffix.lower() == ".cue"}
    playable = [p for p in playable if not (p.suffix.lower() == ".ccd" and p.with_suffix("") in cues)]
    playable += [p for p in files
                 if p.suffix.lower() == ".bin" and p not in tracks and not BIOS.fullmatch(p.stem)]
    chd = {title(p.stem) for p in playable if p.suffix.lower() == ".chd"}
    playable = [p for p in playable if p.suffix.lower() == ".chd" or title(p.stem) not in chd]

    sets = defaultdict(list)
    for p in playable:
        sets[(p.parent, DISC.sub("", p.stem))].append(p)
    m3u_dir.mkdir(parents=True, exist_ok=True)
    written = set()
    entries = []
    for (_, name), discs in sets.items():
        if len(discs) > 1 and all(DISC.search(d.stem) for d in discs):
            discs.sort(key=lambda d: int(DISC.search(d.stem).group(1)))
            m3u = m3u_dir / f"{name}.m3u"
            m3u.write_text("".join(f"{d}\n" for d in discs))
            written.add(m3u)
            entries.append((m3u, name))
        else:
            entries += [(d, d.stem.replace("_", " ")) for d in discs]
    for stale in m3u_dir.glob("*.m3u"):
        if stale not in written:
            stale.unlink()
    return sorted(entries, key=lambda e: e[1].lower())


def games(system: System, rom_dir: Path, m3u_dir: Path) -> list[tuple[Path, str]]:
    """(file, label) for each of a system's games."""
    folder = rom_dir / system.folder
    if system.discs:
        return disc_games(folder, system.extensions, m3u_dir / system.folder)
    # The same game in two places (brick2000's SNES folder has many both at
    # the top and in an A-Z subfolder) is one entry: RetroArch keys saves by
    # file name, so both copies share one save anyway.
    seen, entries = set(), []
    for p in sorted(roms(folder, system.extensions), key=lambda p: (p.stem.lower(), len(p.parts), str(p))):
        if p.stem.lower() not in seen:
            seen.add(p.stem.lower())
            entries.append((p, p.stem))
    return entries


def playlist(system: System, rom_dir: Path, core_dir: Path, m3u_dir: Path) -> dict:
    core = str(core_dir / system.core)
    return {
        "version": "1.5",
        "default_core_path": core,
        "default_core_name": system.core_name,
        "label_display_mode": 0,
        "right_thumbnail_mode": 0,
        "left_thumbnail_mode": 0,
        "sort_mode": 0,
        "items": [{"path": str(p), "label": label, "core_path": core, "core_name": system.core_name,
                   "crc32": "DETECT", "db_name": f"{system.playlist}.lpl"}
                  for p, label in games(system, rom_dir, m3u_dir)],
    }


def write_playlists(rom_dir: Path, playlist_dir: Path, core_dir: Path, m3u_dir: Path) -> dict[str, int]:
    """Rewrite each system's playlist; returns how many games each has. A
    system with no games gets no playlist, so it isn't an empty entry in
    RetroArch's list."""
    playlist_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for system in SYSTEMS:
        data = playlist(system, rom_dir, core_dir, m3u_dir)
        target = playlist_dir / f"{system.playlist}.lpl"
        counts[system.folder] = len(data["items"])
        if not data["items"]:
            target.unlink(missing_ok=True)
            continue
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(target)
    return counts


def import_saves(rom_dir: Path, save_dir: Path) -> list[str]:
    """Copy RetroPie's saves (Game.srm beside Game.smc) into RetroArch's save
    directory, never over a save that's already there."""
    save_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for system in SYSTEMS:
        folder = rom_dir / system.folder
        if not folder.is_dir():
            continue
        for srm in sorted(folder.rglob("*.srm")):
            target = save_dir / srm.name
            if not target.exists():
                shutil.copy2(srm, target)
                copied.append(srm.name)
    return copied
