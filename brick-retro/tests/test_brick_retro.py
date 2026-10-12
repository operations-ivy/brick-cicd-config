import json
import tempfile
import unittest
from pathlib import Path

from brick_retro import games
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
                    "snes/Zelda.smc", "snes/Zelda.srm", "snes/Mario.sfc", "snes/nested/F-Zero.zip"):
            (roms / rel).parent.mkdir(parents=True, exist_ok=True)
            (roms / rel).write_text("x")
        self.roms = roms

    def test_playlists_list_each_systems_games_by_name(self):
        counts = games.write_playlists(self.roms, self.root / "playlists", Path("/cores"))
        self.assertEqual(counts, {"nes": 2, "snes": 3})
        nes = json.loads((self.root / "playlists/Nintendo - Nintendo Entertainment System.lpl").read_text())
        self.assertEqual([i["label"] for i in nes["items"]], ["Adventure Island", "Bomberman"])
        self.assertEqual(nes["default_core_path"], "/cores/nestopia_libretro.so")
        snes = json.loads((self.root / "playlists/Nintendo - Super Nintendo Entertainment System.lpl").read_text())
        self.assertEqual([i["label"] for i in snes["items"]], ["F-Zero", "Mario", "Zelda"])

    def test_missing_folders_are_empty_playlists(self):
        counts = games.write_playlists(self.root / "nothing", self.root / "playlists", Path("/cores"))
        self.assertEqual(counts, {"nes": 0, "snes": 0})

    def test_old_saves_are_imported_once_never_over_a_newer_one(self):
        saves = self.root / "saves"
        self.assertEqual(games.import_saves(self.roms, saves), ["Zelda.srm"])
        (saves / "Zelda.srm").write_text("played on brick9000")
        self.assertEqual(games.import_saves(self.roms, saves), [])
        self.assertEqual((saves / "Zelda.srm").read_text(), "played on brick9000")
