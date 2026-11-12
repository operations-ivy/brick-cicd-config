#!/usr/bin/env python3
"""The only commands Jenkins' SSH key can run on a host (#133).

Installed root-owned as /usr/local/bin/jenkins-gate on every host Jenkins
reaches (jenkins/gate/install), and forced on Jenkins' key in zaphod's
authorized_keys:

    command="/usr/local/bin/jenkins-gate",restrict ssh-ed25519 AAAA... jenkins@...

So the key gets no shell, no port forwarding and no general sudo, only these:

    true                              does the host answer?
    deploy <repo> <branch>            point the host's deploy at a branch and start it
    is-active [--user] <unit>         systemctl's state of a unit
    board-state                       brick-status's /api/state, which listens on localhost only
    busy [proc:<name>|unit:<name>]... one line per process running / user unit active
    pause <unit>...                   stop user units (their timers rerun them), reset-failed
    poweroff                          power off 5 s from now
    crictl-images                     k3s' images, as JSON
    crictl-rmi <ref>                  remove one of k3s' images
    docker-images                     the local Docker images, repo:tag per line
    docker-rmi <ref>                  remove one local Docker image

The callers run "jenkins-gate <command>", so the same call works from the laptop
(any key) when the gate is installed: through Jenkins' key the forced command
replaces it and reads the words from SSH_ORIGINAL_COMMAND. Every argument
must be a plain name; nothing reaches a shell.
"""

import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:/+-]*")  # fullmatch: no trailing newline
# repo -> (env file under $HOME, its variable, the deploy unit)
DEPLOYS = {
    "brick-cicd-config": (".config/brick-status/env", "BRICK_DEPLOY_BRANCH", "brick-deploy.service"),
    "rpi_lcars": (".config/lcars/env", "LCARS_DEPLOY_BRANCH", "lcars-deploy.service"),
}
BOARD_STATE = "http://127.0.0.1:8765/api/state"
# Through Jenkins' key the commands are found on this PATH only, whatever the
# environment says.
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


Action = Callable[[Path], int]  # given $HOME, does the work; the exit status


class Refused(Exception):
    """A request that isn't on the list."""


def names(words: list[str], count: int | None = None) -> list[str]:
    """The arguments, if each is a plain name (and there are `count` of them)."""
    if count is not None and len(words) != count:
        raise Refused(f"expected {count} argument(s)")
    for w in words:
        if not NAME.fullmatch(w):
            raise Refused(f"not a plain name: {w!r}")
    return words


def commands(*argv: list[str]) -> Action:
    """Run each command in turn, stopping at the first that fails."""
    def action(_home: Path) -> int:
        for command in argv:
            if status := subprocess.run(command).returncode:
                return status
        return 0
    return action


def plan(words: list[str]) -> Action:
    """What a request does. Raises Refused for anything not on the list."""
    if words[:1] == ["jenkins-gate"]:
        words = words[1:]
    if not words:
        raise Refused("no command")
    command, args = words[0], words[1:]
    match command:
        case "true":
            names(args, 0)
            return commands(["true"])
        case "is-active":
            user = args[:1] == ["--user"]
            (unit,) = names(args[1:] if user else args, 1)
            return commands(["systemctl", *(["--user"] if user else []), "is-active", unit])
        case "board-state":
            names(args, 0)
            return commands(["curl", "-s", "--max-time", "5", BOARD_STATE])
        case "pause":
            if not args:
                raise Refused("pause needs units")
            return commands(["systemctl", "--user", "stop", *names(args)],
                            ["systemctl", "--user", "reset-failed"])
        case "poweroff":
            names(args, 0)
            return commands(["sudo", "-n", "systemd-run", "--on-active=5", "systemctl", "poweroff"])
        case "crictl-images":
            names(args, 0)
            return commands(["sudo", "-n", "k3s", "crictl", "images", "-o", "json"])
        case "crictl-rmi":
            return commands(["sudo", "-n", "k3s", "crictl", "rmi", *names(args, 1)])
        case "docker-images":
            names(args, 0)
            return commands(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"])
        case "docker-rmi":
            return commands(["docker", "rmi", *names(args, 1)])
        case "deploy":
            repo, branch = names(args, 2)
            if repo not in DEPLOYS:
                raise Refused(f"no deploy for {repo}")
            return lambda home: deploy(repo, branch, home)
        case "busy":
            if not args:
                raise Refused("busy needs proc:<name> or unit:<name>")
            for arg in names(args):
                kind, _, name = arg.partition(":")
                if kind not in ("proc", "unit") or not name:
                    raise Refused(f"busy takes proc:<name> or unit:<name>, not {arg!r}")
            return lambda _home: busy(args)
    raise Refused(f"unknown command: {command}")


def set_env(path: Path, var: str, value: str) -> None:
    """Replace the variable's line in an env file, or add one."""
    lines = path.read_text().splitlines() if path.exists() else []
    line = f"{var}={value}"
    if any(old.startswith(f"{var}=") for old in lines):
        lines = [line if old.startswith(f"{var}=") else old for old in lines]
    else:
        lines.append(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")  # whole or not at all: the deploy reads it
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)


def deploy(repo: str, branch: str, home: Path) -> int:
    env_file, var, unit = DEPLOYS[repo]
    set_env(home / env_file, var, branch)
    return subprocess.run(["systemctl", "--user", "start", "--no-block", unit]).returncode


def busy(args: list[str]) -> int:
    """One line per reason not to stop now (power-down's checks)."""
    for arg in args:
        kind, _, name = arg.partition(":")
        if kind == "proc":
            hit = subprocess.run(["pgrep", "-x", name], stdout=subprocess.DEVNULL).returncode == 0
        else:
            hit = subprocess.run(["systemctl", "--user", "is-active", "--quiet", name]).returncode == 0
        if hit:
            print(f"{'running' if kind == 'proc' else 'active'}: {name}", flush=True)
    return 0


def main(argv: list[str]) -> int:
    original = os.environ.get("SSH_ORIGINAL_COMMAND")
    if original is not None:  # sshd's forced command: Jenkins' key
        os.environ["PATH"] = SAFE_PATH
    words = original.split() if original is not None else argv[1:]
    try:
        action = plan(words)
    except Refused as e:
        print(f"jenkins-gate: refused: {e}", file=sys.stderr)
        return 126
    return action(Path.home())


if __name__ == "__main__":
    sys.exit(main(sys.argv))
