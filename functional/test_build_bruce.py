"""jenkins/bin/build-bruce with git, pio and GitHub stubbed: the parameters a
webhook can set are checked, the extra flags become a PlatformIO environment
that extends the board's, and the outputs are named and summed."""

import hashlib
import importlib.machinery
import importlib.util
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True  # no __pycache__ in jenkins/bin: the image copies it
loader = importlib.machinery.SourceFileLoader("build_bruce", str(ROOT / "jenkins/bin/build-bruce"))
spec = importlib.util.spec_from_loader("build_bruce", loader)
bb = importlib.util.module_from_spec(spec)
loader.exec_module(bb)


def partition_table(*entries: tuple[int, int, int]) -> bytes:
    """(type, subtype, offset) entries as an ESP32 partition table."""
    rows = b"".join(b"\xaa\x50" + struct.pack("<BBII", t, s, o, 0x1000) + b"\0" * 20 for t, s, o in entries)
    return rows + b"\xff" * 32


class FakeBruce:
    """Stands in for git and pio: clone makes a checkout, run builds it."""

    def __init__(self, boards=("CYD-2432S028", "CYD-2USB")):
        self.boards, self.calls, self.src = boards, [], None

    def run(self, *cmd, cwd=None):
        self.calls.append(cmd)
        match cmd:
            case ("git", "clone", *_, dest):
                self.src = Path(dest)
                self.src.mkdir(parents=True)
                (self.src / "platformio.ini").write_text("[platformio]\n")
                return ""
            case ("git", "rev-parse", "HEAD"):
                return "abc123\n"
            case ("pio", "project", "config", "--json-output"):
                ini = (self.src / "platformio.ini").read_text()
                local = [line[5:-1] for line in ini.splitlines() if line.startswith("[env:")]
                return str([[f"env:{b}", []] for b in [*self.boards, *local]]).replace("'", '"')
            case ("pio", "run", "--environment", env):
                built = self.src / ".pio" / "build" / env
                built.mkdir(parents=True)
                (built / "firmware.bin").write_bytes(b"app")
                (built / "partitions.bin").write_bytes(partition_table((1, 2, 0x9000), (0, 0, 0x10000)))
                (self.src / f"Bruce-{env}.bin").write_bytes(b"full")
                return ""
            case ("pio", "--version"):
                return "PlatformIO Core, version 6.2.0\n"
        raise AssertionError(f"unexpected command: {cmd}")


def build(env: dict, fake: FakeBruce | None = None) -> tuple[FakeBruce, Path]:
    fake = fake or FakeBruce()
    tmp = Path(tempfile.mkdtemp())
    with (mock.patch.object(bb, "run", side_effect=fake.run),
          mock.patch.object(bb, "latest_version", return_value="1.18")):
        bb.build(bb.settings(env), tmp / "work", tmp / "out")
    return fake, tmp / "out"


class Settings(unittest.TestCase):
    def test_defaults(self):
        s = bb.settings({})
        self.assertEqual((s["version"], s["board"], s["flags"], s["inversion_off"]), ("", "CYD-2432S028", [], False))

    def test_inversion_off_adds_the_flag_once(self):
        self.assertEqual(bb.settings({"INVERSION_OFF": "true"})["flags"], ["-DTFT_INVERSION_ON"])
        s = bb.settings({"INVERSION_OFF": "true", "EXTRA_FLAGS": "-DTFT_INVERSION_ON -DX=1"})
        self.assertEqual(s["flags"], ["-DTFT_INVERSION_ON", "-DX=1"])

    def test_anything_that_isnt_a_plain_value_is_refused(self):
        for env in ({"VERSION": "1.17; rm -rf /"}, {"VERSION": "../x"}, {"BOARD": "CYD $(id)"},
                    {"BOARD": "a\n[env:x]"}, {"EXTRA_FLAGS": "-DX=$(id)"}, {"EXTRA_FLAGS": "--upload-port=x"},
                    {"EXTRA_FLAGS": "-DX='a b'"}):
            with self.subTest(env=env), self.assertRaises(bb.BuildError):
                bb.settings(env)


class Build(unittest.TestCase):
    def test_inversion_off_builds_a_local_environment_extending_the_board(self):
        fake, out = build({"VERSION": "1.17", "INVERSION_OFF": "true"})
        ini = (fake.src / "platformio.ini").read_text()
        self.assertIn("[env:CYD-2432S028-local]\nextends = env:CYD-2432S028\n", ini)
        self.assertIn("\t${env:CYD-2432S028.build_flags}\n\t-DTFT_INVERSION_ON\n", ini)
        self.assertIn(("pio", "run", "--environment", "CYD-2432S028-local"), fake.calls)
        clone = next(c for c in fake.calls if c[:2] == ("git", "clone"))
        self.assertEqual(clone[clone.index("--branch") + 1], "1.17")

        full, app = out / "Bruce-1.17-CYD-2432S028-invoff.bin", out / "Bruce-1.17-CYD-2432S028-invoff.app.bin"
        self.assertEqual((full.read_bytes(), app.read_bytes()), (b"full", b"app"))
        sums = (out / "SHA256SUMS").read_text()
        self.assertIn(f"{hashlib.sha256(b'full').hexdigest()}  {full.name}\n", sums)
        self.assertIn(f"{hashlib.sha256(b'app').hexdigest()}  {app.name}\n", sums)
        info = (out / "BUILD-INFO.txt").read_text()
        self.assertIn("commit/abc123", info)
        self.assertIn(f"write-flash 0x10000 {app.name}", info)
        self.assertIn(f"write-flash 0x0 {full.name}", info)

    def test_no_flags_builds_the_board_as_released(self):
        fake, out = build({"VERSION": "1.17", "BOARD": "CYD-2USB"})
        self.assertNotIn("[env:", (fake.src / "platformio.ini").read_text())
        self.assertIn(("pio", "run", "--environment", "CYD-2USB"), fake.calls)
        self.assertTrue((out / "Bruce-1.17-CYD-2USB.bin").is_file())

    def test_extra_flags_are_marked_in_the_name(self):
        _, out = build({"VERSION": "1.17", "INVERSION_OFF": "true", "EXTRA_FLAGS": "-DFOO=1"})
        self.assertTrue((out / "Bruce-1.17-CYD-2432S028-invoff-flags.bin").is_file())

    def test_empty_version_builds_the_latest_release(self):
        fake, out = build({})
        clone = next(c for c in fake.calls if c[:2] == ("git", "clone"))
        self.assertEqual(clone[clone.index("--branch") + 1], "1.18")
        self.assertTrue((out / "Bruce-1.18-CYD-2432S028.bin").is_file())

    def test_a_board_bruce_doesnt_have_stops_before_building(self):
        fake = FakeBruce()
        with self.assertRaisesRegex(bb.BuildError, "no board"):
            build({"VERSION": "1.17", "BOARD": "CYD-9999"}, fake)
        self.assertFalse(any(c[:2] == ("pio", "run") for c in fake.calls))


class AppOffset(unittest.TestCase):
    def test_the_first_app_partition(self):
        self.assertEqual(bb.app_offset(partition_table((1, 2, 0x9000), (0, 0, 0x10000), (1, 0x82, 0x3d0000))), 0x10000)

    def test_no_app_partition(self):
        with self.assertRaises(bb.BuildError):
            bb.app_offset(partition_table((1, 2, 0x9000)))


if __name__ == "__main__":
    unittest.main()
