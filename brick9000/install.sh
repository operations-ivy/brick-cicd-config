#!/bin/sh
# Set up brick-status on brick9000. Run on brick9000 itself, from the git
# clone at ~/brick-cicd-config (see the README). After this, brick-deploy.timer
# keeps it up to date from GitHub.
set -eu
here=$(cd "$(dirname "$0")" && pwd)

# Button input (the plasma daemon itself is already installed), and
# avahi-publish to announce brick9000's old names (jenkins.local,
# brick-status.local), which the proxy redirects to the new ones.
sudo apt-get install -y python3-evdev avahi-utils
# The plasma daemon (root) only loads patterns from /etc/plasma/; brick-status
# (zaphod) writes its own into this subdirectory.
sudo install -d -o "$USER" -g "$USER" /etc/plasma/brick-status

mkdir -p ~/.config/brick-status ~/.config/systemd/user ~/.config/labwc
[ -f ~/.config/brick-status/env ] || cp "$here/brick-status.env.example" ~/.config/brick-status/env
# The ntfy topic phone pages go to: anyone who knows it can read and send
# pages, so it's long, random and never printed. Kept if it exists.
topic=~/.config/brick-status/ntfy_topic
[ -s "$topic" ] || (umask 077 && python3 -c 'import secrets; print("brick-" + secrets.token_urlsafe(30))' >"$topic")
cp "$here/brick-status.service" "$here/brick-deploy.service" "$here/brick-deploy.timer" \
    "$here/brick-mirror.service" "$here/brick-mirror.timer" "$here/mdns-alias@.service" \
    ~/.config/systemd/user/
cp "$here/labwc-autostart" ~/.config/labwc/autostart

# Jenkins moved to the cluster (jenkins/, README "Jenkins"). Retire the
# container it used to run in here; its home and secrets stay, as backups.
if docker container inspect brick-jenkins >/dev/null 2>&1; then
    docker rm -f brick-jenkins
    docker image rm brick-jenkins:local || true
fi
if [ -f ~/.config/brick-status/env ] && ! grep -q '^BRICK_STATUS_JENKINS_PASSWORD=' ~/.config/brick-status/env; then
    echo "Add BRICK_STATUS_JENKINS_PASSWORD (the brick-jenkins Secret's brick-status-password) to ~/.config/brick-status/env"
fi
# Ports 80 and 443: the apps at <name>.brick.nozdormu.cloud, and redirects
# from the old names (README, "Ports 80 and 443").
"$here/proxy/up"

systemctl --user daemon-reload
systemctl --user enable --now mdns-alias@jenkins mdns-alias@brick-status
systemctl --user enable brick-status
systemctl --user restart brick-status
systemctl --user enable --now brick-deploy.timer brick-mirror.timer
echo "brick-status installed. Log out and back in (or reboot) to start the kiosk."
