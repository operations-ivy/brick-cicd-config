"""jenkins/gate/jenkins_gate.py: what Jenkins' key may run on a host, and
nothing else (#133)."""

import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from functional.helpers import ROOT, fake_commands, jenkins_gate as gate

GATE = ROOT / "jenkins/gate/jenkins_gate.py"


class Plan(unittest.TestCase):
    """Each allowed request, and what it runs."""

    def runs(self, request: str) -> list[list[str]]:
        with mock.patch.object(gate.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(gate.plan(request.split())(Path("/nonexistent")), 0)
        return [c.args[0] for c in run.call_args_list]

    def test_the_allowed_commands(self):
        cases = {
            "true": [["true"]],
            "is-active lcars-kiosk": [["systemctl", "is-active", "lcars-kiosk"]],
            "is-active --user brick-status": [["systemctl", "--user", "is-active", "brick-status"]],
            "board-state": [["curl", "-s", "--max-time", "5", "http://127.0.0.1:8765/api/state"]],
            "pause brick-roms.service brick-mirror.service": [
                ["systemctl", "--user", "stop", "brick-roms.service", "brick-mirror.service"],
                ["systemctl", "--user", "reset-failed"]],
            "poweroff": [["sudo", "-n", "systemd-run", "--on-active=5", "systemctl", "poweroff"]],
            "crictl-images": [["sudo", "-n", "k3s", "crictl", "images", "-o", "json"]],
            "crictl-rmi docker.io/whitepatrick/wigle-sync:0.1.2": [
                ["sudo", "-n", "k3s", "crictl", "rmi", "docker.io/whitepatrick/wigle-sync:0.1.2"]],
            "docker-images": [["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"]],
            "docker-rmi whitepatrick/overwatch:0.8.1": [["docker", "rmi", "whitepatrick/overwatch:0.8.1"]],
        }
        for request, expected in cases.items():
            with self.subTest(request):
                self.assertEqual(self.runs(request), expected)
                self.assertEqual(self.runs(f"jenkins-gate {request}"), expected)  # as the callers send it

    def test_anything_else_is_refused(self):
        for request in ("", "jenkins-gate", "bash", "sh -c id", "true; id", "cat /etc/shadow",
                        "is-active $(id)", "is-active", "is-active --user", "is-active a b",
                        "pause", "pause -x", "poweroff now", "crictl-rmi", "crictl-rmi a b",
                        "docker-rmi --force x", "deploy", "deploy evil main", "deploy brick-cicd-config",
                        "deploy brick-cicd-config -x", "busy", "busy x", "busy proc:", "busy unit:a;id",
                        "sudo cat /etc/rancher/k3s/k3s.yaml", "docker system dial-stdio",
                        "scp -t /tmp", "rsync --server -e.LsfxC . /"):
            with self.subTest(request):
                with self.assertRaises(gate.Refused):
                    gate.plan(request.split())

    def test_a_failing_step_stops_the_rest(self):
        with mock.patch.object(gate.subprocess, "run", return_value=mock.Mock(returncode=5)) as run:
            self.assertEqual(gate.plan(["pause", "a.service"])(Path("/")), 5)
        self.assertEqual(run.call_count, 1)  # no reset-failed after a failed stop


class Run(unittest.TestCase):
    """The script itself, as sshd runs it (SSH_ORIGINAL_COMMAND) or by hand."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = self.tmp / "log"
        self.path = fake_commands(self.tmp / "bin", {
            "systemctl": f'echo "systemctl $*" >>{self.log}\n'
                         'case "$*" in *"is-active --quiet busy.service"*) exit 0 ;; *is-active*) exit 3 ;; esac\n',
            "pgrep": 'test "$2" = apt\n',
        })

    def gate(self, *argv: str, ssh: str | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": self.path, "HOME": str(self.tmp)}
        env.pop("SSH_ORIGINAL_COMMAND", None)
        if ssh is not None:
            env["SSH_ORIGINAL_COMMAND"] = ssh
        return subprocess.run(["python3", str(GATE), *argv], capture_output=True, text=True, env=env)

    def test_sshd_forced_command_reads_the_original_command(self):
        result = self.gate("ignored", ssh="jenkins-gate is-active --user brick-status")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.log.read_text(), "systemctl --user is-active brick-status\n")

    def test_refused_is_126_and_runs_nothing(self):
        result = self.gate(ssh="sh -c 'id'")
        self.assertEqual(result.returncode, 126)
        self.assertIn("refused", result.stderr)
        self.assertFalse(self.log.exists())

    def test_an_interactive_login_is_refused(self):
        self.assertEqual(self.gate(ssh="").returncode, 126)

    def test_by_hand_without_ssh(self):
        self.assertEqual(self.gate("is-active", "lcars-kiosk").returncode, 3)

    def test_busy_prints_a_line_per_reason(self):
        result = self.gate(ssh="jenkins-gate busy proc:apt proc:dpkg unit:busy.service unit:idle.service")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), ["running: apt", "active: busy.service"])

    def test_deploy_sets_the_branch_in_place_and_starts_the_unit(self):
        env = self.tmp / ".config/brick-status/env"
        env.parent.mkdir(parents=True)
        env.write_text("A=1\nBRICK_DEPLOY_BRANCH=main\nB=2\n")
        self.assertEqual(self.gate(ssh="jenkins-gate deploy brick-cicd-config feature/x").returncode, 0)
        self.assertEqual(env.read_text(), "A=1\nBRICK_DEPLOY_BRANCH=feature/x\nB=2\n")
        self.assertEqual(self.log.read_text(), "systemctl --user start --no-block brick-deploy.service\n")

    def test_deploy_adds_a_missing_line_and_a_missing_file(self):
        self.assertEqual(self.gate(ssh="jenkins-gate deploy rpi_lcars master").returncode, 0)
        self.assertEqual((self.tmp / ".config/lcars/env").read_text(), "LCARS_DEPLOY_BRANCH=master\n")
        self.assertIn("lcars-deploy.service", self.log.read_text())


class SetupHost(unittest.TestCase):
    """jenkins/gate/setup-host's authorized_keys rewrite, against a scratch HOME
    (sudo is a fake that only logs)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / ".ssh").mkdir()
        self.keys = self.tmp / ".ssh/authorized_keys"
        self.keys.write_text("ssh-ed25519 AAAAmine me@laptop\nssh-ed25519 AAAAold jenkins@brick9000\n")
        self.path = fake_commands(self.tmp / "bin", {"sudo": f'echo "sudo $*" >>{self.tmp}/log\n'})

    def setup(self, blob: str, mode: str) -> subprocess.CompletedProcess:
        (self.tmp / "jenkins_gate.py").write_text("")
        env = {**os.environ, "PATH": self.path, "HOME": str(self.tmp), "JENKINS_GATE_SRC": str(self.tmp),
               "JENKINS_GATE_RUN": str(GATE)}
        env.pop("SSH_ORIGINAL_COMMAND", None)
        return subprocess.run(["sh", str(ROOT / "jenkins/gate/setup-host"), "ssh-ed25519", blob, "jenkins@brick", mode],
                              capture_output=True, text=True, env=env)

    def test_the_key_is_forced_to_the_gate_and_yours_is_untouched(self):
        result = self.setup("AAAAold", "keep")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.keys.read_text().splitlines(), [
            "ssh-ed25519 AAAAmine me@laptop",
            'command="/usr/local/bin/jenkins-gate",restrict ssh-ed25519 AAAAold jenkins@brick'])
        self.assertIn("sudo install -o root -g root -m 755", (self.tmp / "log").read_text())
        self.assertEqual(self.setup("AAAAold", "keep").returncode, 0)  # again: no duplicate
        self.assertEqual(len(self.keys.read_text().splitlines()), 2)

    def test_a_new_key_keeps_the_old_until_only(self):
        self.setup("AAAAnew", "keep")
        self.assertEqual(len(self.keys.read_text().splitlines()), 3)
        self.setup("AAAAnew", "only")
        self.assertEqual(self.keys.read_text().splitlines(), [
            "ssh-ed25519 AAAAmine me@laptop",
            'command="/usr/local/bin/jenkins-gate",restrict ssh-ed25519 AAAAnew jenkins@brick'])


class Callers(unittest.TestCase):
    """Every remote command the Jenkins scripts send is one the gate allows."""

    def test_power_down_busy_command(self):
        import functional.test_power_down as t
        for host in t.pd.HOSTS.values():
            with self.subTest(host=host):
                gate.plan(t.pd.busy_command(host).split())


if __name__ == "__main__":
    unittest.main()
