"""jenkins/bin/find-builds, with GitHub and Docker Hub stubbed."""

import contextlib
import importlib.machinery
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True  # no __pycache__ in jenkins/bin: the image copies it
loader = importlib.machinery.SourceFileLoader("find_builds", str(ROOT / "jenkins/bin/find-builds"))
spec = importlib.util.spec_from_loader("find_builds", loader)
fb = importlib.util.module_from_spec(spec)
loader.exec_module(fb)

SYNC, CONSOLE = "whitepatrick/wigle-sync", "whitepatrick/wigle-console"
ARGS = [f"{SYNC}=wigle-sync", f"{CONSOLE}=wigle-sync"]
A, B = "a" * 40, "b" * 40
MAIN = {SYNC: "0.1.11", CONSOLE: "0.1.18"}


class FindBuilds(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.state = f"{d.name}/seen"

    def poll(self, heads, pins=None, hub=None):
        pins = {"main": MAIN, **(pins or {})}
        hub = hub or {}
        calls = []

        def on_hub(image, tag):
            calls.append((image, tag))
            return hub.get((image, tag))

        out, err = io.StringIO(), io.StringIO()
        with (mock.patch.object(fb, "heads", return_value=heads),
              mock.patch.object(fb, "pinned", side_effect=lambda _, ref: pins[ref]),
              mock.patch.object(fb, "on_hub", side_effect=on_hub),
              mock.patch.object(sys, "argv", ["find-builds", self.state, *ARGS]),
              contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
            code = fb.main()
        return code, out.getvalue().splitlines(), err.getvalue(), calls

    def test_a_feature_branch_builds_each_image_as_dev(self):
        code, lines, _, _ = self.poll({"feature/x": A})
        self.assertEqual(code, 0)
        self.assertEqual(sorted(lines), sorted([f"{SYNC} dev-{A[:12]} {A}", f"{CONSOLE} dev-{A[:12]} {A}"]))

    def test_a_release_builds_only_what_it_bumped(self):
        pins = {A: {**MAIN, CONSOLE: "0.1.19"}}
        code, lines, err, _ = self.poll({"release/x": A}, pins)
        self.assertEqual((code, lines, err), (0, [f"{CONSOLE} 0.1.19 {A}"], ""))

    def test_a_failed_build_waits_for_a_new_commit(self):
        self.assertTrue(self.poll({"feature/x": A})[1])
        code, lines, _, calls = self.poll({"feature/x": A})
        self.assertEqual((code, lines, calls), (0, [], []))
        self.assertEqual(len(self.poll({"feature/x": B})[1]), 2)

    def test_a_quiet_repo_never_asks_docker_hub(self):
        self.assertEqual(self.poll({})[3], [])

    def test_a_tag_built_from_another_commit_is_a_clash(self):
        pins = {A: {**MAIN, CONSOLE: "0.1.19"}}
        code, lines, err, _ = self.poll({"release/x": A}, pins, {(CONSOLE, "0.1.19"): B})
        self.assertEqual((code, lines), (4, []))
        self.assertIn("bump the tag", err)

    def test_a_blip_builds_nothing_and_forgets_nothing(self):
        self.poll({"feature/x": A})
        with (mock.patch.object(fb, "heads", side_effect=OSError("no route")),
              mock.patch.object(sys, "argv", ["find-builds", self.state, *ARGS]),
              contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(fb.main(), 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(self.poll({"feature/x": A})[1], [])

    def test_a_kustomization_is_read_by_image(self):
        text = "images:\n  - name: whitepatrick/wigle-sync\n    newTag: 0.1.12\n  - name: whitepatrick/overwatch\n    newTag: 0.8.5\n"
        self.assertEqual(dict(fb.TAG.findall(text)), {SYNC: "0.1.12", "whitepatrick/overwatch": "0.8.5"})
