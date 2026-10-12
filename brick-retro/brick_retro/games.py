"""RetroArch playlists for the synced ROMs, and the one-time import of the
old RetroPie saves."""

import json
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class System:
    folder: str  # under the ROM directory, as on brick2000's /extra/games/roms
    playlist: str  # RetroArch's name for the system (its database name)
    core: str  # libretro core file
    core_name: str
    extensions: tuple[str, ...]


SYSTEMS = (
    System("nes", "Nintendo - Nintendo Entertainment System", "nestopia_libretro.so", "Nestopia",
           (".nes", ".zip")),
    System("snes", "Nintendo - Super Nintendo Entertainment System", "snes9x_libretro.so", "Snes9x",
           (".smc", ".sfc", ".zip")),
)


def roms(folder: Path, extensions: tuple[str, ...]) -> list[Path]:
    """Every ROM under folder (RetroPie keeps NES in A-Z subfolders), by name."""
    if not folder.is_dir():
        return []
    found = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in extensions]
    return sorted(found, key=lambda p: p.stem.lower())


def playlist(system: System, rom_dir: Path, core_dir: Path) -> dict:
    core = str(core_dir / system.core)
    return {
        "version": "1.5",
        "default_core_path": core,
        "default_core_name": system.core_name,
        "label_display_mode": 0,
        "right_thumbnail_mode": 0,
        "left_thumbnail_mode": 0,
        "sort_mode": 0,
        "items": [{"path": str(p), "label": p.stem, "core_path": core, "core_name": system.core_name,
                   "crc32": "DETECT", "db_name": f"{system.playlist}.lpl"}
                  for p in roms(rom_dir / system.folder, system.extensions)],
    }


def write_playlists(rom_dir: Path, playlist_dir: Path, core_dir: Path) -> dict[str, int]:
    """Rewrite each system's playlist; returns how many games each has."""
    playlist_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for system in SYSTEMS:
        data = playlist(system, rom_dir, core_dir)
        target = playlist_dir / f"{system.playlist}.lpl"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(target)
        counts[system.folder] = len(data["items"])
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
