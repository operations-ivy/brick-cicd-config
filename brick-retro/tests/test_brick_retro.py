import json
import tempfile
import unittest
from pathlib import Path

from brick_retro import games, retro_metrics, thumbnails
from brick_retro.pads import BTN_DPAD_DOWN, BTN_DPAD_UP, BTN_GAMEPAD, BTN_JOYSTICK, BTN_MODE, Launcher, Volume, is_pad


class PadTests(unittest.TestCase):
    def test_gamepads_and_joysticks_count_the_cabinet_does_not(self):
        self.assertTrue(is_pad("8BitDo SN30 Pro", {BTN_GAMEPAD, BTN_MODE}))
        self.assertTrue(is_pad("Some DInput pad", {BTN_JOYSTICK}))
        self.assertFalse(is_pad("gpio_keys", {BTN_GAMEPAD}))
        self.assertFalse(is_pad("vc4-hdmi-0", {BTN_GAMEPAD}))
        self.assertFalse(is_pad("8BitDo SN30 Pro IMU", set()))


class FakeRetroArch:
    def __init__(self):
        self.up, self.starts, self.stops, self.redials = False, 0, 0, 0

    def launcher(self, **kw):
        def start():
            self.up, self.starts = True, self.starts + 1

        def stop():
            self.up, self.stops = False, self.stops + 1

        def redial():
            self.redials += 1
        return Launcher(lambda: self.up, start, stop, redial, idle_exit_seconds=900, redial_seconds=30,
                        settle_seconds=3, **kw)


class LauncherTests(unittest.TestCase):
    def test_a_pad_connecting_opens_retroarch(self):
        ra = FakeRetroArch()
        launcher = ra.launcher()
        launcher.tick(set(), now=0)
        self.assertEqual(ra.starts, 0)
        launcher.tick({"/dev/input/event5"}, now=1)
        self.assertEqual(ra.starts, 0)  # udev is still setting it up
        launcher.tick({"/dev/input/event5"}, now=4)
        self.assertEqual(ra.starts, 1)
        launcher.tick({"/dev/input/event5"}, now=5)
        self.assertEqual(ra.starts, 1)

    def test_a_pad_that_drops_while_settling_starts_nothing(self):
        ra = FakeRetroArch()
        launcher = ra.launcher()
        launcher.tick({"p"}, now=0)
        launcher.tick(set(), now=1)
        launcher.tick(set(), now=5)
        self.assertEqual(ra.starts, 0)

    def test_a_pad_there_at_startup_does_not_but_home_does(self):
        ra = FakeRetroArch()
        launcher = ra.launcher(known={"/dev/input/event5"})
        launcher.tick({"/dev/input/event5"}, now=0)
        self.assertEqual(ra.starts, 0)
        launcher.tick({"/dev/input/event5"}, home=True, now=1)
        self.assertEqual(ra.starts, 1)

    def test_quitting_with_the_pad_on_stays_quit(self):
        ra = FakeRetroArch()
        launcher = ra.launcher()
        launcher.tick({"p"}, now=0)
        launcher.tick({"p"}, now=3)
        ra.up = False  # quit from the menu
        launcher.tick({"p"}, now=4)
        launcher.tick({"p"}, now=10)
        self.assertEqual(ra.starts, 1)

    def test_a_drop_redials_and_a_reconnect_keeps_the_game(self):
        ra = FakeRetroArch()
        launcher = ra.launcher()
        launcher.tick({"p"}, now=0)
        launcher.tick({"p"}, now=3)
        launcher.tick(set(), now=100)
        self.assertEqual(ra.redials, 1)
        launcher.tick(set(), now=110)
        self.assertEqual(ra.redials, 1)  # not more than every 30 s
        launcher.tick(set(), now=131)
        self.assertEqual(ra.redials, 2)
        launcher.tick({"p2"}, now=140)  # back (a new event node), mid-game
        self.assertEqual((ra.starts, ra.stops, ra.up), (1, 0, True))

    def test_closes_after_fifteen_minutes_without_a_pad(self):
        ra = FakeRetroArch()
        launcher = ra.launcher()
        launcher.tick({"p"}, now=0)
        launcher.tick({"p"}, now=3)
        launcher.tick(set(), now=10)
        launcher.tick(set(), now=10 + 899)
        self.assertTrue(ra.up)
        launcher.tick(set(), now=10 + 900)
        self.assertEqual((ra.up, ra.stops), (False, 1))


class VolumeTests(unittest.TestCase):
    def setUp(self):
        self.steps = []
        self.volume = Volume(self.steps.append)

    def test_home_plus_dpad_steps_and_is_not_a_launch(self):
        self.assertFalse(self.volume.key(BTN_MODE, 1))
        self.volume.key(BTN_DPAD_UP, 1)
        self.volume.key(BTN_DPAD_UP, 0)
        self.volume.key(BTN_DPAD_DOWN, 1)
        self.volume.hat(-1)
        self.volume.hat(0)
        self.assertFalse(self.volume.key(BTN_MODE, 0))
        self.assertEqual(self.steps, [1, -1, 1])

    def test_holding_home_alone_is_a_close_once_not_a_launch(self):
        self.volume.key(BTN_MODE, 1, now=0)
        self.assertFalse(self.volume.held(2.9))
        self.assertTrue(self.volume.held(3.0))
        self.assertFalse(self.volume.held(4.0))
        self.assertFalse(self.volume.key(BTN_MODE, 0, now=4))

    def test_volume_presses_mean_holding_home_is_not_a_close(self):
        self.volume.key(BTN_MODE, 1, now=0)
        self.volume.key(BTN_DPAD_UP, 1, now=1)
        self.assertFalse(self.volume.held(5))

    def test_bare_home_is_a_launch_and_dpad_alone_does_nothing(self):
        self.volume.key(BTN_DPAD_UP, 1)
        self.volume.hat(1)
        self.volume.key(BTN_MODE, 1)
        self.assertTrue(self.volume.key(BTN_MODE, 0))
        self.assertEqual(self.steps, [])


class GamesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        roms = self.root / "roms"
        for rel in ("nes/B/Bomberman.nes", "nes/A/Adventure Island.NES", "nes/readme.txt",
                    "snes/Zelda.smc", "snes/Zelda.srm", "snes/Mario.sfc", "snes/nested/F-Zero.zip",
                    "gb/T/Tetris (World).zip", "gba/Metroid Fusion (USA).gba", "megadrive/S/Sonic.bin",
                    "psx/WipEout (USA).chd", "psx/wipeout_(v1.0).bin",
                    "psx/Final Fantasy VII (USA) (Disc 2).chd", "psx/Final Fantasy VII (USA) (Disc 1).chd",
                    "psx/Tekken 3 (Track 1).bin", "psx/Tekken 3 (Track 2).bin",
                    "psx/Ace Combat 3.bin", "psx/R4.ccd", "psx/R4.img", "psx/SaGa.ccd", "psx/SaGa.img.ecm",
                    "psx/Lost.cue", "psx/SCPH1001.BIN", "psx/GT2.ccd", "psx/GT2.img"):
            (roms / rel).parent.mkdir(parents=True, exist_ok=True)
            (roms / rel).write_text("x")
        (roms / "psx/Tekken 3.cue").write_text(
            'FILE "Tekken 3 (Track 1).bin" BINARY\n  TRACK 01 MODE2/2352\n'
            'FILE "Tekken 3 (Track 2).bin" BINARY\n  TRACK 02 AUDIO\n')
        (roms / "psx/Lost.cue").write_text('FILE "Lost.bin" BINARY\n')
        (roms / "psx/GT2.cue").write_text('FILE "GT2.img" BINARY\n')
        self.roms = roms

    def write(self):
        return games.write_playlists(self.roms, self.root / "playlists", Path("/cores"), self.root / "m3u")

    def items(self, name):
        return json.loads((self.root / "playlists" / f"{name}.lpl").read_text())["items"]

    def test_playlists_list_each_systems_games_by_name(self):
        counts = self.write()
        self.assertEqual(counts, {"nes": 2, "snes": 3, "gb": 1, "gbc": 0, "gba": 1, "megadrive": 1,
                                  "mastersystem": 0, "psx": 6})
        nes = json.loads((self.root / "playlists/Nintendo - Nintendo Entertainment System.lpl").read_text())
        self.assertEqual([i["label"] for i in nes["items"]], ["Adventure Island", "Bomberman"])
        self.assertEqual(nes["default_core_path"], "/cores/nestopia_libretro.so")
        snes = self.items("Nintendo - Super Nintendo Entertainment System")
        self.assertEqual([i["label"] for i in snes], ["F-Zero", "Mario", "Zelda"])
        self.assertEqual(self.items("Sega - Mega Drive - Genesis")[0]["core_path"],
                         "/cores/genesis_plus_gx_libretro.so")

    def test_ps1_lists_each_game_once_and_playable(self):
        self.write()
        psx = self.items("Sony - PlayStation")
        # The .bin copy of WipEout hides behind its .chd; Tekken 3's tracks are
        # its cue sheet's; SaGa is still .ecm-packed and Lost's track is gone;
        # GT2's .cue and .ccd are one image; SCPH1001 is a BIOS.
        self.assertEqual([i["label"] for i in psx],
                         ["Ace Combat 3", "Final Fantasy VII (USA)", "GT2", "R4", "Tekken 3", "WipEout (USA)"])
        self.assertEqual(Path(psx[2]["path"]).suffix, ".cue")
        self.assertEqual(psx[0]["core_path"], games.PCSX_REARMED)
        ff7 = Path(psx[1]["path"])
        self.assertEqual(ff7, self.root / "m3u/psx/Final Fantasy VII (USA).m3u")
        self.assertEqual(ff7.read_text().splitlines(),
                         [str(self.roms / "psx/Final Fantasy VII (USA) (Disc 1).chd"),
                          str(self.roms / "psx/Final Fantasy VII (USA) (Disc 2).chd")])

    def test_a_disc_set_that_goes_away_takes_its_m3u_with_it(self):
        self.write()
        (self.roms / "psx/Final Fantasy VII (USA) (Disc 2).chd").unlink()
        self.write()
        self.assertEqual(list((self.root / "m3u/psx").iterdir()), [])
        self.assertIn("Final Fantasy VII (USA) (Disc 1)", [i["label"] for i in self.items("Sony - PlayStation")])

    def test_systems_without_games_get_no_playlist(self):
        self.write()
        self.assertFalse((self.root / "playlists/Nintendo - Game Boy Color.lpl").exists())
        counts = games.write_playlists(self.root / "nothing", self.root / "playlists", Path("/cores"),
                                       self.root / "m3u")
        self.assertEqual(set(counts.values()), {0})
        self.assertEqual(list((self.root / "playlists").iterdir()), [])

    def test_old_saves_are_imported_once_never_over_a_newer_one(self):
        saves = self.root / "saves"
        self.assertEqual(games.import_saves(self.roms, saves), ["Zelda.srm"])
        (saves / "Zelda.srm").write_text("played on brick9000")
        self.assertEqual(games.import_saves(self.roms, saves), [])
        self.assertEqual((saves / "Zelda.srm").read_text(), "played on brick9000")


class LookTest(unittest.TestCase):
    """The CRT shader for every core, and PS1 at twice its resolution."""

    ROOT = Path(__file__).resolve().parents[2] / "brick9000" / "retroarch"

    def test_every_core_gets_the_crt_shader_unless_turned_off(self):
        from brick_retro import __main__ as main
        cmd = main.retroarch_command()
        self.assertEqual(cmd[:3], ["retroarch", "--appendconfig", main.APPEND_CONFIG])
        self.assertEqual(cmd[3], "--set-shader")
        preset = Path(cmd[4])
        self.assertTrue(preset.is_file(), preset)
        # The preset's shader path is relative to the preset.
        shader0 = [l for l in preset.read_text().splitlines() if l.startswith("shader0")][0]
        self.assertTrue((preset.parent / shader0.split('"')[1]).is_file())
        self.assertNotIn("--set-shader", main.retroarch_command(shader=""))

    def test_shaders_are_on_and_ps1_renders_at_twice_its_resolution(self):
        cfg = (self.ROOT / "retroarch.cfg").read_text()
        self.assertIn('video_shader_enable = "true"', cfg)
        self.assertIn('core_options_path = "~/.config/retroarch/brick-core-options.cfg"', cfg)
        self.assertIn('pcsx_rearmed_neon_enhancement_enable = "enabled"', (self.ROOT / "core-options.cfg").read_text())



class MetricsTests(unittest.TestCase):
    def test_written_only_when_something_changed(self):
        with tempfile.TemporaryDirectory() as d:
            path = f"{d}/brick-retro.prom"
            retro_metrics.PADS.set(1)
            retro_metrics.flush(path)
            self.assertIn("brick_retro_pads_connected 1", Path(path).read_text())
            Path(path).unlink()
            retro_metrics.flush(path)
            self.assertFalse(Path(path).exists())  # nothing changed, nothing written
            retro_metrics.PADS.set(2)
            retro_metrics.flush(path)
            self.assertIn("brick_retro_pads_connected 2", Path(path).read_text())

    def test_no_textfile_directory_no_error(self):
        retro_metrics.PADS.set(3)
        retro_metrics.flush("/nonexistent/brick-retro.prom")

    def test_games_per_system(self):
        text = retro_metrics.games_text({"nes": 12, "psx": 0})
        self.assertIn('brick_retro_games{system="nes"} 12', text)
        self.assertIn('brick_retro_games{system="psx"} 0', text)


class ThumbnailTests(unittest.TestCase):
    SERVER = ["7th Saga, The (USA)", "ActRaiser (Europe)", "ActRaiser (USA)", "ActRaiser (Japan) (Virtual Console)",
              "Mario _ Wario (Japan)", "Zelda (USA) (Beta)", "Zelda (Europe)",
              "Kirby _ The Amazing Mirror (USA)"]

    def by_key(self):
        out = {}
        for n in self.SERVER:
            out.setdefault(thumbnails.key(n), []).append(n)
        return out

    def test_goodtools_and_bare_names_match_no_intro(self):
        b = self.by_key()
        self.assertEqual(thumbnails.best("7th Saga, The (U) [!]", b), "7th Saga, The (USA)")
        self.assertEqual(thumbnails.best("ActRaiser", b), "ActRaiser (USA)")
        self.assertEqual(thumbnails.best("ActRaiser (E)", b), "ActRaiser (Europe)")
        self.assertEqual(thumbnails.best("Mario and Wario", b), "Mario _ Wario (Japan)")
        self.assertEqual(thumbnails.best("Kirby & The Amazing Mirror (USA)", b), "Kirby _ The Amazing Mirror (USA)")
        self.assertEqual(thumbnails.best("Zelda (U)", b), "Zelda (Europe)")  # a release over a beta
        self.assertIsNone(thumbnails.best("Not On The Server", b))

    def test_file_name_like_retroarch(self):
        self.assertEqual(thumbnails.file_name("Mario & Wario: Go?"), "Mario _ Wario_ Go_.png")

    def test_sync_fetches_missing_only_and_remembers_misses(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "pl").mkdir()
            items = [{"label": "ActRaiser"}, {"label": "ActRaiser"}, {"label": "Nope"}]
            (d / "pl" / "Nintendo - Super Nintendo Entertainment System.lpl").write_text(json.dumps({"items": items}))
            fetched, listed = [], []

            def index_fn(system, kind):
                listed.append(kind)
                return self.SERVER

            def fetch_fn(system, kind, name, target, limiter):
                fetched.append((kind, name))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"png")

            counts = thumbnails.sync(d / "pl", d / "th", d / "state.json", now=1000, index_fn=index_fn, fetch_fn=fetch_fn)
            self.assertEqual(sorted(fetched), sorted((k, "ActRaiser (USA)") for k in thumbnails.KINDS))
            snes = counts["Nintendo - Super Nintendo Entertainment System"]
            self.assertEqual((snes["games"], snes["Named_Boxarts"]), (2, 1))
            fetched.clear()
            listed.clear()
            thumbnails.sync(d / "pl", d / "th", d / "state.json", now=2000, index_fn=index_fn, fetch_fn=fetch_fn)
            self.assertEqual((fetched, listed), ([], []))  # nothing missing, the miss remembered: no server calls
            thumbnails.sync(d / "pl", d / "th", d / "state.json", now=1000 + 31 * 86400, index_fn=index_fn, fetch_fn=fetch_fn)
            self.assertEqual(len(listed), 3)  # the miss expired: asked again

    def test_offline_records_no_misses(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "pl").mkdir()
            (d / "pl" / "X.lpl").write_text(json.dumps({"items": [{"label": "A"}]}))

            def down(system, kind):
                raise OSError("offline")

            thumbnails.sync(d / "pl", d / "th", d / "state.json", index_fn=down)
            self.assertEqual(json.loads((d / "state.json").read_text()), {})

    def test_thumbnail_metrics(self):
        text = retro_metrics.thumbnails_text({"X": {"games": 3, "Named_Boxarts": 2}})
        self.assertIn('brick_retro_thumbnails{system="X",kind="Named_Boxarts"} 2', text)
        self.assertIn('brick_retro_playlist_games{system="X"} 3', text)

    def test_one_entry_per_name_across_folders(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "roms"
            for rel in ("snes/Mario.smc", "snes/M/Mario.smc", "snes/Zelda.sfc"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_bytes(b"x")
            snes = next(s for s in games.SYSTEMS if s.folder == "snes")
            got = games.games(snes, root, Path(d) / "m3u")
        self.assertEqual([label for _, label in got], ["Mario", "Zelda"])
        self.assertEqual(got[0][0], root / "snes/Mario.smc")
