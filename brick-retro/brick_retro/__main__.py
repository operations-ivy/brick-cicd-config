"""brick-retro: retro games on brick9000 (RetroArch over the board).

    python3 -m brick_retro            # the daemon: launches RetroArch for a pad
    python3 -m brick_retro games      # rewrite the playlists, import old saves

Turning a paired controller on opens RetroArch's game list over the board;
quitting RetroArch, or holding Home for 3 seconds, shows the board again.
Home held + D-pad up/down sets the volume. While RetroArch runs, brick-status keeps the screen on (the flag file).
"""

import logging
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import games
from .pads import ABS_HAT0Y, Launcher, Volume, is_pad

log = logging.getLogger("brick-retro")
HOME = Path.home()


def env(name: str, default: str) -> str:
    return os.environ.get(f"BRICK_RETRO_{name}", default)


ROM_DIR = Path(env("ROM_DIR", str(HOME / "games/roms")))
SAVE_DIR = Path(env("SAVE_DIR", str(HOME / "games/saves")))
PLAYLIST_DIR = Path(env("PLAYLIST_DIR", str(HOME / ".config/retroarch/playlists")))
# Multi-disc games, one .m3u each (games.disc_games).
M3U_DIR = Path(env("M3U_DIR", str(HOME / "games/m3u")))
CORE_DIR = Path(env("CORE_DIR", "/usr/lib/aarch64-linux-gnu/libretro"))
APPEND_CONFIG = env("CONFIG", str(Path(__file__).resolve().parents[2] / "brick9000/retroarch/retroarch.cfg"))
FLAG = Path(env("FLAG", os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "brick-retro.playing")))


def retroarch_running() -> bool:
    return subprocess.run(["pgrep", "-x", "retroarch"], stdout=subprocess.DEVNULL).returncode == 0


def start_retroarch() -> None:
    log.info("starting RetroArch")
    # Its own session: restarting brick-retro (a deploy) leaves a game alone.
    subprocess.Popen(["retroarch", "--appendconfig", APPEND_CONFIG], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_retroarch() -> None:
    log.info("no controller for a while: closing RetroArch (it saves a state first)")
    subprocess.run(["pkill", "-TERM", "-x", "retroarch"])


def force_quit_retroarch() -> None:
    log.info("Home held: closing RetroArch")
    subprocess.run(["pkill", "-TERM", "-x", "retroarch"])
    for _ in range(10):
        if not retroarch_running():
            return
        time.sleep(0.5)
    subprocess.run(["pkill", "-KILL", "-x", "retroarch"])


def trusted_pads() -> list[str]:
    out = subprocess.run(["bluetoothctl", "devices", "Trusted"], capture_output=True, text=True).stdout
    macs = [line.split()[1] for line in out.splitlines() if line.startswith("Device ")]
    pads = []
    for mac in macs:
        info = subprocess.run(["bluetoothctl", "info", mac], capture_output=True, text=True).stdout
        if "Icon: input-gaming" in info:
            pads.append(mac)
    return pads


def redial() -> None:
    """Ask the trusted pads to reconnect, in the background (a page takes seconds)."""
    def run():
        for mac in trusted_pads():
            subprocess.run(["bluetoothctl", "connect", mac], capture_output=True, timeout=15)
    threading.Thread(target=run, daemon=True).start()


# The Picade's speaker is very loud at full volume: never above MAX_VOLUME,
# in ten even notches up to it.
MAX_VOLUME = float(env("MAX_VOLUME", "0.5"))


def change_volume(step: int) -> None:
    # PipeWire's default sink (the Picade's DAC); WirePlumber remembers it.
    notch = f"{MAX_VOLUME / 10:.3f}{'+' if step > 0 else '-'}"
    subprocess.run(["wpctl", "set-volume", "-l", str(MAX_VOLUME), "@DEFAULT_AUDIO_SINK@", notch])


def set_flag(on: bool) -> None:
    if on:
        FLAG.touch()
    else:
        FLAG.unlink(missing_ok=True)


def daemon() -> None:
    import evdev  # apt: python3-evdev; imported here so tests don't need it

    def scan() -> dict[str, "evdev.InputDevice"]:
        found = {}
        for path in evdev.list_devices():
            try:
                d = evdev.InputDevice(path)
            except OSError:
                continue
            keys = set(d.capabilities().get(evdev.ecodes.EV_KEY, []))
            if is_pad(d.name, keys):
                found[path] = d
            else:
                d.close()
        return found

    pads = scan()
    launcher = Launcher(retroarch_running, start_retroarch, stop_retroarch, redial,
                        idle_exit_seconds=float(env("IDLE_EXIT_SECONDS", "900")), known=set(pads))
    volume = Volume(change_volume)
    log.info("brick-retro up: %d pad(s) connected", len(pads))
    while True:
        home = False
        readable, _, _ = select.select(list(pads.values()), [], [], 1.0)
        for d in readable:
            try:
                for e in d.read():
                    if e.type == evdev.ecodes.EV_KEY:
                        home |= volume.key(e.code, e.value, time.time())
                    elif e.type == evdev.ecodes.EV_ABS and e.code == ABS_HAT0Y:
                        volume.hat(e.value)
            except OSError:
                pass  # it disconnected; the rescan drops it
        current = scan()
        for path in set(pads) - set(current):
            log.info("controller gone: %s", pads[path].name)
        for path in set(current) - set(pads):
            log.info("controller connected: %s", current[path].name)
        for path, d in current.items():
            if path in pads:
                d.close()
                current[path] = pads[path]
        for path in set(pads) - set(current):
            pads[path].close()
        pads = current
        if volume.held(time.time()) and retroarch_running():
            force_quit_retroarch()
            launcher.known = set(pads)  # and don't reopen it for the same pad
        launcher.tick(set(pads), home)
        set_flag(retroarch_running())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if sys.argv[1:] == ["games"]:
        counts = games.write_playlists(ROM_DIR, PLAYLIST_DIR, CORE_DIR, M3U_DIR)
        copied = games.import_saves(ROM_DIR, SAVE_DIR)
        log.info("playlists: %s; imported %d old save(s)", counts, len(copied))
        return
    daemon()


if __name__ == "__main__":
    main()
