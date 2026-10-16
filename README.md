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
| `brick9000` (Docker) | Caddy on ports 80 and 443: every app at `https://<name>.brick.nozdormu.cloud` | The front door for the whole homelab; the board stays reachable when the cluster is down |
| `brick9000` (systemd timer) | `brick-mirror`: offline copies of the brick repos | Material for rebuilding the cluster, kept outside it |
| cluster (`jenkins` namespace, on brick2000) | Jenkins: maintenance jobs, later builds and tests in agent pods | Nothing it does needs to work while the cluster is down; keeps compiling off brick9000 |
| `brick1982` (host) | 7" touchscreen: the LCARS panel (operations-ivy/rpi_lcars); host tooling from here | Needs the screen; left the cluster 2026-10-14 to become a wall display |

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
brick-status reads brick9000's own vitals directly (so the board shows them
when the cluster is down), and its logs stay in its own journal. It runs its
own node-exporter for Prometheus (below, "Metrics"); being scraped never
makes it depend on the cluster. The secrets it needs live in files on the host, never in git or
a SealedSecret. `~/.config/brick-jenkins/secrets/` there also keeps the
plaintext of Jenkins' secrets, which are sealed from it (below).

## Jenkins (on the cluster)

Jenkins runs on the cluster, a Helm release of the
[jenkinsci chart](https://github.com/jenkinsci/helm-charts) in the `jenkins`
namespace, pinned to brick2000 (`jenkins/values.yaml`). It's at
`https://jenkins.brick.nozdormu.cloud`, through brick9000's proxy and the
cluster's Traefik.

It ran on brick9000 in Docker from 2026-09-30 to 2026-10-08, so it could
rebuild the cluster from outside it. None of its jobs did, though: they all
work on a running cluster (or on brick9000 over SSH), so it moved back. The
offline repo copies, which a rebuild would need, stay on brick9000
(`brick-mirror.timer`). A recovery procedure, when there is one, should be a
script in git that runs from anywhere, not a Jenkins job.

- **Nothing at start-up needs the internet.** The image
  (`jenkins/Dockerfile`, `whitepatrick/brick-jenkins` on Docker Hub) has the
  plugins (`plugins.txt`), `kubectl`, `helm`, the Docker CLI, `ssh` and
  Python baked in, and so do the jobs and their scripts. The chart's own
  plugin download and config-reload sidecar are off. Agent pods
  (`jenkins/inbound-agent`) are the one exception, and only build jobs use them.
- **Configuration is code.** `values.yaml` holds the users, permissions and
  configuration as code; `seed.groovy` turns `jobs/<name>.groovy` into jobs
  at every start. Changes made in the UI are overwritten.
- **One file per job.** `jobs/<name>.groovy` is a declarative pipeline whose
  header comments give its description, parameters and schedule (see
  `seed.groovy`); the pipeline calls a script in `bin/`, where the logic lives.
  Maintenance jobs run on the controller (`agent { label 'built-in' }`), which
  has the tools and secrets; the controller takes nothing else, so build and
  test jobs get a throwaway agent pod on brick2000.
- **Agent pods** run in the `jenkins-agents` namespace, where the controller
  may only manage pods. They run as a ServiceAccount with no token, so the
  repo code a test job runs can't call the cluster's API at all. Each test
  job's pod has the agent's `jnlp` container (it has git, and clones the
  repo) and one for the tests, with CPU and memory limits:
  `test-brick-cicd-config` uses this Jenkins image (already on brick2000,
  with git, Python 3 and ssh; a test keeps its tag in step with
  `values.yaml`), the Poetry repos the same pinned `python:3.11` image and
  Poetry as their Dockerfiles. Those two images come from Docker Hub, so a
  node that hasn't pulled them can't start a test while the internet is
  down. chucks-wisdom's database tests need Docker, so they skip in a pod.
- **The plasma library drives the cabinet's buttons.** `jenkins/lib/*.groovy`
  is a shared library the seed puts ahead of every job's pipeline (no Jenkins
  library plugin source to fetch: it's baked into the image like the jobs).
  `steps { script { plasma('test') { ... } } }` marks the running build with
  what it's doing (its description becomes `plasma:test` for the block's
  length). brick-status reads the mark from running builds as it polls
  Jenkins and lights that stage's pattern (README, "brick-status", lights):
  a pull, so it works from agent pods, which can't reach brick9000. Kinds:
  `checkout`, `install`, `test`, `verify`, `build`, `push`, `deploy`.
- **Reproducible images.** `build-image` and `verify-images` share one recipe
  (`lib/build.groovy`): rootless BuildKit pinned by digest,
  `SOURCE_DATE_EPOCH` = the commit's time with file timestamps rewritten to
  it, the `org.opencontainers.image.revision` and `.source` labels, no
  provenance or SBOM. A commit always builds the same digest (two `--no-cache`
  builds of wigle-sync matched on brick2000), so `verify-images` can check
  every published image against its source. Pushes use the `dockerhub`
  credential (the Secret's `dockerhub-username` and `dockerhub-token`).
  `bin/hub-image-info` reads a tag's digest and labels from Docker Hub.
- **Webhooks from the LAN.** Every job can be started with
  `curl -X POST 'https://jenkins.brick.nozdormu.cloud/generic-webhook-trigger/invoke?token=<job>-<secret>'`,
  parameters in the query string (`&APPLY=true`). The secret is
  `~/.config/brick-jenkins/secrets/webhook_secret` on brick9000, the same as
  before the move, so existing webhook URLs still work.
- **Cluster access.** Jobs act on the cluster as the `jenkins`
  ServiceAccount, with Roles only in the namespaces they touch (`wigle`,
  `chuck`; `extraObjects` in `values.yaml`). They reach the hosts with
  Jenkins' own SSH key (authorized for `zaphod` on each host).
- **Offline copies of the repos.** `mirror-repos` keeps clones of
  brick-k8s-config, wigle-sync and chucks-wisdom in Jenkins' home every 2
  hours; the other jobs read manifests and scripts from those, so they work
  without GitHub. brick9000 keeps its own copies the same way
  (`~/.local/share/brick-mirrors`).
- **Metrics.** The chart's ServiceMonitor has Prometheus scrape Jenkins as
  `job="jenkins"`, the same job name it had on brick9000.

| Job | Does | Webhook parameters |
| --- | --- | --- |
| `mirror-repos` | Refreshes the local repo clones (also every 2 hours) | |
| `prune-images` | Keeps the newest two versions of each app image on Docker Hub, both nodes and brick9000 (its Docker over SSH) | `APPLY=true` to delete (dry run otherwise) |
| `wigle-sync-now` | Runs wigle-sync from its CronJob now | |
| `deploy-brick9000` | Points brick9000's deploy at a branch and starts it now instead of at its 2-hourly timer, which then keeps following that branch (returns at once; the deploy logs to `journalctl _SYSTEMD_USER_UNIT=brick-deploy.service` on brick9000) | `BRANCH=<branch>` to try a branch; without it, or empty, `main` |
| `deploy-brick1982` | The same for brick1982's deploy | `BRANCH=<branch>`; without it, `main` |
| `test-<repo>` | Runs a repo's tests in an agent pod: `test-brick-cicd-config` (`scripts/test`), `test-wigle-sync`, `test-chucks-wisdom`, `test-overwatch` (`poetry run pytest`) | `BRANCH=<branch>`; without it, `main` |
| `test-all` | Runs every `test-<repo>` on `main`, one at a time, and fails naming the repos that failed (nightly) | |
| `build-image` | Builds an app image (`lib/build.groovy`'s table: wigle-sync, wigle-console, joke-reader, joke-importer, overwatch) from a commit of its repo, reproducibly, in a rootless BuildKit pod on brick2000, and pushes it to Docker Hub; refuses a tag Hub already has | `IMAGE=whitepatrick/<name>`, `REF=<full commit>`, `TAG=<version>`; `PUSH=false` to only build |
| `verify-images` | For each app image, rebuilds the commit its newest Hub tag's revision label names, from scratch, and fails naming any whose digest differs (nightly, after `test-all`) | |
| `chuck-importer` | Runs the chucks-wisdom joke importer (a Kubernetes Job) | `QUERY`, `CATEGORIES`, `JOKES`, `TRIES_PER_CATEGORY`, `MAX_DUPLICATES`, `SLEEP_SECONDS` (see chucks-wisdom's `CLUSTER_SETUP.md`), `WAIT=true` to wait for it |

### Secrets

The `brick-jenkins` Secret holds the admin and `brick-status` passwords, the
webhook secret and Jenkins' SSH key; every key is a file in
`/run/secrets/additional` in the pod. Its plaintext of record is
`~/.config/brick-jenkins/secrets/` on brick9000 (save the admin password in
KeePass too). `jenkins/seal-secrets`, run on the laptop, seals those files
into `brick-k8s-secrets` at `jenkins/brick-jenkins-sealedsecret.yaml`:

```bash
jenkins/seal-secrets
kubectl apply -f ~/code/brick-k8s-secrets/jenkins/brick-jenkins-sealedsecret.yaml
```

`prune-images` deletes Docker Hub tags only with an access token that has
delete scope: put one in `dockerhub_token` in that directory and seal again.
Without it, it prunes the nodes and brick9000, and only lists what it would
delete on Hub.

To authorize Jenkins' key on each host, from a machine that can already
reach them:

```bash
key=$(ssh zaphod@brick9000.local cat .config/brick-jenkins/secrets/ssh_key.pub)
for h in 192.168.1.183 192.168.1.170 192.168.1.221; do
    ssh zaphod@$h "grep -qxF '$key' ~/.ssh/authorized_keys || echo '$key' >>~/.ssh/authorized_keys"
done
```

When reimaging a host, preload that key for `zaphod` (Raspberry Pi Imager can)
so Jenkins can reach it again.

### Releasing

Any change under `jenkins/` other than `values.yaml` changes the image, so
bump `controller.image.tag` in `values.yaml` along with it (`<Jenkins
version>-<n>`). After merging, from the laptop:

```bash
jenkins/release
```

It builds and pushes the tag on brick2000's BuildKit if Docker Hub doesn't
have it (refusing a tag already built from different files), then runs
`helm upgrade --install` with the chart version it pins. Configuration
changes apply when the pod restarts, which the upgrade does. Logs:
`kubectl -n jenkins logs statefulset/jenkins -c jenkins`.

### Ports 80 and 443: the proxy

`brick9000/proxy/` runs Caddy (Docker, host networking, `caddy/Caddyfile`) as
the front door for every web UI in the homelab, each at its own name under
`brick.nozdormu.cloud`, over HTTPS:

| Name | Goes to |
| --- | --- |
| `status.brick.nozdormu.cloud` | the board's read-only mirror on `127.0.0.1:8765` |
| `volume.brick.nozdormu.cloud` | brick-status's volume page (`/volume`), the only name it takes volume changes through |
| `jenkins.`, `grafana.`, `prometheus.`, `wigle.`, `radar.`, `reader.`, `overwatch.`, `homeassistant.brick.nozdormu.cloud` | Traefik on either node, port 80, which routes by the same name |
| `dashboard.brick.nozdormu.cloud` | Traefik's HTTPS entrypoint (the Dashboard's self-signed backend) |

Plain HTTP to any of them redirects to HTTPS. brick9000 is the front door
rather than the cluster's Traefik so that the board still answers when the
cluster is down. A node that stops answering is skipped for 30s.
Every app gets its own origin, so none of them needs to know it's behind a
proxy.

How the names work, and the hand-made parts outside git:

- **Public DNS** (Porkbun, which also registers `nozdormu.cloud`): one record,
  `A *.brick` to `192.168.1.221`. From outside the house it leads nowhere.
- **The router's DNS** drops public answers that point
  at private addresses (DNS rebinding protection), so each name also has an
  entry in its static DNS host table, pointing at
  `192.168.1.221`. It doesn't take wildcards: **a new app needs a new entry
  there**. These entries also keep the names working while the internet is
  down.
- **Certificates**: one Let's Encrypt wildcard for `*.brick.nozdormu.cloud`,
  through the DNS challenge (the image adds Caddy's Porkbun plugin; see
  `Dockerfile`), so nothing needs to be reachable from outside. Caddy renews it
  itself. The Porkbun API keys are in
  `~/.config/brick-proxy/secrets/porkbun_api_key` and `porkbun_secret_key`
  (mode 600, never in git), given to the container as Docker secrets. API
  access must be on for the domain in Porkbun's settings. `proxy/up` creates
  them empty if missing, and the proxy runs without them; only the HTTPS names
  wait for a certificate. Porkbun's keys can change every domain on the
  account, so treat them like any other secret.

The old names redirect (`308`, so a webhook's POST stays a POST, with
`curl -L`): `http://jenkins.local` to Jenkins, and anything else on port 80
(`brick-status.local`, `brick9000.local`, the bare address) to the board's
mirror. brick9000 still announces them with `mdns-alias@<name>` user units
(`jenkins`, `brick-status`) until they're retired.

`install.sh` starts it; deploy rebuilds the image (only when the `Dockerfile`
changes does that take long) and reloads it when `brick9000/proxy/` changes.
Logs: `journalctl CONTAINER_NAME=brick-proxy`.

## brick-status (the status board on brick9000)

`brick-status/` is a small Python daemon (standard library plus
`python3-evdev`) that runs as a systemd user service on brick9000:

- Every 15s it asks Jenkins, on the cluster, for each job's state (as the
  read-only `brick-status` user, `BRICK_STATUS_JENKINS_PASSWORD` in the env
  file). A passing build shows green, a running one amber, a failed one red.
  When Jenkins can't be reached the Builds view shows no data (grey) rather
  than a failure: the Cluster view says why. The Builds tile shows
  the pixel-art Jenkins, or the horned one in flames when a job has failed
  (artwork from [jenkins.io](https://jenkins.io/), CC BY-SA 3.0, credited on the
  Builds view; see `static/vendor/jenkins/`). The other tiles have our own
  pixel art to match (`brick-status/tools/pixel_art.py` draws it into
  `static/art/`): a smiling server for the cluster, or one on fire when it's
  down, and two plugs for wigle-sync, connected while syncing is OK, pulled
  apart and sparking otherwise.
- It asks Prometheus (`https://prometheus.brick.nozdormu.cloud`, through
  brick9000's own proxy to an Ingress defined in `brick-k8s-config`) about the cluster (node
  readiness, workloads short of replicas, crash-looping pods, down scrape
  targets; only a NotReady node or the API being down makes the cluster
  "Down", while an unhealthy workload makes it "Degraded") and wigle-sync (Pushgateway metrics, plus a running CronJob pod
  meaning "uploading"). The wigle-sync view has three rows: how the last sync
  with the Pi went (OK, failed, or waiting for internet), how many files it
  uploaded, and when the next one runs. It only alerts when every run for 2
  hours has had errors; the pwnagotchi being away and syncs pausing are normal.
- **Services:** failed systemd units and timers that stopped firing, on
  every host (`brick_status/units.py`). brick9000's own system and user
  units are read straight from `systemctl`, so they still show when the
  cluster is down; the rest come from Prometheus: node-exporter's systemd
  collector for the other hosts' system units, and for user units (which that
  collector can't see) `brick-units.timer` on brick1982, which every 2
  minutes writes `brick_systemd_unit_problem` to node-exporter's textfile
  directory (`/var/lib/node-exporter-textfile`). A timer is a problem when
  its next run is more than 15 minutes overdue, or it's enabled with nothing
  scheduled; the report itself going 10 minutes stale is one too. A problem
  makes the Services view "Failing" (yellow, like a degraded workload: a
  failed job isn't the cluster down) and shows on the CRT label and the
  lights; it doesn't page. Units known to fail are listed with why in
  `units.EXPECTED` (brick2000's, which are hands off) and show green.
  brick69 isn't covered yet: it isn't a node, and it's often out driving.
- It also checks the internet itself, by opening a TCP connection to three
  public DNS anycast addresses (1.1.1.1, 8.8.8.8, 9.9.9.9); any one answering
  means it's up. An ISP outage is outside our control, so it isn't a failure:
  the header shows an orange "Internet Down", pods stuck in
  `ImagePullBackOff`/`ErrImagePull` (and the workloads they belong to) say
  "internet down", and wigle-sync counts uploads it couldn't make
  as deferred, not failed.
- It serves the board page on `127.0.0.1:8765`, which Chromium shows in
  kiosk mode (`brick9000/labwc-autostart`). The page polls it every second,
  and reloads itself when brick-status restarts, so a deploy updates the
  screen too.
- The same page is on the LAN, read-only, at
  `https://status.brick.nozdormu.cloud`: a plain mirror of what the cabinet shows, including which
  view is up. It changes nothing on the cabinet. Its CRT window stays empty
  apart from its label, since only the kiosk runs cmatrix: requests that come
  through the proxy (it adds `X-Forwarded-For`) can't start one, which would
  stop the kiosk's. While the cabinet is quiet the header says "Display Off"
  and the data keeps updating.
- The overview is a CRT window running a terminal program (below), the three area
  tiles, and a compact vitals strip for brick420 and brick2000 (from
  node_exporter) and brick9000 itself (read from `/proc` and `/sys`, so they
  show when the cluster is down). Each host gets a pie for root disk and LED segment
  bars for CPU, memory and temperature, green, yellow or red by level (CPU and
  memory turn yellow at 85% and red at 95%, temperature at 158°F and 176°F,
  disk at 85% and 95%), and its CPU temperature in °F beside the bar. The header shows a red
  "SSHD Down" if sshd stops on brick9000, and nothing while it's up.
- The CRT window plays real terminal programs in turn, 10 minutes each by the
  clock (`BRICK_STATUS_CRT_PROGRAMS`, `BRICK_STATUS_CRT_MINUTES`): cmatrix,
  pipes, a gallery of public-domain paintings drawn with chafa
  (`brick-status/art/paintings/`, credits in its `CREDITS.md`), cbonsai,
  rain and nyancat (tty-clock is available but not in the default list). The bezel says which. The window is a
  square-ish CRT frame about a quarter of the screen, with the three tiles
  stacked beside it and the vitals below; each program names a font size
  (smaller shows more of programs that draw at a fixed size, like nyancat). Only names in
  `matrix.PROGRAMS` run, and ones that aren't installed are skipped. Each
  runs in a pseudo-terminal sized to the window; its output streams to the
  page as server-sent events and xterm.js draws it with its WebGL renderer
  (the default DOM renderer kept Chromium above a full core on the Pi 4).
- The colour still means health: green when all is well, orange when
  degraded or building, red when failing. cmatrix colours itself (it has no
  orange, so the page's theme repaints its yellow); every other program is
  tinted by the page, which maps all 256 terminal colours to shades of the
  health colour by brightness. Measured on brick9000 at about 140x20 (whole
  machine, ~30% with the board idle): pipes adds about 7 points, cmatrix 1,
  the rest under 1. hollywood was tried and left out: its widgets held the
  Pi at 96% whatever its settings (#28). It all runs only while the overview
  is on screen, and not overnight.
- It reads the front-panel buttons from the `gpio_keys` input device (grabbed,
  so presses don't reach Chromium) and drives the screen with `wlopm` and the
  button lights through the plasma daemon's FIFO, `/tmp/plasma`.

The board has three modes:

| Mode | When | Screen | Button lights |
| --- | --- | --- | --- |
| idle | no press in the last 2 minutes | overview | the most important event (below) |
| active | a button was pressed | the chosen view | each view's button in its area's health colour; the selected one pulses, and so does any area with a build or upload running (green) |
| quiet | the quiet schedule (below) | off | off; only the side button does anything: it turns everything on for 30 minutes |

The six buttons are two rows of three. The top row picks Overview, Builds and
Cluster; the bottom row picks wigle-sync and Services; the last one is free for now. The
joystick left/right steps through the views. The buttons bounce (a release
can be followed by a phantom press 16-120ms later), so presses within 150ms
of the same button's release are ignored. The other cabinet keys (Start,
Coin, joystick up/down) don't change the view but count as input.

The quiet schedule is `BRICK_STATUS_QUIET` in the env file: daily windows
(`00:00-06:00`) or weekly ones (`Mon 00:00-Fri 16:00`), several per entry
separated by `,` (quiet inside any of them), and `;`-separated entries each
optionally starting on a date (`2026-10-09: ...`, from midnight). The latest
entry that has started applies, so a schedule change can be set up ahead of
time. brick9000's is off overnight (00:00-06:00) until 2026-10-09. From then
it's on weekday evenings, 16:00 to midnight, and all weekend from Friday 16:00
to Monday 00:01: off Monday 00:01-16:00 and Tuesday to Friday 00:00-16:00. While it's quiet the cabinet keys do nothing except the side button
(`KEY_ESC`): it turns everything on for 30 minutes
(`BRICK_STATUS_WAKE_SECONDS`), and the header shows "Awake until ..." until
then. Pressed while the board is already on, including during those 30
minutes, it does nothing.

Idle light patterns, highest priority first:

| Pattern | Event |
| --- | --- |
| `nightly` | the show, while Jenkins' nightly `test-all` runs: each button's four LEDs spin a colour wheel, a white-hot comet sweeps the chain trailing the spectrum, fireworks burst on random buttons, and it all breathes (outranks everything, for the few minutes it runs) |
| `alert` | red pulse: anything failing |
| `stage-<kind>` | a running Jenkins build marked by the plasma library: `checkout` a cyan comet streaming along the chain, `install` an amber bar filling, `test` a green scanner swinging end to end, `verify` purple heads racing in from both ends to a white flash, `build` the rainbow, `push` blue waves rushing out, `deploy` the boil; with several, the stage furthest along wins |
| `building` | amber chase: a Jenkins job is running |
| `uploading` | green pulse: wigle-sync is uploading |
| `warn` | slow amber pulse: something degraded |
| `unknown` | grey pulse: no data (e.g. Prometheus unreachable) |
| `calm` | slow green (0 255 0) breathing: all good |

A deploy or an image build on brick9000 itself (below) takes over the lights
in idle and active mode. While brick9000 deploys a new commit, `deploying`
boils the buttons at a rolling boil: each one swells in a random colour on its
own quick beat (a bubble every 0.13-0.33 s) and pops dark, a new colour each
time. An image build alone flashes `image-building`
rainbow. Either way, `image-pushed` then flashes bright green or `image-failed`
pulses red for a minute (`BRICK_STATUS_BUILD_RESULT_SECONDS`). A deploy
outranks an image build, so the buttons boil until the whole deploy is done,
and for at least 10 seconds (`BRICK_STATUS_DEPLOY_MIN_SECONDS`) so a deploy with
nothing to rebuild is still seen; its result follows for the usual minute.

The patterns are PNGs the daemon writes into `/etc/plasma/brick-status/` at
startup (40 pixels wide: 10 button slots of 4 LEDs; one row per frame at
60fps), so changing one needs only a restart, not a reinstall.

### Alerts

The board shows everything, but from 2026-10-09 it's dark all work week. So
brick-status also pages a phone, through [ntfy](https://ntfy.sh), for the few
problems that need a person and won't heal by themselves
(`brick_status/notify.py`). The aim is under one page a month. Everything else
(failed Jenkins jobs, crash-loops, degraded workloads) stays on the board.
Pages come from brick9000, outside the cluster, so they still go out when the
cluster is down.

| Page | After |
| --- | --- |
| Can't reach Prometheus, though the internet is up | 30 min |
| Control plane down (`up{job="apiserver"}`, also a row on the cluster view) | 30 min |
| A node `NotReady` | 30 min |
| wigle-sync failing (its check is red after 2h of failing runs; files pile up on brick69) | 12 h |
| A host's root disk over 85% | 1 h |

Each problem pages once when it has lasted that long and once more ("all
clear") when it's gone. A problem can't clear while its data is missing:
nodes aren't "back" just because the control plane took their readiness
with it. Open problems and unsent pages are kept in
`~/.local/state/brick-status/notify.json`, so a restart doesn't page twice and
a page that couldn't go out (no internet) goes out later. Every problem, paged
or not, is logged to `incidents.jsonl` next to it, for the weekly report.

Ol' Brick, the outfit's old prospector, writes the pages, and they carry the
board's pixel art (PNGs in `static/notify/`, drawn by
`tools/pixel_art.py`; iOS shows PNG attachments, not SVG).

**Privacy.** Pages go through the public ntfy.sh server (and Apple's push
service, to reach an iPhone). There is no end-to-end encryption, and the server
keeps messages for a few hours (the art too, at an unguessable public URL).
So pages hold only brick names, durations and
states: a test fails if any message could contain an IP, URL or `.local`
name. Under Ol' Brick's line, a page carries one line of numbers with fixed
labels, never log text: the host's disk, memory, CPU and temperature for a
full disk; how the other nodes are coping for a node or the control plane
down; the last sync's state for wigle-sync. A line that could leak anything
is dropped rather than sent. `notify --test` shows brick9000's own numbers
that way. The one exception is where tapping a page goes, which ntfy sends
separately from the message (`BRICK_STATUS_NTFY_CLICK`, by default the
board's mirror at `https://status.brick.nozdormu.cloud`). That name is already
in public DNS and only opens on the home network; set it empty to send no
link. The topic name works as the password: anyone who knows it can read and
send pages. `install.sh` makes a long random one in
`~/.config/brick-status/ntfy_topic` (mode 600; never in git or the env file).
To self-host ntfy later, point `BRICK_STATUS_NTFY_URL` at it.

Subscribe on the phone: install the ntfy app, add a subscription to the
topic in that file (server `ntfy.sh`), then on brick9000:

```bash
cd ~/brick-cicd-config/brick-status && python3 -m brick_status notify --test
```

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
moves again. From the moment it finds a new commit until it's done, the
buttons boil (see the lights above); it records its progress in
`~/.local/state/brick-build/deploy.json`. Changes under `brick9000/` itself
(units, autostart) still need `install.sh` rerun; the deploy log says so.

brick9000 follows `main`. To try a branch on the real board before merging,
run the `deploy-brick9000` Jenkins job with `BRANCH=<branch>` (or its
webhook with `&BRANCH=<branch>`): it sets `BRICK_DEPLOY_BRANCH` in the env
file and starts the deploy now instead of waiting for the timer. Run it again
after each push to the branch, and once more with `BRANCH` empty (or the
webhook without it) after merging, to go back to `main`. Deploy logs:
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

It runs brick-status' and brick-retro's unit tests, then the functional
tests in `functional/`, which run the real things against stubs: both deploy
scripts against throwaway git repos (a
passing commit deploys, a failing one is refused and not retried, branches
are followed and left, brick9000's lights and proxy step), and Jenkins'
`start_deploy` with `ssh` and `git ls-remote` faked (no bad branch name reaches
a host; rpi_lcars' deploy for brick1982 too).

After a deploy, `scripts/smoke` checks the live result, read-only: every
`*.brick.nozdormu.cloud` name answers through the proxy, the services on
brick9000 and brick1982 are running, and the board's data is fresh. It exits 1 if anything is down.

## Games (brick-retro, on brick9000)

NES, SNES, Game Boy, Game Boy Color, Game Boy Advance, Genesis, Master System
and PlayStation over the board with a Bluetooth controller (RetroArch with the
Nestopia, Snes9x, Gambatte, mGBA, Genesis Plus GX and PCSX-ReARMed cores).
Every console gets crt-pi, a Raspberry Pi friendly CRT shader (scanlines, a
slight curve; `brick9000/retroarch/shaders`, vendored with its GPL-2.0+
credit), passed by brick-retro with `--set-shader` (`BRICK_RETRO_SHADER=` to
turn it off). PlayStation games render at twice the PS1's resolution
(PCSX-ReARMed's enhanced resolution, `retroarch/core-options.cfg`), scaled
to the full screen. Measured on brick9000 (2026-10-14): ReARMed 6.4-9x full
speed native, 4.5-8x at 2x, 3.4x at 2x with crt-pi in the heaviest games.
SwanStation (3.1-3.9x, software only) and Beetle PSX HW (0.8-1.7x) were
tried: their GPU renderers can't run on the Pi 4 (Vulkan stalls; their
OpenGL ones need more than its OpenGL 3.1, even with a desktop-GL RetroArch
built to test), so neither could upscale.
Debian has no PlayStation core, so `install.sh` builds PCSX-ReARMed from a
pinned commit into `/usr/local/lib/libretro`.

- **Playing:** turn a paired controller on and RetroArch's game list opens
  over the board. Start + Select opens RetroArch's menu; quitting RetroArch
  (Quit, twice), or holding Home for 3 seconds (always works, even when
  RetroArch has lost the pad), shows the board again. With the controller already on, Home
  opens the games.
- **Volume:** hold Home and press the D-pad up or down. Ten notches up to
  `BRICK_RETRO_MAX_VOLUME` (half the DAC's range: the Picade's speaker is very
  loud); it starts at 15% and WirePlumber remembers it. Or from any browser
  at home: `https://volume.brick.nozdormu.cloud`, a slider whose 100% is the
  same cap (`BRICK_STATUS_MAX_VOLUME`, keep it equal), plus a mute button.
  It follows changes made with the pad.
- **Bluetooth drops:** the game pauses, and brick-retro asks the controller to
  reconnect every 30 seconds. Battery saves are written every 10 seconds, and
  a state is saved on quit and loaded on start. After 15 minutes with no
  controller, RetroArch closes so the board isn't hidden behind a dead game.
- **The board:** while RetroArch is open, brick-status keeps the screen on
  whatever the quiet schedule says (brick-retro's flag file). The cabinet's
  own buttons stay brick-status's.
- **ROMs:** `brick-roms.timer` copies each system's folder daily from
  brick2000's `/extra/games/roms` (`brick9000/sync-roms`), plus the BIOS
  files from `/extra/games/BIOS/system` to `~/games/system`, so games work
  when brick2000 or the cluster is down. It then rewrites the playlists and
  imports RetroPie's old `.srm` saves (never over a newer one). A system
  with no games gets no playlist. brick2000 lets the `~/.ssh/brick-roms` key
  only read `/extra/games` (`rrsync -ro`); `install.sh` prints the line to
  authorize. A game in two folders (brick2000's SNES folder has many both
  at the top and in an A-Z subfolder) is one entry.
- **Names:** the list shows clean names ("7th Saga, The (U) [!]" is "7th
  Saga, The"; `games.label()`): region, language, revision and date tags
  and GoodTools' dump marks go; (Beta), (Proto), (Disc 2), a multicart's
  name, translations and hacks stay. Two versions of one game keep their
  region to tell them apart. The files keep their names: RetroArch names
  saves after the file, and sync-roms mirrors brick2000's copies.
- **Thumbnails:** after the playlists, `python3 -m brick_retro thumbnails`
  fetches box art, title screens and screenshots for every game that hasn't
  got them, from libretro's thumbnail server, at about 1 MB/s
  (`BRICK_RETRO_THUMB_RATE`). RetroArch looks thumbnails up by the playlist
  label, and its own updater (Online Updater, Playlist Thumbnails Updater)
  asks the server for exactly that, but the server uses No-Intro names ("7th
  Saga, The (USA)"), so most would miss. `brick_retro/thumbnails.py`
  matches the ROM's file name to the server's names: the same title (tags,
  articles, punctuation and roman numerals aside), then the same title where
  only one side has a subtitle, then without a "Disney's"-style prefix, then
  a near spelling with the same numbers; only ever when exactly one game
  fits. It prefers the game's own region, then USA, and saves each image
  under the label. 1920 of 2089 games match (2026-10-14); the rest are
  unlicensed carts, hacks and odd file names. A game with no match isn't
  asked about again for 30 days (sooner when the matcher changes).
  `brick_retro_thumbnails{system,kind}` counts them.
- **PlayStation:** one entry per game. A `.chd` hides a `.bin`/`.cue` copy of
  the same game, a multi-disc game is one `.m3u` (`~/games/m3u`, so it keeps
  one memory card; swap discs from RetroArch's menu), and `.ecm`/`.7z`
  images stay on brick2000 until someone unpacks them. `psx/PS1/` on
  brick2000 duplicates the `.chd` files beside it and isn't copied.
- **Controller mapping** is code: `brick9000/retroarch/autoconfig/*.cfg`,
  installed by `install.sh`; RetroArch's settings are
  `brick9000/retroarch/retroarch.cfg`. The pairing itself lives in BlueZ
  (`/var/lib/bluetooth`) and survives reboots.

The 8BitDo SF30 Pro is paired in D-input mode: turn it on with **Start + B**
(other modes show up as a different device, with no mapping). Pair a
controller once (hold its Pair button until the lights flash fast):

```bash
bluetoothctl
  scan on        # wait for the controller's name
  pair <MAC>
  trust <MAC>
  connect <MAC>
```

## brick1982 (the touchscreen)

`brick1982` (192.168.1.222, static lease) is a Raspberry Pi 4 8GB with the
official 7" DSI touchscreen (800x480). It left the cluster on 2026-10-14 to
become a wall display. Since 2026-10-15 its screen shows the **LCARS panel**,
Home Assistant's thermostat in LCARS, from its own repo:
[operations-ivy/rpi_lcars](https://github.com/operations-ivy/rpi_lcars),
which carries its kiosk unit, deploy timer and `install.sh` (`deploy/`).
The Jenkins job `deploy-brick1982` deploys that panel from a branch of
rpi_lcars (empty: `master`).

brick-arena, the Chromium kiosk that was here before (pixel planets, the pod
arena, packet weather, and `arena.brick.nozdormu.cloud`), was retired for it
on 2026-10-15; it's in this repo's history.

What stays here is the host's tooling, which `brick1982/install.sh` sets up:
node-exporter on `:9100`, `brick-units.timer` (its failed user units for
brick9000's board), and `brick-deploy.timer`, which keeps the clone at
`~/brick-cicd-config` following `BRICK_DEPLOY_BRANCH` in
`~/.config/brick-cicd-config/env` (`brick1982/deploy`: test in a scratch
worktree, switch only if the tests pass; nothing restarts, since the units run
from the clone). Changes under `brick1982/` need `install.sh` run again.

```bash
git clone https://github.com/operations-ivy/brick-cicd-config.git ~/brick-cicd-config
~/brick-cicd-config/brick1982/install.sh
```

To see what the panel shows from elsewhere:

```bash
ssh zaphod@brick1982 'XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 grim -' > panel.png
```

## Metrics

Everything here that can be measured is, for Prometheus (scrape jobs in
brick-k8s-config's `kube-prometheus-stack-values.yaml`) and the "Brick
boards" Grafana dashboard (`dashboards/`, `kubectl apply -f dashboards/`).
No pip packages on the hosts: `brick_status/metrics.py` writes the text
format, and brick-retro imports it from the same checkout.

| What | Where Prometheus gets it | Metrics |
| --- | --- | --- |
| brick-status | `https://status.brick.nozdormu.cloud/metrics` (through the proxy; it listens on localhost) | `brick_status_*`: each view's and check's state, polls, mode, view, lights, buttons, pages, CRT programs, kiosk browser last seen, requests |
| The LCARS panel (rpi_lcars) | `brick1982:8767/metrics` | `lcars_*`: Home Assistant link and requests, codes entered, relocks, changes sent, taps, screen, backlight, frame time |
| brick-retro | node-exporter's textfile collector on brick9000 | `brick_retro_*`: RetroArch open, controllers, launches, closes, games per system |
| Oneshot user units (`brick-deploy` on both hosts, `lcars-deploy`, `brick-roms`, `brick-mirror`) | textfile collector (`ExecStopPost`: `brick_status/jobs.py`) | `brick_job_*{unit}`: last run, result, duration, last success; the deploys' branch and commit |
| brick9000 itself | node-exporter on `:9100` (install.sh; systemd units too) | `node_*` |
| brick1982 itself | node-exporter on `:9100` (`brick1982/install.sh`; systemd units too) | `node_*` |
| Caddy | `brick9000:9180/metrics` | `caddy_*`, per name |

The textfile directory is `/var/lib/node-exporter-textfile` on both hosts,
like the cluster's DaemonSet. brick1982 has its own node-exporter too (the
same install as brick9000's, `brick1982/install.sh`), scraped as
`brick1982-node` since it left the cluster on 2026-10-14. The board's vitals
strip only uses the cluster's node-exporter
(`job="node-exporter"`): brick9000, brick1982 and brick69 are scraped too,
but aren't nodes.

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
