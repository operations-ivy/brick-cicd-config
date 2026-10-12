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
    "$here/brick-retro.service" "$here/brick-roms.service" "$here/brick-roms.timer" \
    ~/.config/systemd/user/
cp "$here/labwc-autostart" ~/.config/labwc/autostart

# brick-retro: NES and SNES over the board with a Bluetooth controller
# (README, "Games").
sudo apt-get install -y retroarch libretro-nestopia libretro-snes9x rsync
# Raspberry Pi OS starts every radio blocked (rfkill default_state=0) and
# NetworkManager only unblocks Wi-Fi; systemd-rfkill keeps this across reboots.
for r in /sys/class/rfkill/rfkill*; do
    [ "$(cat "$r/type")" = bluetooth ] && echo 0 | sudo tee "$r/soft" >/dev/null
done
# A paired controller reconnects faster.
if ! grep -q '^FastConnectable *= *true' /etc/bluetooth/main.conf; then
    sudo sed -i 's/^#*FastConnectable *=.*/FastConnectable = true/' /etc/bluetooth/main.conf
    sudo systemctl restart bluetooth
fi
mkdir -p ~/games/roms ~/games/saves ~/games/states ~/.config/retroarch/autoconfig ~/.config/brick-retro
for f in "$here"/retroarch/autoconfig/*.cfg; do
    [ -f "$f" ] && cp "$f" ~/.config/retroarch/autoconfig/
done
[ -f ~/.config/brick-retro/env ] || cp "$here/brick-retro.env.example" ~/.config/brick-retro/env
# The Picade's speaker is very loud: start quiet, once (WirePlumber keeps
# whatever Home + D-pad sets after that).
volume_set=~/.local/state/brick-retro/volume-set
if [ ! -f "$volume_set" ]; then
    wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.15 && mkdir -p "$(dirname "$volume_set")" && touch "$volume_set"
fi
# The key brick-roms copies ROMs with. brick2000 must let it read only the ROMs.
[ -f ~/.ssh/brick-roms ] || ssh-keygen -q -t ed25519 -N '' -C brick9000-roms -f ~/.ssh/brick-roms
roms_host=$(sed -n 's/^BRICK_ROMS_SOURCE=.*@//p' ~/.config/brick-retro/env | tail -n 1)
roms_host=${roms_host:-brick2000}
ssh-keygen -F "$roms_host" >/dev/null || ssh-keyscan -t ed25519 "$roms_host" >>~/.ssh/known_hosts 2>/dev/null
if ! ssh -i ~/.ssh/brick-roms -o BatchMode=yes "zaphod@$roms_host" true 2>/dev/null; then
    echo "On $roms_host, add this line to ~/.ssh/authorized_keys, then run: systemctl --user start brick-roms"
    echo "  command=\"rrsync -ro /extra/games/roms\",restrict $(cat ~/.ssh/brick-roms.pub)"
fi

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
systemctl --user enable --now brick-deploy.timer brick-mirror.timer brick-roms.timer
systemctl --user enable brick-retro
systemctl --user restart brick-retro
echo "brick-status installed. Log out and back in (or reboot) to start the kiosk."
