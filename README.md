# brick-cicd-config

CI/CD and the status board for the brick k3s cluster. Cluster-level
infrastructure (k3s, monitoring, the Sealed Secrets controller) lives in
[`brick-k8s-config`](https://github.com/operations-ivy/brick-k8s-config), and
the `SealedSecret` manifests themselves in the private
[`brick-k8s-secrets`](https://github.com/operations-ivy/brick-k8s-secrets); this
repo covers what builds and ships apps onto it, plus the wall display that
shows how that's going.

## Layout

| Where | What | Why there |
| --- | --- | --- |
| `brick2000` (in-cluster) | Jenkins | Needs RAM and disk; the NVMe worker has both |
| `brick9000` (standalone host) | Kiosk display, `brick-status` daemon, button lights | Needs the screen and GPIO buttons; must keep working when the cluster doesn't |

### brick9000 is not a cluster node

`brick9000` (192.168.1.221, static DHCP lease, `brick9000.local`) is a
Raspberry Pi with a screen and plasma buttons, from a repurposed Pimoroni
Picade. It deliberately does **not** join k3s:

- Its job is to report on the cluster, so it can't depend on the cluster. When
  the cluster is down, the board should say so instead of going dark.
- Everything it runs needs local hardware (display, GPIO buttons, screen
  blanking), which would run on the host even if it were a node.
- Unplugging or reimaging it never makes a node go `NotReady` or reschedules
  pods.

The cost: the cluster's node_exporter and Promtail DaemonSets don't cover it.
brick-status reads brick9000's own vitals directly, and its logs stay in its
own journal. Any secrets it needs would live in a gitignored `.env` on the
host instead of a SealedSecret.

## Jenkins

`jenkins/`: the [Jenkins Helm chart](https://github.com/jenkinsci/helm-charts),
with the controller and build agents pinned to `brick2000`
(`chuck.io/storage-node=true`, same as Loki/Tempo/Prometheus) and
`JENKINS_HOME` on a 20Gi `local-path` PVC on its NVMe. The controller runs no
builds itself (`numExecutors: 0`); each build gets an agent pod through the
Kubernetes plugin. Install:

```bash
kubectl apply -f jenkins/jenkins-namespace.yaml
helm repo add jenkins https://charts.jenkins.io
helm repo update
helm upgrade --install jenkins jenkins/jenkins --version 5.9.64 \
  --namespace jenkins -f jenkins/jenkins-values.yaml
kubectl apply -f jenkins/jenkins-ingress.yaml
```

Exposed on the LAN at `http://jenkins.local`. Announce the name over mDNS
from brick420 (see "LAN names for ingresses" in `brick-k8s-config`):

```bash
ssh zaphod@192.168.1.183 'sudo systemctl enable --now mdns-alias@jenkins'
```

The chart generates the `admin` password into the `jenkins` Secret, so no
password lives in git. Read it back with:

```bash
kubectl -n jenkins get secret jenkins -o jsonpath='{.data.jenkins-admin-password}' | base64 -d; echo
```

Save it in KeePass; a `helm upgrade` keeps the existing Secret.

The Prometheus plugin (`controller.additionalPlugins`) serves metrics at
`/prometheus`, and a `ServiceMonitor` (`controller.prometheus`) has the
cluster's Prometheus scrape them. Both are needed: enabling
`controller.prometheus` alone creates the `ServiceMonitor` but not the plugin.
Metric names are prefixed `default_jenkins_`. This is where `brick-status`
reads green/red build state from.

## brick-status (the status board on brick9000)

`brick-status/` is a small Python daemon (standard library plus
`python3-evdev`) that runs as a systemd user service on brick9000:

- Every 15s it asks Prometheus (`http://prometheus.local`, an Ingress defined
  in `brick-k8s-config`) about builds (Jenkins), the cluster (node
  readiness, workloads short of replicas, crash-looping pods, down scrape
  targets) and wigle-sync (Pushgateway metrics, plus a running CronJob pod
  meaning "uploading"). wigle-sync only alerts when every run for 2 hours has
  had errors; the pwnagotchi being away and syncs pausing are normal.
- It serves the board page on `127.0.0.1:8765`, which Chromium shows in
  kiosk mode (`brick9000/labwc-autostart`). The page polls it every second.
- The overview is a CRT window running the real `cmatrix -bs`, the three area
  tiles, and a vitals panel (CPU, memory, temperature, root disk) for brick420
  and brick2000 (from node_exporter) and brick9000 itself (read from `/proc`
  and `/sys`, since it runs no node_exporter). The header shows a red
  "SSHD Down" if sshd stops on brick9000, and nothing while it's up.
- cmatrix runs in a pseudo-terminal sized to the window; its output streams to
  the page as server-sent events and xterm.js draws it with its WebGL renderer
  (the default DOM renderer kept Chromium above a full core on the Pi 4; WebGL
  brought the whole system to about 27% CPU). The rain is green when all is
  well, orange when degraded or building, red when failing; cmatrix has no
  orange, so the page's terminal theme repaints its yellow. It runs only while
  the overview is on screen, and not overnight.
- It reads the front-panel buttons from the `gpio_keys` input device (grabbed,
  so presses don't reach Chromium) and drives the screen with `wlopm` and the
  button lights through the plasma daemon's FIFO, `/tmp/plasma`.

The board has three modes:

| Mode | When | Screen | Button lights |
| --- | --- | --- | --- |
| idle | no press in the last 2 minutes | overview | the most important event (below) |
| active | a button was pressed | the chosen view | each view's button in its area's health colour; the selected one pulses, and so does any area with a build or upload running (green) |
| quiet | 00:00-06:00 | off | off; any cabinet key wakes the board into active mode |

The six buttons are two rows of three. The top row picks Overview, Builds and
Cluster; bottom-left picks wigle-sync; the other two are free for now. The
joystick left/right steps through the views. The buttons bounce (a release
can be followed by a phantom press 16-120ms later), so presses within 150ms
of the same button's release are ignored. The other cabinet keys (Start,
Coin, joystick up/down) don't change the view but still count as input: they
wake the board overnight.

Idle light patterns, highest priority first:

| Pattern | Event |
| --- | --- |
| `alert` | red pulse: anything failing |
| `building` | amber chase: a Jenkins build is running |
| `uploading` | green pulse: wigle-sync is uploading |
| `warn` | slow amber pulse: something degraded |
| `unknown` | grey pulse: no data (e.g. Prometheus unreachable) |
| `calm` | slow green (0 255 0) breathing: all good |

The patterns are PNGs the daemon writes into `/etc/plasma/brick-status/` at
startup (40 pixels wide: 10 button slots of 4 LEDs; one row per frame at
60fps), so changing one needs only a restart, not a reinstall.

### Install on brick9000

brick9000 runs a git clone of this repo (public, so no credentials). Once, on
brick9000 (the installer uses `sudo` for the apt package and the pattern
directory):

```bash
git clone https://github.com/operations-ivy/brick-cicd-config.git ~/brick-cicd-config
~/brick-cicd-config/brick9000/install.sh
```

Settings live in `~/.config/brick-status/env` on brick9000 (created from
`brick9000/brick-status.env.example`). Logs are in the system journal
(`journalctl --user` finds nothing on this Pi):
`journalctl _SYSTEMD_USER_UNIT=brick-status.service -f`.

### Updates deploy from GitHub

Nothing is copied to brick9000 by hand. Every 2 minutes `brick-deploy.timer`
runs `brick9000/deploy`, which fetches from GitHub and, if the followed
branch has moved, runs the tests on the new commit in a scratch worktree. Only
if they pass does it switch the clone to that commit and restart
brick-status. A commit that fails is left alone until the branch moves again.
Changes under `brick9000/` itself (units, autostart) still need
`install.sh` rerun; the deploy log says so.

brick9000 follows `main`. To try a branch on the real board before merging,
set `BRICK_DEPLOY_BRANCH=<branch>` in the env file; it deploys on the next
check. Set it back to `main` after merging. Deploy logs:
`journalctl _SYSTEMD_USER_UNIT=brick-deploy.service`.

Tests (standard library `unittest`, no hardware needed):

```bash
scripts/test
```

## Making changes

Tests run on the laptop before anything reaches GitHub. Enable the pre-push
hook once per clone; it refuses any push whose tests fail:

```bash
git config core.hooksPath .githooks
```

Work on a branch, commit, then:

```bash
scripts/open-pr
```

It runs the tests, and only if they pass pushes the branch, opens a PR
against `main` (titled from the commits) and comments a test summary on it
(`scripts/test-report`: one lit square per test, details folded away).
Run it again after more commits to push them and post a fresh result.

The page's fonts (Barlow Condensed, IBM Plex Mono) and xterm.js are vendored
under `brick-status/brick_status/static/vendor/` with their licenses, so the
board needs no internet access.
