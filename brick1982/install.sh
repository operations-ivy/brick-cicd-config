#!/bin/sh
# Set up brick1982's host tooling from this repo. Run on brick1982 itself,
# from the git clone at ~/brick-cicd-config (see the README). The panel on its
# screen is the LCARS panel (operations-ivy/rpi_lcars, its own deploy/); this
# is what stays here: node-exporter, brick-units' report of failed user units,
# and brick-deploy.timer, which keeps this clone up to date from GitHub
# (brick-units and the deploys' metrics run from it).
set -eu
here=$(cd "$(dirname "$0")" && pwd)

mkdir -p ~/.config/brick-cicd-config ~/.config/systemd/user
# The branch this clone follows. It used to live in brick-arena's env file.
if [ ! -f ~/.config/brick-cicd-config/env ]; then
    branch=$(sed -n 's/^BRICK_DEPLOY_BRANCH=//p' ~/.config/brick-arena/env 2>/dev/null | tail -n 1)
    printf 'BRICK_DEPLOY_BRANCH=%s\n' "${branch:-main}" >~/.config/brick-cicd-config/env
fi
cp "$here/brick-deploy.service" "$here/brick-deploy.timer" \
    "$here/brick-units.service" "$here/brick-units.timer" ~/.config/systemd/user/

# Prometheus scrapes brick1982 like brick9000 (not a cluster node since
# 2026-10-14): node-exporter on :9100 with the cluster DaemonSet's flags,
# reading brick-units' report from the textfile directory.
sudo apt-get install -y --no-install-recommends prometheus-node-exporter
sudo install -d -o "$USER" -g "$USER" -m 755 /var/lib/node-exporter-textfile
# No comments after a value in this file, and no backslashes (systemd
# EnvironmentFile): [.] for a literal dot.
node_args='ARGS="--collector.textfile.directory=/var/lib/node-exporter-textfile --collector.systemd --collector.systemd.enable-restarts-metrics --collector.systemd.unit-include=.+[.](service|timer) --collector.wifi"'
if [ "$(cat /etc/default/prometheus-node-exporter 2>/dev/null)" != "$node_args" ]; then
    printf '%s\n' "$node_args" | sudo tee /etc/default/prometheus-node-exporter >/dev/null
    sudo systemctl restart prometheus-node-exporter
fi
sudo systemctl enable --now prometheus-node-exporter

# brick-arena and its Chromium kiosk, retired 2026-10-15 for the LCARS panel:
# gone if they're still here. Chromium stays installed (harmless; remove by
# hand if wanted).
systemctl --user disable --now brick-arena.service 2>/dev/null || true
rm -f ~/.config/systemd/user/brick-arena.service
rm -rf ~/.config/brick-arena ~/.local/share/brick-arena
if [ -f /etc/systemd/system/brick-kiosk.service ]; then
    sudo systemctl disable --now brick-kiosk.service || true
    sudo rm -f /etc/systemd/system/brick-kiosk.service
    sudo systemctl daemon-reload
fi
sudo rm -rf /usr/local/share/brick-arena

# User services run from boot with nobody logged in.
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now brick-deploy.timer brick-units.timer
echo "brick1982 host tooling installed (the panel: ~/rpi_lcars/deploy/install.sh)."
