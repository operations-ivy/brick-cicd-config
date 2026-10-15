#!/bin/sh
# Set up brick-arena and the kiosk on brick1982. Run on brick1982 itself, from
# the git clone at ~/brick-cicd-config (see the README). After this,
# brick-deploy.timer keeps it up to date from GitHub.
set -eu
here=$(cd "$(dirname "$0")" && pwd)

# cage: a single-window Wayland compositor, so no desktop is needed on this
# Lite install. grim takes screenshots of the panel for checking it remotely.
sudo apt-get install -y --no-install-recommends cage chromium grim fonts-dejavu-core

mkdir -p ~/.config/brick-arena ~/.config/systemd/user ~/.local/share/brick-arena
[ -f ~/.config/brick-arena/env ] || cp "$here/brick-arena.env.example" ~/.config/brick-arena/env
cp "$here/brick-arena.service" "$here/brick-deploy.service" "$here/brick-deploy.timer" \
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

sudo cp "$here/90-backlight.rules" /etc/udev/rules.d/
sudo udevadm trigger --subsystem-match=backlight --action=add
sudo cp "$here/brick-kiosk.service" /etc/systemd/system/
sudo python3 "$here/blank-cursors" /usr/local/share/brick-arena/cursors/default

# User services run from boot with nobody logged in.
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable brick-arena
systemctl --user restart brick-arena
systemctl --user enable --now brick-deploy.timer brick-units.timer

sudo systemctl daemon-reload
sudo systemctl disable getty@tty1.service
sudo systemctl enable brick-kiosk.service
sudo systemctl set-default graphical.target
sudo systemctl restart brick-kiosk.service
echo "brick-arena installed; the kiosk is on tty1."
