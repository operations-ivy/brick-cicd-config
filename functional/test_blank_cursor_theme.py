"""brick9000/blank-cursor-theme, run for real into a scratch icons folder:
every cursor file must parse as an Xcursor image whose pixels are all
transparent, and the theme must not inherit a visible one."""

import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from functional.helpers import ROOT

SCRIPT = ROOT / "brick9000" / "blank-cursor-theme"


def images(data: bytes) -> list[tuple[int, int, bytes]]:
    """The (width, height, ARGB pixels) of each image in an Xcursor file."""
    magic, header, _version, ntoc = struct.unpack_from("<4s3I", data)
    assert magic == b"Xcur", magic
    found = []
    for i in range(ntoc):
        chunk_type, _size, position = struct.unpack_from("<3I", data, header + 12 * i)
        if chunk_type == 0xFFFD0002:
            _hdr, _t, _s, _v, width, height, *_ = struct.unpack_from("<9I", data, position)
            start = position + 36
            assert len(data) >= start + 4 * width * height, "truncated image"
            found.append((width, height, data[start:start + 4 * width * height]))
    return found


class BlankCursorTheme(unittest.TestCase):
    def setUp(self):
        self.icons = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run([sys.executable, str(SCRIPT), str(self.icons)], check=True, capture_output=True)
        self.theme = self.icons / "brick-blank"

    def test_every_cursor_is_invisible(self):
        cursors = sorted((self.theme / "cursors").iterdir())
        self.assertLessEqual({"default", "left_ptr"}, {c.name for c in cursors})
        for cursor in cursors:
            found = images(cursor.read_bytes())
            self.assertTrue(found, cursor.name)
            for _width, _height, pixels in found:
                alphas = pixels[3::4]
                self.assertFalse(any(alphas), f"{cursor.name} has a visible pixel")

    def test_no_fallback_theme(self):
        index = (self.theme / "index.theme").read_text()
        self.assertIn("Name=brick-blank", index)
        self.assertNotIn("Inherits", index)

    def test_rerun_is_harmless(self):
        subprocess.run([sys.executable, str(SCRIPT), str(self.icons)], check=True, capture_output=True)
        self.assertTrue((self.theme / "cursors" / "default").is_file())


if __name__ == "__main__":
    unittest.main()
