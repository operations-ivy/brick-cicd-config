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
cp "$here/brick-arena.service" "$here/brick-deploy.service" "$here/brick-deploy.timer" ~/.config/systemd/user/

sudo cp "$here/90-backlight.rules" /etc/udev/rules.d/
sudo udevadm trigger --subsystem-match=backlight --action=add
sudo cp "$here/brick-kiosk.service" /etc/systemd/system/

# User services run from boot with nobody logged in.
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable brick-arena
systemctl --user restart brick-arena
systemctl --user enable --now brick-deploy.timer

sudo systemctl daemon-reload
sudo systemctl disable getty@tty1.service
sudo systemctl enable brick-kiosk.service
sudo systemctl set-default graphical.target
sudo systemctl restart brick-kiosk.service
echo "brick-arena installed; the kiosk is on tty1."
