"""jenkins/bin/github-status, against a stand-in GitHub API."""

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "jenkins/bin/github-status"
A, B = "a" * 40, "b" * 40
IMAGE = "whitepatrick/brick-jenkins"


class GitHub(http.server.BaseHTTPRequestHandler):
    posts, answer = [], 201

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        GitHub.posts.append((self.path, self.headers.get("Authorization"), body))
        self.send_response(GitHub.answer)
        self.end_headers()

    def log_message(self, *args):
        pass


class GithubStatus(unittest.TestCase):
    def setUp(self):
        GitHub.posts, GitHub.answer = [], 201
        self.server = http.server.HTTPServer(("127.0.0.1", 0), GitHub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        self.token = self.dir / "github-token"
        self.queue = self.dir / "queue"

    def run_it(self, *lines):
        new = self.dir / "new"
        new.write_text("".join(line + "\n" for line in lines))
        env = {**os.environ, "GITHUB_API": f"http://127.0.0.1:{self.server.server_port}",
               "GITHUB_TOKEN_FILE": str(self.token)}
        out = subprocess.run([sys.executable, str(SCRIPT), str(self.queue), str(new)],
                             capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(out.returncode, 0)
        return out

    def test_posts_the_state_with_the_token(self):
        self.token.write_text("tok\n")
        self.run_it(f"brick-cicd-config {A} {IMAGE} 2.580.1-aaaaaaaaaaaa success https://jenkins/job/build-image/5/")
        path, auth, body = GitHub.posts[0]
        self.assertEqual(path, f"/repos/operations-ivy/brick-cicd-config/statuses/{A}")
        self.assertEqual(auth, "Bearer tok")
        self.assertEqual(body, {"state": "success", "context": "jenkins-image",
                                "description": f"{IMAGE}:2.580.1-aaaaaaaaaaaa is on Docker Hub",
                                "target_url": "https://jenkins/job/build-image/5/"})
        self.assertEqual(self.queue.read_text(), "")

    def test_without_a_token_they_wait_then_go(self):
        out = self.run_it(f"brick-cicd-config {A} {IMAGE} 2.580.1-aaaaaaaaaaaa pending")
        self.assertIn("wait for it", out.stderr)
        self.assertEqual(GitHub.posts, [])
        self.token.write_text("tok")
        self.run_it()
        self.assertEqual([p[2]["state"] for p in GitHub.posts], ["pending"])

    def test_the_newest_state_per_commit_wins(self):
        self.run_it(f"brick-cicd-config {A} {IMAGE} t pending", f"brick-cicd-config {B} {IMAGE} t pending")
        self.run_it(f"brick-cicd-config {A} {IMAGE} t success")
        self.token.write_text("tok")
        self.run_it()
        self.assertEqual(sorted((p[0][-40:], p[2]["state"]) for p in GitHub.posts),
                         [(A, "success"), (B, "pending")])

    def test_github_down_keeps_them_and_a_bad_line_is_dropped(self):
        self.token.write_text("tok")
        GitHub.answer = 500
        self.run_it(f"brick-cicd-config {A} {IMAGE} t success", "brick-cicd-config not-a-sha x t success",
                    f"brick-cicd-config {B} {IMAGE} t; rm -rf / success")
        self.assertEqual(len(GitHub.posts), 1)
        self.assertIn(A, self.queue.read_text())
        GitHub.answer = 201
        self.run_it()
        self.assertEqual(self.queue.read_text(), "")

    def test_the_token_is_never_printed(self):
        self.token.write_text("s3cret-tok")
        GitHub.answer = 403
        out = self.run_it(f"brick-cicd-config {A} {IMAGE} t success")
        self.assertNotIn("s3cret-tok", out.stdout + out.stderr)
        self.assertIn("403", out.stderr)


if __name__ == "__main__":
    unittest.main()
