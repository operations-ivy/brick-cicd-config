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
| `brick9000` (standalone host) | Kiosk display, `brick-status` daemon, button lights | Needs the screen and GPIO buttons; must keep working when the cluster doesn't |
| `brick9000` (Docker) | Jenkins: bootstrap and maintenance jobs | Has to work when the cluster doesn't, and be able to rebuild it |
| `brick9000` (Docker) | Caddy on port 80: `jenkins.local`, and `brick-status.local` (a read-only mirror of the board) | One port, two names; both services listen only on localhost |

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
own journal. The secrets it needs live in files on the host
(`~/.config/brick-jenkins/secrets/`), never in git or a SealedSecret.

## Jenkins (on brick9000)

brick9000 should be able to rebuild the whole brick homelab in an emergency,
so the Jenkins that runs maintenance and bootstrap jobs can't live in the
cluster it rebuilds. It runs on brick9000 in Docker (`brick9000/jenkins/`),
at `http://jenkins.local`, and jobs run on brick9000 itself (no agents). It
listens only on `127.0.0.1:8080`; the proxy (below) is what serves the name.

- **Nothing at start-up needs the internet.** The image
  (`brick9000/jenkins/Dockerfile`) has the plugins (`plugins.txt`), `kubectl`,
  `helm`, the Docker CLI, `ssh` and Python baked in; change one by rebuilding.
- **Configuration is code.** `casc.yaml` (users, permissions) and
  `seed.groovy` (the jobs) are read from brick9000's git clone, mounted
  read-only into the container. deploy rebuilds the image when the
  Dockerfile, plugins or compose file change, and otherwise reloads the
  configuration when `casc.yaml`, `seed.groovy` or `jobs/` change. The job
  scripts in `bin/` are read live. Changes made in the UI are overwritten.
- **One file per job.** `jobs/<name>.groovy` is a declarative pipeline whose
  header comments give its description, parameters and schedule (see
  `seed.groovy`); the pipeline calls a script in `bin/`, where the logic lives.
- **Webhooks from the LAN.** Every job can be started with
  `curl -X POST 'http://jenkins.local/generic-webhook-trigger/invoke?token=<job>-<secret>'`,
  parameters in the query string (`&APPLY=true`). The secret is
  `~/.config/brick-jenkins/secrets/webhook_secret` on brick9000.
- **Cluster access per run.** Jobs reach the hosts with Jenkins' own SSH key
  (authorized for `zaphod` on each host) and fetch a kubeconfig from brick420
  over SSH for each run, so no cluster credentials are stored on brick9000.
- **Offline copies of the repos.** `mirror-repos` keeps clones of
  brick-k8s-config, wigle-sync and chucks-wisdom every 2 hours; the other jobs
  read manifests and scripts from those, so they work without GitHub.

| Job | Does | Webhook parameters |
| --- | --- | --- |
| `mirror-repos` | Refreshes the local repo clones (also every 2 hours) | |
| `prune-images` | Keeps the newest two versions of each app image on Docker Hub, both nodes and brick9000 | `APPLY=true` to delete (dry run otherwise) |
| `wigle-sync-now` | Runs wigle-sync from its CronJob now | |
| `deploy-brick9000` | Starts brick9000's deploy now instead of at its 2-hourly timer (returns at once; the deploy logs to `journalctl --user -u brick-deploy` on brick9000) | |
| `chuck-importer` | Runs the chucks-wisdom joke importer (a Kubernetes Job) | `QUERY`, `CATEGORIES`, `JOKES`, `TRIES_PER_CATEGORY`, `MAX_DUPLICATES`, `SLEEP_SECONDS` (see chucks-wisdom's `CLUSTER_SETUP.md`), `WAIT=true` to wait for it |

### Set up

`brick9000/install.sh` runs `brick9000/jenkins/setup` (generates the secrets:
admin and brick-status passwords, the webhook secret, a config reload token
and Jenkins' SSH key; reruns never replace one) and `brick9000/jenkins/up`
(builds the image and starts the container; Docker restarts it on boot). Then,
once, authorize Jenkins' key on each host, from a machine that can already
reach them:

```bash
key=$(ssh zaphod@brick9000.local cat .config/brick-jenkins/secrets/ssh_key.pub)
for h in 192.168.1.183 192.168.1.170 192.168.1.221; do
    ssh zaphod@$h "grep -qxF '$key' ~/.ssh/authorized_keys || echo '$key' >>~/.ssh/authorized_keys"
done
```

When reimaging a host, preload that key for `zaphod` (Raspberry Pi Imager can)
so Jenkins can reach it again. Sign in as `admin` with the password in
`~/.config/brick-jenkins/secrets/admin_password` (save it in KeePass). Logs:
`journalctl CONTAINER_NAME=brick-jenkins`; `JENKINS_HOME` is
`~/.local/share/brick-jenkins`.

### Port 80: the proxy

`brick9000/proxy/` runs Caddy (Docker, host networking) on port 80, routing
by name (`caddy/Caddyfile`): `jenkins.local` to Jenkins on `127.0.0.1:8080`, and
anything else to brick-status on `127.0.0.1:8765` as the read-only mirror.
Plain HTTP, LAN only. `install.sh` starts it; deploy reloads it when
`brick9000/proxy/` changes. brick9000 announces the extra names with
`mdns-alias@<name>` user units (`jenkins`, `brick-status`). The catch-all also
sends `/prometheus` to Jenkins, so the cluster's Prometheus can scrape Jenkins'
metrics at `192.168.1.221:80` (see brick-k8s-config's kube-prometheus-stack
values). Logs: `journalctl CONTAINER_NAME=brick-proxy`.

## brick-status (the status board on brick9000)

`brick-status/` is a small Python daemon (standard library plus
`python3-evdev`) that runs as a systemd user service on brick9000:

- Every 15s it asks Jenkins, on brick9000 itself, for each job's state (as the
  read-only `brick-status` user), so the Jenkins view keeps working while the
  cluster is down, which is when the bootstrap jobs run. The Jenkins tile shows
  the pixel-art Jenkins, or the horned one in flames when a job has failed
  (artwork from [jenkins.io](https://jenkins.io/), CC BY-SA 3.0, credited on the
  Jenkins view; see `static/vendor/jenkins/`). The other tiles have our own
  pixel art to match (`brick-status/tools/pixel_art.py` draws it into
  `static/art/`): a smiling server for the cluster, or one on fire when it's
  down, and two plugs for wigle-sync, connected while syncing is OK, pulled
  apart and sparking otherwise.
- It asks Prometheus (`http://prometheus.local`, an Ingress defined
  in `brick-k8s-config`) about the cluster (node
  readiness, workloads short of replicas, crash-looping pods, down scrape
  targets) and wigle-sync (Pushgateway metrics, plus a running CronJob pod
  meaning "uploading"). The wigle-sync view has three rows: how the last sync
  with the Pi went (OK, failed, or waiting for internet), how many files it
  uploaded, and when the next one runs. It only alerts when every run for 2
  hours has had errors; the pwnagotchi being away and syncs pausing are normal.
- It also checks the internet itself, by opening a TCP connection to three
  public DNS anycast addresses (1.1.1.1, 8.8.8.8, 9.9.9.9); any one answering
  means it's up. An ISP outage is outside our control, so it isn't a failure:
  the header shows an orange "Internet Down", pods stuck in
  `ImagePullBackOff`/`ErrImagePull` (and the workloads they belong to) count
  as degraded rather than down, and wigle-sync counts uploads it couldn't make
  as deferred, not failed.
- It serves the board page on `127.0.0.1:8765`, which Chromium shows in
  kiosk mode (`brick9000/labwc-autostart`). The page polls it every second,
  and reloads itself when brick-status restarts, so a deploy updates the
  screen too.
- The same page is on the LAN, read-only, at `http://brick-status.local` (or
  `brick9000.local`): a plain mirror of what the cabinet shows, including which
  view is up. It changes nothing on the cabinet. Its CRT window stays empty
  apart from its label, since only the kiosk runs cmatrix: requests that come
  through the proxy (it adds `X-Forwarded-For`) can't start one, which would
  stop the kiosk's. While the cabinet is quiet the header says "Display Off"
  and the data keeps updating.
- The overview is a CRT window running the real `cmatrix -bs`, the three area
  tiles, and a compact vitals strip for brick420 and brick2000 (from
  node_exporter) and brick9000 itself (read from `/proc` and `/sys`, since it
  runs no node_exporter). It shows no numbers: each host gets a pie for root
  disk and LED segment bars for CPU, memory and temperature, green, yellow or
  red by level (CPU and memory turn yellow at 85% and red at 95%, temperature
  at 70°C and 80°C, disk at 85% and 95%). The header shows a red
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
| quiet | the quiet schedule (below) | off | off; only the side button does anything: it turns everything on for 30 minutes |

The six buttons are two rows of three. The top row picks Overview, Jenkins and
Cluster; bottom-left picks wigle-sync; the other two are free for now. The
joystick left/right steps through the views. The buttons bounce (a release
can be followed by a phantom press 16-120ms later), so presses within 150ms
of the same button's release are ignored. The other cabinet keys (Start,
Coin, joystick up/down) don't change the view but count as input.

The quiet schedule is `BRICK_STATUS_QUIET` in the env file: daily windows
(`00:00-06:00`) or weekly ones (`Mon 00:00-Fri 16:00`), `;`-separated entries
each optionally starting on a date (`2026-10-09: ...`, from midnight). The
latest entry that has started applies, so a schedule change can be set up
ahead of time. brick9000's is off overnight (00:00-06:00) until 2026-10-09,
then off through the work week, Monday 00:00 to Friday 16:00, and on all
weekend. While it's quiet the cabinet keys do nothing except the side button
(`KEY_ESC`): it turns everything on for 30 minutes
(`BRICK_STATUS_WAKE_SECONDS`), and the header shows "Awake until ..." until
then. Pressed while the board is already on, including during those 30
minutes, it does nothing.

Idle light patterns, highest priority first:

| Pattern | Event |
| --- | --- |
| `alert` | red pulse: anything failing |
| `building` | amber chase: a Jenkins job is running |
| `uploading` | green pulse: wigle-sync is uploading |
| `warn` | slow amber pulse: something degraded |
| `unknown` | grey pulse: no data (e.g. Prometheus unreachable) |
| `calm` | slow green (0 255 0) breathing: all good |

An image build on brick9000 itself (below) takes over the lights in idle and
active mode: `image-building` flashes rainbow while it runs, then
`image-pushed` flashes bright green or `image-failed` pulses red for a minute
(`BRICK_STATUS_BUILD_RESULT_SECONDS`).

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

Nothing is copied to brick9000 by hand. Every 2 hours (and 2 minutes after
boot) `brick-deploy.timer` runs `brick9000/deploy`, which fetches from GitHub
and, if the followed branch has moved, runs the tests on the new commit in a
scratch worktree. Only if they pass does it switch the clone to that commit
and restart brick-status. A commit that fails is left alone until the branch
moves again. Changes under `brick9000/` itself (units, autostart) still need
`install.sh` rerun; the deploy log says so.

brick9000 follows `main`. To try a branch on the real board before merging,
set `BRICK_DEPLOY_BRANCH=<branch>` in the env file, then check now instead of
waiting for the timer:

```bash
systemctl --user start brick-deploy.service
```

Run that again after each push to the branch. Set it back to `main` after
merging (and run it once more). Deploy logs:
`journalctl _SYSTEMD_USER_UNIT=brick-deploy.service`.

### Image builds on brick9000

brick9000 is an arm64 Pi 4 with Docker, so it can build the cluster's (arm64)
images natively. `brick9000/build-image` checks out a ref of a repo, builds
it and pushes the image, and records its progress in
`~/.local/state/brick-build/status.json` for the lights. Run it detached, so
an SSH drop doesn't kill the build and its log lands in the journal:

```bash
systemd-run --user --collect --unit=brick-build ~/brick-cicd-config/brick9000/build-image \
    https://github.com/operations-ivy/wigle-sync <ref> whitepatrick/wigle-console:<tag> Dockerfile.web
journalctl _SYSTEMD_USER_UNIT=brick-build.service -f
```

Pushing needs a one-time `docker login -u whitepatrick` on brick9000 with a
Docker Hub access token (Read & Write), which Docker keeps in
`~/.docker/config.json`, outside the repo.

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
