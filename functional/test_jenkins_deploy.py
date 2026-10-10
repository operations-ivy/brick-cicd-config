"""jenkins/bin/lib.sh's start_deploy, run for real: ssh is replaced by a
command that runs the remote side locally (against a scratch HOME), and
git ls-remote by one that knows a fixed set of branches."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from functional.helpers import ROOT, fake_commands


class StartDeploy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home, self.log = self.tmp / "home", self.tmp / "commands.log"
        (self.home / ".config/brick-status").mkdir(parents=True)
        self.env_file = self.home / ".config/brick-status/env"
        self.env_file.write_text("BRICK_STATUS_X=1\nBRICK_DEPLOY_BRANCH=main\nBRICK_STATUS_Y=2\n")
        self.path = fake_commands(self.tmp / "bin", {
            # ssh <host> sh -s -- <env file> <branch>: run it here, as the host would.
            "ssh": f'echo "ssh $1" >> {self.log}; shift; HOME={self.home} exec "$@"\n',
            "systemctl": f'echo "systemctl $*" >> {self.log}\n',
            "git": ('if [ "$1" = ls-remote ]; then\n'
                    '  case "$5" in main|feature/radar) exit 0 ;; *) exit 2 ;; esac\n'
                    'fi\nexec /usr/bin/git "$@"\n'),
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, branch: str, env_file: Path | None = None) -> subprocess.CompletedProcess:
        rel = (env_file or self.env_file).relative_to(self.home)
        script = f'. {ROOT}/jenkins/bin/lib.sh; start_deploy brick9000 {rel} "$1"'
        return subprocess.run(["sh", "-c", script, "sh", branch], capture_output=True, text=True,
                              env={**os.environ, "PATH": self.path, "JENKINS_HOME": ""})

    def lines(self):
        return self.env_file.read_text().splitlines()

    def test_a_branch_is_set_and_the_deploy_started(self):
        result = self.start("feature/radar")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines(), ["BRICK_STATUS_X=1", "BRICK_DEPLOY_BRANCH=feature/radar", "BRICK_STATUS_Y=2"])
        self.assertIn("systemctl --user start --no-block brick-deploy.service", self.log.read_text())

    def test_empty_means_main(self):
        self.start("feature/radar")
        self.assertEqual(self.start("").returncode, 0)
        self.assertIn("BRICK_DEPLOY_BRANCH=main", self.lines())

    def test_a_missing_line_is_added(self):
        self.env_file.write_text("BRICK_STATUS_X=1\n")
        self.assertEqual(self.start("main").returncode, 0)
        self.assertEqual(self.lines(), ["BRICK_STATUS_X=1", "BRICK_DEPLOY_BRANCH=main"])

    def test_bad_names_and_unknown_branches_never_reach_the_host(self):
        for branch in ("x;rm -rf /", "-foo", "$(id)", "no-such-branch"):
            result = self.start(branch)
            self.assertNotEqual(result.returncode, 0, branch)
        self.assertFalse(self.log.exists())  # no ssh, no systemctl
        self.assertIn("BRICK_DEPLOY_BRANCH=main", self.lines())


if __name__ == "__main__":
    unittest.main()
