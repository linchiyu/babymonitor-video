#!/bin/bash
# Install the baby monitor on Raspberry Pi OS (Bookworm or newer).
# Usage: sudo ./scripts/install.sh    (safe to re-run; never overwrites your config)
#        sudo RESET_CONFIG=1 ./scripts/install.sh    (replace config with the example; old one kept as .bak)
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo: sudo $0"; exit 1; }

BOOT=/boot/firmware
[ -d "$BOOT" ] || { echo "$BOOT not found: Raspberry Pi OS Bookworm or newer is required"; exit 1; }

REPO=$(cd "$(dirname "$0")/.." && pwd)
APP=/opt/babymonitor
CONFIG=$BOOT/babymonitor.conf

apt-get update
apt-get install -y --no-install-recommends python3-picamera2

id babymonitor >/dev/null 2>&1 ||
  useradd --system --no-create-home --shell /usr/sbin/nologin --groups video babymonitor

install -d "$APP"
install -m 644 "$REPO/babymonitor/server.py" "$APP/server.py"
install -m 755 "$REPO/scripts/wifi-setup.sh" "$APP/wifi-setup.sh"
if [ "${RESET_CONFIG:-}" = 1 ] && [ -f "$CONFIG" ]; then
  mv "$CONFIG" "$CONFIG.bak"
  echo "Moved the old config to $CONFIG.bak (copy your Wi-Fi settings back from it)."
fi
if [ ! -f "$CONFIG" ]; then
  cp "$REPO/config/babymonitor.conf.example" "$CONFIG"
  echo "Created $CONFIG - set WIFI_SSID and WIFI_PASSWORD there."
fi

install -m 644 "$REPO/systemd/babymonitor.service" "$REPO/systemd/babymonitor-wifi.service" /etc/systemd/system/
# Hardware watchdog: reboots the Pi if the kernel or systemd itself hangs (15 s is the Pi's maximum).
install -d /etc/systemd/system.conf.d
printf '[Manager]\nRuntimeWatchdogSec=15\n' > /etc/systemd/system.conf.d/babymonitor-watchdog.conf
systemctl daemon-reexec
systemctl daemon-reload
# Wi-Fi applies on the next boot: switching networks now could drop this SSH session mid-install.
systemctl enable babymonitor-wifi.service
systemctl enable babymonitor.service
systemctl restart babymonitor.service

PORT=$(BABYMONITOR_CONFIG=$CONFIG python3 "$APP/server.py" --get PORT)
cat <<EOF

Installed. The stream is running now and starts automatically on every boot.
  Open:  http://$(hostname).local:$PORT/   or   http://$(hostname -I | awk '{print $1}'):$PORT/
  Wi-Fi: edit $CONFIG, then reboot (sudo reboot).
  Logs:  journalctl -u babymonitor -f
EOF
