"""jenkins/bin/power-down against fake ssh, kubectl, maintenance and smoke that
record every call, so each test can check what would have happened, in order."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from functional.helpers import ROOT, fake_commands

sys.dont_write_bytecode = True  # no __pycache__ in jenkins/bin: the image copies it
loader = importlib.machinery.SourceFileLoader("power_down", str(ROOT / "jenkins/bin/power-down"))
spec = importlib.util.spec_from_loader("power_down", loader)
pd = importlib.util.module_from_spec(spec)
loader.exec_module(pd)

# Each fake logs "<name> <args>" and reads its answers from files in $STATE.
FAKES = {
    "ssh": """
printf 'ssh %s\n' "$(echo "$*" | tr '\n' ' ')" >>"$STATE/log"
for last; do :; done
case "$last" in
  "jenkins-gate poweroff") touch "$STATE/off" ;;
  "jenkins-gate true") [ -e "$STATE/off" ] && exit 255 ;;
  "jenkins-gate busy "*) cat "$STATE/busy" 2>/dev/null ;;
esac
exit 0
""",
    "kubectl": """
echo "kubectl $*" >>"$STATE/log"
case "$*" in
  "get pods -n jenkins-agents"*) cat "$STATE/agents" 2>/dev/null || echo '{"items": []}' ;;
  *spec.nodeName=*)  # the node's pods: before.json until the drain, node.json after
    f=before.json; [ -e "$STATE/drained" ] && f=node.json
    cat "$STATE/$f" 2>/dev/null || echo '{"items": []}' ;;
  "get deployments"*) cat "$STATE/deployments.json" 2>/dev/null || echo '{"items": []}' ;;
  *status.phase=Pending*) cat "$STATE/pending.json" 2>/dev/null || echo '{"items": []}' ;;
  drain*) touch "$STATE/drained"; exit "$(cat "$STATE/drain_rc" 2>/dev/null || echo 0)" ;;
  "rollout status"*deployment/slow*) exit 1 ;;
esac
exit 0
""",
    "maintenance": 'echo "maintenance NODE=$NODE DURATION=$DURATION END=$END" >>"$STATE/log"\n',
    "smoke": 'echo "smoke" >>"$STATE/log"\n',
}


def pod(namespace: str, name: str, owner: str = "ReplicaSet", deployment: str = "") -> dict:
    """A pod; with deployment, one of that Deployment's (owned by its ReplicaSet)."""
    if deployment:
        return {"metadata": {"namespace": namespace, "name": f"{deployment}-5f7d9-x2c4q",
                             "labels": {"pod-template-hash": "5f7d9"},
                             "ownerReferences": [{"kind": "ReplicaSet", "name": f"{deployment}-5f7d9"}]},
                "status": {"phase": "Running"}}
    return {"metadata": {"namespace": namespace, "name": name, "ownerReferences": [{"kind": owner}]},
            "status": {"phase": "Running"}}


def deployment(namespace: str, name: str, replicas: int = 1, strategy: str = "RollingUpdate") -> dict:
    return {"metadata": {"namespace": namespace, "name": name},
            "spec": {"replicas": replicas, "strategy": {"type": strategy}}}


class PowerDown(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = fake_commands(self.state / "bin", FAKES)
        self.enterContext(mock.patch.dict(os.environ, {"PATH": path, "STATE": str(self.state)}))
        self.enterContext(mock.patch.object(pd, "MAINTENANCE", self.state / "bin/maintenance"))
        self.enterContext(mock.patch.object(pd, "SMOKE", self.state / "bin/smoke"))
        self.enterContext(mock.patch.object(pd, "POLL", 0))
        self.enterContext(mock.patch.object(pd, "DOWN_WAIT", 5))

    def go(self, **params: str) -> tuple[str | None, str]:
        """Run main() with the job's parameters; (the SystemExit message or None, stdout)."""
        env = {"ACTION": "down", "DURATION": "4h", "SHUTDOWN": "true", "DRY_RUN": "false", **params}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            try:
                pd.main()
            except SystemExit as e:
                return str(e), out.getvalue()
        return None, out.getvalue()

    def log(self) -> list[str]:
        path = self.state / "log"
        return [line.rstrip() for line in path.read_text().splitlines()] if path.exists() else []

    def changes(self) -> list[str]:
        """The calls that change something (not the reads and SSH probes)."""
        reads = ("kubectl get", "kubectl wait", "kubectl rollout status")
        return [line for line in self.log() if not line.startswith(reads)
                and not line.endswith(" true") and "jenkins-gate busy" not in line]

    def test_brick2000_is_refused_with_the_manual_steps(self):
        error, _ = self.go(NODE="brick2000")
        self.assertIn("refused", error)
        self.assertIn("kubectl drain brick2000", error)
        self.assertIn("uncordon brick2000", error)
        self.assertEqual(self.log(), [])

    def test_brick420_and_unknown_nodes_are_refused(self):
        self.assertIn("control plane", self.go(NODE="brick420")[0])
        self.assertIn("NODE must be one of", self.go(NODE="brick69")[0])
        self.assertEqual(self.log(), [])

    def test_busy_host_is_refused_before_anything_changes(self):
        (self.state / "busy").write_text("running: retroarch\n")
        error, _ = self.go(NODE="brick9000")
        self.assertIn("busy", error)
        self.assertIn("running: retroarch", error)
        self.assertEqual(self.changes(), [])

    def test_a_jenkins_build_on_the_node_is_busy(self):
        (self.state / "agents").write_text(json.dumps({"items": [pod("jenkins-agents", "test-wigle-sync-abc")]}))
        error, _ = self.go(NODE="brick666")
        self.assertIn("Jenkins build: jenkins-agents/test-wigle-sync-abc", error)
        self.assertEqual(self.changes(), [])

    def test_dry_run_checks_and_prints_but_changes_nothing(self):
        error, out = self.go(NODE="brick666", DRY_RUN="true")
        self.assertIsNone(error)
        self.assertIn("DRY RUN", out)
        self.assertIn("would 2. cordon and drain brick666", out)
        self.assertIn("would 3. power brick666 off", out)
        self.assertEqual(self.changes(), [])
        self.assertTrue(any("jenkins-gate busy" in line for line in self.log()), "the busy check still runs")

    def test_k3s_node_down_in_order(self):
        (self.state / "pending.json").write_text(json.dumps({"items": [pod("overwatch", "scanner-0")]}))
        error, out = self.go(NODE="brick666")
        self.assertIsNone(error)
        changes = self.changes()
        self.assertEqual(changes[0], "maintenance NODE=brick666 DURATION=4h END=false")
        self.assertEqual(changes[1], "kubectl cordon brick666")
        self.assertTrue(changes[2].startswith("kubectl drain brick666 --ignore-daemonsets"))
        self.assertIn("poweroff", changes[3])
        self.assertEqual(len(changes), 4)
        self.assertIn("brick666 is drained", out)
        self.assertIn("overwatch/scanner-0", out)
        self.assertIn("off the network", out)

    def test_single_copies_move_before_the_drain(self):
        (self.state / "before.json").write_text(json.dumps({"items": [
            pod("kube-system", "", deployment="coredns"), pod("argocd", "", deployment="argocd-repo-server"),
            pod("wigle", "", deployment="wigle-console"), pod("overwatch", "", deployment="overwatch-scanner"),
            pod("db", "", deployment="recreated"), pod("monitoring", "alloy-x", owner="DaemonSet")]}))
        (self.state / "deployments.json").write_text(json.dumps({"items": [
            deployment("kube-system", "coredns"), deployment("argocd", "argocd-repo-server"),
            deployment("wigle", "wigle-console", replicas=2), deployment("overwatch", "overwatch-scanner"),
            deployment("db", "recreated", strategy="Recreate"), deployment("other", "coredns")]}))
        error, out = self.go(NODE="brick666", SHUTDOWN="false")
        self.assertIsNone(error)
        changes = self.changes()
        self.assertEqual(changes[1], "kubectl cordon brick666")
        # Only single-copy, rolling, not-pinned Deployments on this node, before the drain.
        self.assertEqual(changes[2:4], ["kubectl rollout restart -n argocd deployment/argocd-repo-server",
                                        "kubectl rollout restart -n kube-system deployment/coredns"])
        self.assertTrue(changes[4].startswith("kubectl drain brick666"))
        self.assertIn("kubectl rollout status -n kube-system deployment/coredns --timeout=2m", self.log())

    def test_a_copy_that_wont_move_is_left_to_the_drain(self):
        (self.state / "before.json").write_text(json.dumps({"items": [pod("x", "", deployment="slow")]}))
        (self.state / "deployments.json").write_text(json.dumps({"items": [deployment("x", "slow")]}))
        error, out = self.go(NODE="brick666", SHUTDOWN="false")
        self.assertIsNone(error)
        self.assertIn("warning: x/slow didn't move within 2m", out)
        self.assertTrue(any(c.startswith("kubectl drain") for c in self.changes()))

    def test_failed_drain_is_undone(self):
        (self.state / "drain_rc").write_text("1")
        error, _ = self.go(NODE="brick666")
        self.assertIn("didn't drain", error)
        changes = self.changes()
        self.assertIn("kubectl uncordon brick666", changes)
        self.assertEqual(changes[-1], "maintenance NODE=brick666 DURATION=1h END=true")
        self.assertFalse(any("poweroff" in c for c in changes))

    def test_pods_left_after_the_drain_stop_the_power_off(self):
        (self.state / "node.json").write_text(json.dumps({"items": [
            pod("monitoring", "node-exporter-x", owner="DaemonSet"), pod("default", "bare", owner="None")]}))
        error, _ = self.go(NODE="brick666")
        self.assertIn("still runs pods", error)
        self.assertIn("default/bare", error)
        self.assertNotIn("node-exporter", error)
        self.assertFalse(any("poweroff" in c for c in self.changes()))

    def test_host_down_stops_its_units_and_never_drains(self):
        error, _ = self.go(NODE="brick9000", DURATION="1h")
        self.assertIsNone(error)
        changes = self.changes()
        self.assertEqual(changes[0], "maintenance NODE=brick9000 DURATION=1h END=false")
        self.assertIn("jenkins-gate pause brick-roms.service brick-mirror.service", changes[1])
        self.assertIn("jenkins-gate poweroff", changes[2])
        self.assertFalse(any(c.startswith("kubectl") for c in changes))

    def test_without_shutdown_it_only_drains(self):
        error, out = self.go(NODE="brick666", SHUTDOWN="false")
        self.assertIsNone(error)
        self.assertFalse(any("poweroff" in c for c in self.changes()))
        self.assertIn("run ACTION=up", out)

    def test_up_uncordons_ends_the_window_and_smokes(self):
        error, _ = self.go(NODE="brick666", ACTION="up")
        self.assertIsNone(error)
        log = self.log()
        self.assertIn("kubectl wait --for=condition=Ready node/brick666 --timeout=5m", log)
        self.assertEqual(self.changes(), ["kubectl uncordon brick666",
                                          "maintenance NODE=brick666 DURATION=1h END=true", "smoke"])

    def test_bad_action(self):
        self.assertIn("ACTION must be", self.go(NODE="brick666", ACTION="sideways")[0])


if __name__ == "__main__":
    unittest.main()
