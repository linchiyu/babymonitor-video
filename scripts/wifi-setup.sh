#!/bin/bash
# Apply the Wi-Fi settings from babymonitor.conf through NetworkManager.
# Runs as root at every boot via babymonitor-wifi.service.
set -u

CONFIG=${BABYMONITOR_CONFIG:-/boot/firmware/babymonitor.conf}
APP_DIR=${APP_DIR:-/opt/babymonitor}
CON=babymonitor-wifi

# Read a key with the server's literal parser - never `source` the file, so passwords
# containing $, backticks or quotes are passed through unchanged.
get() {
  python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from server import load_config; print(load_config(sys.argv[2]).get(sys.argv[3], ""))' \
    "$APP_DIR" "$CONFIG" "$1"
}

SSID=$(get WIFI_SSID)
PASS=$(get WIFI_PASSWORD)
COUNTRY=$(get WIFI_COUNTRY)

if [ -z "$SSID" ]; then
  echo "WIFI_SSID is empty in $CONFIG; leaving Wi-Fi unchanged"
  exit 0
fi

for _ in $(seq 30); do
  nmcli general status >/dev/null 2>&1 && break
  sleep 1
done

if [ -n "$COUNTRY" ] && command -v raspi-config >/dev/null; then
  raspi-config nonint do_wifi_country "$COUNTRY"
fi
rfkill unblock wifi 2>/dev/null || true

if nmcli -t -f NAME connection show | grep -qxF "$CON"; then
  nmcli connection modify "$CON" 802-11-wireless.ssid "$SSID"
else
  nmcli connection add type wifi con-name "$CON" ifname wlan0 ssid "$SSID"
fi
if [ -n "$PASS" ]; then
  nmcli connection modify "$CON" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASS"
else
  nmcli connection modify "$CON" remove 802-11-wireless-security  # open network
fi
nmcli connection modify "$CON" connection.autoconnect yes connection.autoconnect-priority 10

# autoconnect keeps retrying if this fails (e.g. router still booting).
nmcli --wait 15 connection up "$CON" || echo "Could not connect to '$SSID' yet; NetworkManager will keep trying"
