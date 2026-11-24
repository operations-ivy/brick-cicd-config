#!/bin/sh
# Set up lidlesseye (the Pimoroni OctoCam: an original Pi Zero W, ARMv6, and its
# ov5647 camera) from this repo. Run on lidlesseye itself, from the git clone at
# ~/brick-cicd-config: sh ~/brick-cicd-config/lidlesseye/install.sh
#
# - MediaMTX streams the camera (mediamtx.yml): RTSP on :8554, WebRTC on
#   :8889, its metrics on :9998. It only encodes, in hardware; the cluster
#   takes the snapshots (brick-k8s-config's cameras/).
# - node-exporter on :9100, scraped by Prometheus like brick1982.
#
# Safe to rerun: it installs MediaMTX only when the pinned release isn't the
# one installed, and restarts the stream only when something changed.
set -eu
here=$(cd "$(dirname "$0")" && pwd)

# The release, pinned: a newer one is a change here (its checksum from the
# release's checksums.sha256). armv6: the Zero W's ARM1176.
MEDIAMTX_VERSION=v1.21.3
MEDIAMTX_SHA256=cbacd828dd7c8bb67954eee2b0b5590b1f1f6994faa6fcd86a3cf0ba50213095

changed=no

if ! id mediamtx >/dev/null 2>&1; then
    sudo useradd --system --no-create-home --shell /usr/sbin/nologin mediamtx
fi

installed=$(cat /usr/local/lib/mediamtx/VERSION 2>/dev/null || true)
if [ "$installed" != "$MEDIAMTX_VERSION" ]; then
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    tarball="mediamtx_${MEDIAMTX_VERSION}_linux_armv6.tar.gz"
    curl -fsSL -o "$tmp/$tarball" \
        "https://github.com/bluenviron/mediamtx/releases/download/$MEDIAMTX_VERSION/$tarball"
    echo "$MEDIAMTX_SHA256  $tmp/$tarball" | sha256sum -c -
    tar xzf "$tmp/$tarball" -C "$tmp" mediamtx LICENSE
    sudo install -d -m 755 /usr/local/lib/mediamtx
    sudo install -m 755 "$tmp/mediamtx" /usr/local/lib/mediamtx/mediamtx
    sudo install -m 644 "$tmp/LICENSE" /usr/local/lib/mediamtx/LICENSE
    echo "$MEDIAMTX_VERSION" | sudo tee /usr/local/lib/mediamtx/VERSION >/dev/null
    changed=yes
fi

sudo install -d -m 755 /etc/mediamtx
for f in mediamtx.yml; do
    if ! cmp -s "$here/$f" "/etc/mediamtx/$f"; then
        sudo install -m 644 "$here/$f" "/etc/mediamtx/$f"
        changed=yes
    fi
done
if ! cmp -s "$here/mediamtx.service" /etc/systemd/system/mediamtx.service; then
    sudo install -m 644 "$here/mediamtx.service" /etc/systemd/system/mediamtx.service
    sudo systemctl daemon-reload
    changed=yes
fi
sudo systemctl enable mediamtx.service >/dev/null 2>&1
if [ "$changed" = yes ] || ! systemctl is-active -q mediamtx.service; then
    sudo systemctl restart mediamtx.service
fi

# Prometheus scrapes lidlesseye like brick1982 (brick-k8s-config's
# kube-prometheus-stack values): node-exporter on :9100.
if ! dpkg -s prometheus-node-exporter >/dev/null 2>&1; then
    sudo apt-get install -y --no-install-recommends prometheus-node-exporter
fi

echo "lidlesseye: MediaMTX $MEDIAMTX_VERSION $(systemctl is-active mediamtx.service)," \
    "node-exporter $(systemctl is-active prometheus-node-exporter.service)"
