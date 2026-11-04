"""jenkins/bin/hub-image-info, with Docker Hub stubbed: a brand-new image's
repository doesn't exist yet, and the registry says 401 for it, not 404."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True  # no __pycache__ in jenkins/bin: the image copies it
loader = importlib.machinery.SourceFileLoader("hub_image_info", str(ROOT / "jenkins/bin/hub-image-info"))
spec = importlib.util.spec_from_loader("hub_image_info", loader)
hii = importlib.util.module_from_spec(spec)
loader.exec_module(hii)


def fake_get(routes):
    """get() answering from {url prefix: (headers, body) or HTTP error code}."""
    def get(url, headers=None):
        for prefix, answer in routes.items():
            if url.startswith(prefix):
                if isinstance(answer, int):
                    raise urllib.error.HTTPError(url, answer, "stub", {}, None)
                return answer
        raise AssertionError("unexpected request: " + url)
    return get


TOKEN = {"https://auth.docker.io/token": ({}, {"token": "t"})}
MANIFESTS = "https://registry-1.docker.io/v2/whitepatrick/new/manifests/"
HUB_REPO = "https://hub.docker.com/v2/repositories/whitepatrick/new/"


def run(routes, *args):
    out = io.StringIO()
    with (mock.patch.object(hii, "get", side_effect=fake_get({**TOKEN, **routes})),
          mock.patch.object(sys, "argv", ["hub-image-info", *args]),
          contextlib.redirect_stdout(out)):
        code = hii.main()
    return code, out.getvalue()


class HubImageInfo(unittest.TestCase):
    def test_a_repository_that_doesnt_exist_yet_is_missing(self):
        code, out = run({MANIFESTS: 401, HUB_REPO: 404}, "whitepatrick/new", "0.2.0")
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out), {"image": "whitepatrick/new", "tag": "0.2.0", "exists": False})

    def test_a_401_on_a_repository_that_exists_is_still_an_error(self):
        with self.assertRaises(urllib.error.HTTPError):
            run({MANIFESTS: 401, HUB_REPO: ({}, {"name": "new"})}, "whitepatrick/new", "0.2.0")

    def test_a_missing_tag_is_missing(self):
        self.assertEqual(run({MANIFESTS: 404}, "whitepatrick/new", "0.2.0")[0], 3)

    def test_no_repository_means_no_newest_tag(self):
        code, out = run({HUB_REPO + "tags": 404}, "whitepatrick/new")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"], "no tags")


if __name__ == "__main__":
    unittest.main()
