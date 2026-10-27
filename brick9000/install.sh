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
# The overview's CRT window plays these in turn (README, "brick-status").
sudo apt-get install -y cmatrix pipes-sh cbonsai tty-clock nyancat bsdgames chafa
# RetroArch's menu uses the board's typeface (retroarch/retroarch.cfg).
sudo apt-get install -y --no-install-recommends fonts-ibm-plex
# Prometheus scrapes brick9000 like a node (README, "Metrics"): node-exporter
# on :9100 with the same flags as the cluster's DaemonSet (systemd units, Wi-Fi, and
# a textfile directory for brick-retro and the oneshot units' runs). Scraping
# brick9000 never makes it depend on the cluster.
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

# The plasma daemon (root) only loads patterns from /etc/plasma/; brick-status
# (zaphod) writes its own into this subdirectory.
sudo install -d -o "$USER" -g "$USER" /etc/plasma/brick-status

mkdir -p ~/.config/brick-status ~/.config/systemd/user ~/.config/labwc
[ -f ~/.config/brick-status/env ] || cp "$here/brick-status.env.example" ~/.config/brick-status/env
# The ntfy topic phone pages go to: anyone who knows it can read and send
# pages, so it's long, random and never printed. Kept if it exists.
# Not while pages are paused (pages-paused, README "Alerts"): a new topic
# would quietly turn them back on.
topic=~/.config/brick-status/ntfy_topic
[ -s "$topic" ] || [ -e ~/.config/brick-status/pages-paused ] ||
    (umask 077 && python3 -c 'import secrets; print("brick-" + secrets.token_urlsafe(30))' >"$topic")
cp "$here/brick-status.service" "$here/brick-deploy.service" "$here/brick-deploy.timer" \
    "$here/brick-mirror.service" "$here/brick-mirror.timer" "$here/mdns-alias@.service" \
    "$here/brick-retro.service" "$here/brick-roms.service" "$here/brick-roms.timer" \
    ~/.config/systemd/user/
cp "$here/labwc-autostart" ~/.config/labwc/autostart
# A full-screen game handed straight to the display (direct scanout) makes
# the vc4 HDMI output re-sync every few seconds: black screen, the panel's
# HDMI badge. Always composite instead (labwc reads this at login).
grep -q '^WLR_SCENE_DISABLE_DIRECT_SCANOUT=' ~/.config/labwc/environment 2>/dev/null ||
    echo 'WLR_SCENE_DISABLE_DIRECT_SCANOUT=1' >>~/.config/labwc/environment

# brick-retro: retro games over the board with a Bluetooth controller
# (README, "Games").
sudo apt-get install -y retroarch libretro-nestopia libretro-snes9x libretro-gambatte libretro-mgba \
    libretro-genesisplusgx rsync build-essential git cmake libgl-dev libegl-dev libgles-dev
# Debian packages no PlayStation, 32X, PSP, Dreamcast or Neo Geo core: build
# each from a pinned commit into /usr/local/lib/libretro, once per commit, on
# two cores (brick9000 has no fan: three held it at 77 °C, 2026-10-16)
# (the commit is kept beside the core). games.py names the paths.
# core <name> <repo> <commit> <file the build makes, under its tree> <build command...>
core() {
    name=$1 repo=$2 commit=$3 built=$4
    shift 4
    target=/usr/local/lib/libretro/${name}_libretro.so
    [ "$(cat "$target.commit" 2>/dev/null)" = "$commit" ] && return
    echo "install.sh: building the $name core (see the README for how long)"
    build=$(mktemp -d)
    git -C "$build" init -q
    git -C "$build" fetch -q --depth 1 "$repo" "$commit"
    git -C "$build" checkout -q FETCH_HEAD
    git -C "$build" submodule -q update --init --recursive --depth 1
    (cd "$build" && nice "$@") >"$build.log" 2>&1 || { tail -n 20 "$build.log"; exit 1; }
    sudo install -D -m 644 "$build/$built" "$target"
    # PPSSPP's fonts and UI art, which its core reads from the system directory.
    [ "$name" = ppsspp ] && rsync -a --delete "$build/assets/" "$HOME/games/system/PPSSPP/"
    [ "$name" = fbneo_neogeo ] &&
        sudo install -m 644 "$build/dats/FinalBurn Neo (ClrMame Pro XML, Neogeo only).dat" \
            /usr/local/lib/libretro/fbneo_neogeo.dat
    echo "$commit" | sudo tee "$target.commit" >/dev/null
    rm -rf "$build" "$build.log"
}
cmake_core='cmake -S . -B build -DLIBRETRO=ON -DCMAKE_BUILD_TYPE=Release "$@" && cmake --build build -j2'
core pcsx_rearmed https://github.com/libretro/pcsx_rearmed c8816799b50388e61cfe237fe2cdbb7d8175f20a \
    pcsx_rearmed_libretro.so make -f Makefile.libretro platform=rpi4_64 -j3
core picodrive https://github.com/libretro/picodrive 1890c2932234c9d30f4cd3851d02228baae8f09e \
    picodrive_libretro.so make -f Makefile.libretro platform=aarch64 -j2
# PPSSPP and Flycast for OpenGL ES: Debian's arm64 RetroArch is built against
# GLES, and a core built for desktop GL can't get a context from it.
core ppsspp https://github.com/hrydgard/ppsspp fa50bb1976065c4f8b1b47af227d367fe9771555 \
    build/lib/ppsspp_libretro.so sh -c "$cmake_core" - -DUSING_GLES2=ON
core flycast https://github.com/flyinghead/flycast 5aa091fde632fb332c8d8c34e280d62dc951954c \
    build/flycast_libretro.so sh -c "$cmake_core" - -DUSE_VULKAN=OFF -DUSE_GLES=ON
# FinalBurn Neo's Neo Geo-only build (REGEN_HEADERS: the checked-in driver list
# names every arcade game, so the link fails without it), and its list of the
# games (games.py).
core fbneo_neogeo https://github.com/libretro/FBNeo 68f0d3e47587f700c221392a0d565e2f9bfd728e \
    src/burner/libretro/fbneo_neogeo_libretro.so make -C src/burner/libretro SUBSET=neogeo REGEN_HEADERS=1 platform=rpi4_64 -j2
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
mkdir -p ~/games/roms ~/games/system ~/games/saves ~/games/states ~/.config/retroarch/autoconfig ~/.config/brick-retro
# Core options brick-retro's RetroArch uses (retroarch.cfg points at this copy).
cp "$here/retroarch/core-options.cfg" ~/.config/retroarch/brick-core-options.cfg
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
# The key brick-roms copies ROMs and BIOS files with. brick2000 must let it
# read only /extra/games.
[ -f ~/.ssh/brick-roms ] || ssh-keygen -q -t ed25519 -N '' -C brick9000-roms -f ~/.ssh/brick-roms
roms_host=$(sed -n 's/^BRICK_ROMS_SOURCE=.*@//p' ~/.config/brick-retro/env | tail -n 1)
roms_host=${roms_host:-brick2000}
ssh-keygen -F "$roms_host" >/dev/null || ssh-keyscan -t ed25519 "$roms_host" >>~/.ssh/known_hosts 2>/dev/null
# Listing BIOS/ also catches the older line, which only allowed roms/.
if ! rsync --list-only -e "ssh -i $HOME/.ssh/brick-roms -o BatchMode=yes" "zaphod@$roms_host:BIOS/" >/dev/null 2>&1; then
    echo "On $roms_host, put this line in ~/.ssh/authorized_keys (replacing any brick9000-roms line), then run: systemctl --user start brick-roms"
    echo "  command=\"rrsync -ro /extra/games\",restrict $(cat ~/.ssh/brick-roms.pub)"
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
# Ports 80 and 443 aren't brick9000's any more: the front door runs on the
# cluster since brick9000's hardware failed (2026-10-17). Make sure the old
# Caddy is gone (README, "Ports 80 and 443").
"$here/proxy/retire"

systemctl --user daemon-reload
systemctl --user enable --now mdns-alias@jenkins mdns-alias@brick-status
systemctl --user enable brick-status
systemctl --user restart brick-status
systemctl --user enable --now brick-deploy.timer brick-mirror.timer brick-roms.timer
systemctl --user enable brick-retro
systemctl --user restart brick-retro
echo "brick-status installed. Log out and back in (or reboot) to start the kiosk."
