"""jenkins/bin/smoke: a host in a maintenance window is reported, not failed."""

import contextlib
import importlib.machinery
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

from functional.helpers import StubPrometheus, jenkins_gate

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True  # no __pycache__ in jenkins/bin: the image copies it
loader = importlib.machinery.SourceFileLoader("smoke", str(ROOT / "jenkins/bin/smoke"))
spec = importlib.util.spec_from_loader("smoke", loader)
smoke = importlib.util.module_from_spec(spec)
loader.exec_module(smoke)


def fake_ssh(host, command):
    """brick1982 is off (no answer); brick9000 is up. Only what jenkins-gate allows."""
    jenkins_gate.plan(command.split())  # raises Refused for anything else
    if host == "brick1982":
        return ""
    return "active" if "is-active" in command else "{}"


class Maintenance(unittest.TestCase):
    def run_smoke(self, windows):
        prom = StubPrometheus({"brick_maintenance_until_seconds": windows})
        self.addCleanup(prom.close)
        out = io.StringIO()
        with (
            mock.patch.multiple(smoke, PROMETHEUS=prom.url, ssh=fake_ssh, status=lambda url: 200),  # every name answers
            mock.patch.object(smoke.time, "time", return_value=0.0),
            contextlib.redirect_stdout(out),
        ):
            rc = smoke.main()
        return rc, out.getvalue()

    def test_a_down_host_fails(self):
        rc, said = self.run_smoke([])
        self.assertEqual(rc, 1)
        self.assertIn("DOWN brick1982 lcars-kiosk (unreachable)", said)

    def test_a_host_in_maintenance_is_reported_not_failed(self):
        rc, said = self.run_smoke([({"node": "brick1982"}, 1.0)])
        self.assertEqual(rc, 0)
        self.assertIn("mnt  brick1982 lcars-kiosk (unreachable)", said)
        self.assertIn("all up", said)

    def test_no_prometheus_excuses_nothing(self):
        err = io.StringIO()
        with mock.patch.object(smoke.urllib.request, "urlopen", side_effect=OSError("no route")), \
                contextlib.redirect_stderr(err):
            self.assertEqual(smoke.in_maintenance(), set())
        self.assertIn("no maintenance windows", err.getvalue())


if __name__ == "__main__":
    unittest.main()
