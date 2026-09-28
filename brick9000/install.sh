#!/bin/sh
# Set up brick-status on brick9000. Run on brick9000 itself, from the git
# clone at ~/brick-cicd-config (see the README). After this, brick-deploy.timer
# keeps it up to date from GitHub.
set -eu
here=$(cd "$(dirname "$0")" && pwd)

# Button input; the plasma daemon itself is already installed.
sudo apt-get install -y python3-evdev
# The plasma daemon (root) only loads patterns from /etc/plasma/; brick-status
# (zaphod) writes its own into this subdirectory.
sudo install -d -o "$USER" -g "$USER" /etc/plasma/brick-status

mkdir -p ~/.config/brick-status ~/.config/systemd/user ~/.config/labwc
[ -f ~/.config/brick-status/env ] || cp "$here/brick-status.env.example" ~/.config/brick-status/env
cp "$here/brick-status.service" "$here/brick-deploy.service" "$here/brick-deploy.timer" \
    ~/.config/systemd/user/
cp "$here/labwc-autostart" ~/.config/labwc/autostart

systemctl --user daemon-reload
systemctl --user enable brick-status
systemctl --user restart brick-status
systemctl --user enable --now brick-deploy.timer
echo "brick-status installed. Log out and back in (or reboot) to start the kiosk."
