# The volume dial

An [M5Stack Dial v1.1](https://shop.m5stack.com/products/m5stack-dial-v1-1)
parked next to brick9000. It has an ESP32-S3, a 1.28" round 240x240 screen and
a rotary encoder around it. Turning it sets brick9000's speaker volume, one
click is 5% (20 clicks from silent to the cap), and pressing the screen mutes.
It follows changes made anywhere else (the pad, the web page) within about 2
seconds and dims after a minute untouched. It's drawn in the board's palette
and typefaces.

It talks only to brick-status's `/api/volume` through
`https://volume.brick.nozdormu.cloud` (the same API as the web page), so it
needs brick9000 and the router's DNS entry, but no cluster and no Home
Assistant. The screen shows `OFFLINE` in red when it can't reach brick9000,
and `NO SIGNAL` before it has ever heard from it.

- `dial.yaml`: the ESPHome config.
- `secrets.yaml`: the Wi-Fi network and password, and the OTA password.
  Gitignored. Copy `secrets.example.yaml` to it and fill it in.
- `esphome`: runs the pinned ESPHome image in Docker, so nothing needs
  installing.

## First flash (USB)

1. Plug the Dial into the laptop with a USB-C **data** cable. It shows up as
   `/dev/ttyACM0`. If it doesn't, hold the button under the screen while
   plugging it in, to force download mode. The laptop user may need to be in
   the `dialout` group.
2. `volume-dial/esphome run dial.yaml --device /dev/ttyACM0`
3. Unplug it and power it from a USB-C wall charger (not brick9000's USB
   ports: they share ~1.2A).

## Updates (Wi-Fi)

`volume-dial/esphome run dial.yaml --device <its address>` builds and
flashes over the air with the OTA password. `logs` instead of `run` shows its
log.
