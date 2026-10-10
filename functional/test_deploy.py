"""brick9000/deploy and brick1982/deploy, run for real against throwaway git
repos: a bare "origin" standing in for GitHub and a clone standing in for the
device, with systemctl (and brick9000's proxy/up) replaced by recorders.

Each commit carries a RESULT file; its scripts/test passes only when RESULT
says "pass", so the tests can make a branch good or bad."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from functional.helpers import ROOT, fake_commands

TEST_SCRIPT = '#!/bin/sh\ngrep -qx pass "$(dirname "$0")/../RESULT"\n'


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class DeployHarness:
    def __init__(self, host: str):
        self.host = host
        self.tmp = Path(tempfile.mkdtemp())
        self.origin, self.work, self.device = self.tmp / "origin.git", self.tmp / "work", self.tmp / "device"
        self.home, self.log = self.tmp / "home", self.tmp / "commands.log"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.work)], check=True, capture_output=True)
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            git(self.work, "config", k, v)
        # The real deploy script (and what it sources), unchanged.
        for rel in (f"{host}/deploy", "brick9000/build-status.sh"):
            dest = self.work / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, dest)
        (self.work / "scripts").mkdir()
        (self.work / "scripts/test").write_text(TEST_SCRIPT)
        (self.work / "scripts/test").chmod(0o755)
        self.commit("pass", "first")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.device)], check=True, capture_output=True)
        env_dir = self.home / ".config" / ("brick-status" if host == "brick9000" else "brick-arena")
        env_dir.mkdir(parents=True)
        self.env_file = env_dir / "env"
        self.follow("main")
        self.path = fake_commands(self.tmp / "bin", {"systemctl": f'echo "systemctl $*" >> {self.log}\n'})

    def commit(self, result: str, message: str, branch: str = "main", touch: str | None = None) -> str:
        if branch != "main":
            git(self.work, "checkout", "-q", "-B", branch)
        (self.work / "RESULT").write_text(result + "\n")
        # Something new in every commit, even when RESULT doesn't change.
        self.count = getattr(self, "count", 0) + 1
        (self.work / "COMMIT").write_text(f"{self.count} {message}\n")
        if touch:
            p = self.work / touch
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(message)
            if touch.endswith("/up"):
                p.write_text(f'#!/bin/sh\necho "proxy up" >> {self.log}\n')
                p.chmod(0o755)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", message)
        git(self.work, "push", "-q", "origin", f"HEAD:{branch}")
        sha = git(self.work, "rev-parse", "HEAD")
        if branch != "main":
            git(self.work, "checkout", "-q", "main")
        return sha

    def follow(self, branch: str):
        self.env_file.write_text(f"BRICK_DEPLOY_BRANCH={branch}\n")

    def deploy(self) -> subprocess.CompletedProcess:
        env = {**os.environ, "HOME": str(self.home), "PATH": self.path, "XDG_STATE_HOME": str(self.home / "state")}
        env.pop("XDG_CONFIG_HOME", None)
        return subprocess.run([str(self.device / self.host / "deploy")], capture_output=True, text=True, env=env)

    @property
    def head(self) -> str:
        return git(self.device, "rev-parse", "HEAD")

    @property
    def commands(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class DeployScripts:
    host = ""
    service = ""

    def setUp(self):
        self.h = DeployHarness(self.host)

    def tearDown(self):
        self.h.cleanup()

    def test_nothing_new_is_a_quiet_no_op(self):
        before = self.h.head
        result = self.h.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.h.head, before)
        self.assertEqual(self.h.commands, [])

    def test_a_passing_commit_is_deployed_and_the_service_restarted(self):
        sha = self.h.commit("pass", "second")
        result = self.h.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.h.head, sha)
        self.assertIn(f"systemctl --user restart {self.service}", self.h.commands)

    def test_a_failing_commit_is_refused_and_not_retried(self):
        good = self.h.commit("pass", "good")
        self.h.deploy()
        bad = self.h.commit("fail", "bad")
        result = self.h.deploy()
        self.assertEqual(result.returncode, 1)
        self.assertIn("tests failed", result.stderr)
        self.assertEqual(self.h.head, good)
        failed = (self.h.home / "state" / "brick-deploy" / "failed").read_text().strip()
        self.assertEqual(failed, bad)
        # The same bad commit again: nothing to do, quietly.
        again = self.h.deploy()
        self.assertEqual((again.returncode, again.stderr), (0, ""))
        # A fix on top deploys and clears the memory of the failure.
        fixed = self.h.commit("pass", "fixed")
        self.assertEqual(self.h.deploy().returncode, 0)
        self.assertEqual(self.h.head, fixed)
        self.assertFalse((self.h.home / "state" / "brick-deploy" / "failed").exists())

    def test_it_follows_a_branch_and_comes_back_to_main(self):
        feature = self.h.commit("pass", "try me", branch="feature")
        self.h.follow("feature")
        self.assertEqual(self.h.deploy().returncode, 0)
        self.assertEqual(self.h.head, feature)
        main = self.h.commit("pass", "merged elsewhere")
        self.h.follow("main")
        self.assertEqual(self.h.deploy().returncode, 0)
        self.assertEqual(self.h.head, main)

    def test_a_missing_branch_stays_put(self):
        before = self.h.head
        self.h.follow("no-such-branch")
        result = self.h.deploy()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no branch 'no-such-branch'", result.stderr)
        self.assertEqual(self.h.head, before)


class Brick1982Deploy(DeployScripts, unittest.TestCase):
    host, service = "brick1982", "brick-arena"

    def test_a_change_under_brick1982_says_to_rerun_install(self):
        self.h.commit("pass", "unit change", touch="brick1982/brick-kiosk.service")
        result = self.h.deploy()
        self.assertIn("run brick1982/install.sh", result.stderr)


class Brick9000Deploy(DeployScripts, unittest.TestCase):
    host, service = "brick9000", "brick-status"

    def test_the_lights_see_the_deploy_and_its_result(self):
        sha = self.h.commit("pass", "second")
        self.h.deploy()
        status = json.loads((self.h.home / "state" / "brick-build" / "deploy.json").read_text())
        self.assertEqual((status["state"], status["image"]), ("succeeded", "brick9000"))
        self.assertEqual(status["ref"], sha[:7])
        self.assertIn("finished", status)

    def test_a_failed_deploy_shows_as_failed(self):
        self.h.commit("fail", "bad")
        self.h.deploy()
        status = json.loads((self.h.home / "state" / "brick-build" / "deploy.json").read_text())
        self.assertEqual(status["state"], "failed")

    def test_a_proxy_change_runs_proxy_up(self):
        self.h.commit("pass", "proxy", touch="brick9000/proxy/up")
        self.assertEqual(self.h.deploy().returncode, 0)
        self.assertIn("proxy up", self.h.commands)


if __name__ == "__main__":
    unittest.main()
