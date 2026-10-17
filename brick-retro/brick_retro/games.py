"""RetroArch playlists for the synced ROMs, and the one-time import of the
old RetroPie saves."""

import json
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

# Built from source by install.sh: Debian packages none of these cores.
PCSX_REARMED = "/usr/local/lib/libretro/pcsx_rearmed_libretro.so"
PICODRIVE = "/usr/local/lib/libretro/picodrive_libretro.so"
PPSSPP = "/usr/local/lib/libretro/ppsspp_libretro.so"
FLYCAST = "/usr/local/lib/libretro/flycast_libretro.so"
FBNEO = "/usr/local/lib/libretro/fbneo_neogeo_libretro.so"
# FinalBurn Neo's list of the Neo Geo games it runs (its dats/ folder,
# installed beside the core by install.sh): names the zips, says which are clones.
FBNEO_NEOGEO_DAT = "/usr/local/lib/libretro/fbneo_neogeo.dat"


@dataclass(frozen=True)
class System:
    folder: str  # under the ROM directory, as on brick2000's /extra/games/roms
    playlist: str  # RetroArch's name for the system (its database name)
    core: str  # libretro core file, in the core directory unless absolute
    core_name: str
    extensions: tuple[str, ...]
    discs: bool = False  # CD images: cue sheets, multi-disc sets (see disc_games)
    arcade_dat: str = ""  # arcade zips: the core's game list (see arcade_games)
    needs: str = ""  # a BIOS file in the system directory without which no game starts
    drop: str = ""  # a regex taken out of each file name for its label


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
    # PicoDrive, not Debian's Genesis Plus GX: that one is built without CHD
    # support, and every Sega CD game on brick2000 is a .chd. BIOS:
    # bios_CD_U.bin (and _E, _J) in the system directory.
    System("segacd", "Sega - Mega-CD - Sega CD", PICODRIVE, "PicoDrive", (".chd", ".cue"), discs=True),
    # GoodTools names carry the system ("Doom 32X (JU) [!]"): without it the
    # label is "Doom", which the thumbnail server's "Doom (Japan, USA)" matches.
    System("sega32x", "Sega - 32X", PICODRIVE, "PicoDrive", (".32x", ".bin"), drop=r"\s+32X\b"),
    # Only disc images: the .pbp files in brick2000's psp folder are
    # PlayStation games converted for the PSP, which PPSSPP can't run.
    # Scene names ("Crisis_Core_Final_Fantasy_VII_USA_READNFO_PSP-Googlecus")
    # lose everything from the region on.
    System("psp", "Sony - PlayStation Portable", PPSSPP, "PPSSPP", (".iso", ".cso"),
           drop=r"[_.](?:USA|EUR|JPN|JAP|ASIA)(?:[_.].*)?$"),
    System("Dreamcast", "Sega - Dreamcast", FLYCAST, "Flycast", (".chd", ".cue", ".gdi"), discs=True),
    # FinalBurn Neo checks the BIOS's checksums, and brick2000's old
    # neogeo.zip (MAME 0.78's) fails them: no list until a current one is there.
    System("neogeo", "SNK - Neo Geo", FBNEO, "FinalBurn Neo", (".zip",), arcade_dat=FBNEO_NEOGEO_DAT,
           needs="neogeo.zip"),
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


# Tags label() takes off a game's name. The files keep them: RetroArch names
# saves after the file, and brick2000's copy is the one sync-roms mirrors.
_REGION_CODES = re.compile(r"[UEJWFGSIKABCHNDR4]{1,4}")  # GoodTools: (U), (JUE), (4)
_REGIONS = {"usa", "europe", "japan", "world", "asia", "australia", "brazil", "canada", "china", "france",
            "germany", "italy", "korea", "netherlands", "spain", "sweden", "uk", "taiwan", "hong kong"}
_LANGUAGES = {"en", "fr", "de", "es", "it", "ja", "nl", "pt", "sv", "no", "da", "fi", "zh", "ko", "pl", "ru",
              "fre", "ger", "spa", "ita", "chinese", "english", "french", "german", "spanish", "japanese", "unl"}
_NOISE = re.compile(r"(rev|v|version)\s*[\d.a-z]*|[a-z]{3} \d{4}|\d{4}-\d{2}-\d{2}|ntsc(-[uj])?|pal|"
                    r"s[lc][eu]s-\d+|en,.*|.*(,\s*)(en|fr|de|es|it|ja)\b.*", re.IGNORECASE)


def _noise(tag: str) -> bool:
    t = tag.strip()
    if _REGION_CODES.fullmatch(t) or _NOISE.fullmatch(t):
        return True
    parts = [x.strip().lower() for x in t.split(",")]
    return all(x in _REGIONS or x in _LANGUAGES for x in parts)


def label(stem: str) -> str:
    """A game's name for the list, from its file name: "7th Saga, The (U) [!]"
    is "7th Saga, The". Region, language, revision and date tags and GoodTools'
    dump marks ([!], [a1], [b1], [p1], [f1], [c], [S]) go; what says something
    stays: (Beta), (Proto), (Disc 2), a multicart's name, and translations and
    hacks as "(Translated)" and "(Hack)"."""
    name = stem if " " in stem else stem.replace("_", " ")
    extra = []
    for tag in re.findall(r"\[([^\]]*)\]", name):
        if re.match(r"T[+-]", tag):
            extra.append("(Translated)")
        elif re.fullmatch(r"h\d*.*", tag):
            extra.append("(Hack)")
    name = re.sub(r"\[[^\]]*\]", " ", name)
    name = re.sub(r"\(([^)]*)\)", lambda m: " " if _noise(m.group(1)) else m.group(0), name)
    name = re.sub(r"\s+,", ",", re.sub(r"\s+", " ", name)).strip(" -")
    for e in extra:
        if e not in name:
            name = f"{name} {e}"
    return name or stem


def _unique(entries: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    """Clean labels, putting a tag back where two games would look the same
    (ActRaiser (U) and ActRaiser (E)): first the region, else the file name."""
    labels = [label(name) for _, name in entries]
    counts: dict[str, int] = {}
    for lb in labels:
        counts[lb] = counts.get(lb, 0) + 1
    out = []
    for (path, name), lb in zip(entries, labels):
        if counts[lb] > 1:
            tags = [t for t in re.findall(r"\(([^)]*)\)", name) if _noise(t)]
            lb = f"{lb} ({tags[0]})" if tags else name
        out.append((path, lb))
    seen: dict[str, int] = {}
    for i, (path, lb) in enumerate(out):  # still the same (two "(U)" dumps): the file name
        seen[lb] = seen.get(lb, 0) + 1
    return [(path, entries[i][1] if seen[lb] > 1 else lb) for i, (path, lb) in enumerate(out)]


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


_BOARD = re.compile(r"\s*\([A-Z]{3}-\d[^)]*\)")  # (NGM-2420), (NGM-063 ~ NGH-063)


def _crcs(zip_path: Path) -> set[str]:
    """The CRC32 of each file in a zip, from its directory (nothing is unpacked)."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            return {f"{i.CRC:08x}" for i in z.infolist()}
    except (OSError, zipfile.BadZipFile):
        return set()


def arcade_games(folder: Path, dat: Path) -> list[tuple[Path, str]]:
    """Arcade zips the core will start, by their name in its list ("Metal Slug -
    Super Vehicle-001" for mslug.zip). The core checks every ROM's checksum, so
    a zip is listed only when it (with its parent beside it, for a clone) holds
    every ROM the list asks for, BIOS aside: an older dump of a game fails to
    start. Its BIOS isn't a game, a zip it doesn't know is left out, and a
    clone (a bootleg, another region) only shows when its parent doesn't.
    Board numbers go, and of "Puzzled / Joy Joy Kid" the first name stays."""
    known = {g.get("name"): g for g in ET.parse(dat).getroot().iter("game")}
    bios = {r.get("crc") for g in known.values() if g.get("isbios") == "yes" for r in g.iter("rom")}
    zips = {p.stem: p for p in roms(folder, (".zip",))}
    playable = {}
    for name, path in zips.items():
        g = known.get(name)
        if g is None or g.get("isbios") == "yes":
            continue
        have = _crcs(path)
        parent = zips.get(g.get("cloneof", ""))
        if parent is not None and parent.parent == path.parent:  # the core looks beside the clone
            have |= _crcs(parent)
        need = {r.get("crc") for r in g.iter("rom") if r.get("crc") and r.get("status") != "nodump"}
        if need - bios <= have:
            playable[name] = (path, g)
    entries = []
    for name, (path, g) in playable.items():
        if g.get("cloneof") in playable:
            continue
        text = _BOARD.sub("", g.findtext("description", name))
        entries.append((path, text.split(" / ")[0].strip()))
    return sorted(entries, key=lambda e: e[1].lower())


def clean_name(system: System, stem: str) -> str:
    """A ROM's file name less what its system drops ("Doom 32X (JU)" is "Doom
    (JU)"); a name with no spaces left has its _ and . as spaces
    ("Breath.of.Fire.III.EUR.PSP-PGS" is "Breath of Fire III"). The menu's
    label and the thumbnail match both start from this."""
    if not system.drop:
        return stem
    name = re.sub(system.drop, "", stem)
    return name if " " in name else re.sub(r"[_.]", " ", name)


def games(system: System, rom_dir: Path, m3u_dir: Path) -> list[tuple[Path, str]]:
    """(file, label) for each of a system's games."""
    folder = rom_dir / system.folder
    if system.arcade_dat:
        if not folder.is_dir() or not Path(system.arcade_dat).is_file():
            return []
        return _unique(arcade_games(folder, Path(system.arcade_dat)))
    if system.discs:
        return _unique(disc_games(folder, system.extensions, m3u_dir / system.folder))
    # The same game in two places (brick2000's SNES folder has many both at
    # the top and in an A-Z subfolder) is one entry: RetroArch keys saves by
    # file name, so both copies share one save anyway.
    seen, entries = set(), []
    for p in sorted(roms(folder, system.extensions), key=lambda p: (p.stem.lower(), len(p.parts), str(p))):
        if p.stem.lower() not in seen:
            seen.add(p.stem.lower())
            entries.append((p, clean_name(system, p.stem)))
    return _unique(entries)


def playlist(system: System, rom_dir: Path, core_dir: Path, m3u_dir: Path, system_dir: Path) -> dict:
    core = str(core_dir / system.core)
    found = [] if system.needs and not (system_dir / system.needs).is_file() else games(system, rom_dir, m3u_dir)
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
                  for p, label in found],
    }


def write_playlists(rom_dir: Path, playlist_dir: Path, core_dir: Path, m3u_dir: Path,
                    system_dir: Path) -> dict[str, int]:
    """Rewrite each system's playlist; returns how many games each has. A
    system with no games (or without the BIOS it needs) gets no playlist, so
    it isn't an empty or broken entry in RetroArch's list."""
    playlist_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for system in SYSTEMS:
        data = playlist(system, rom_dir, core_dir, m3u_dir, system_dir)
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
