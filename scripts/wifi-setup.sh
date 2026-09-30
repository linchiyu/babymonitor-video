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
  BABYMONITOR_CONFIG=$CONFIG python3 "$APP_DIR/server.py" --get "$1"
}

SSID=$(get WIFI_SSID) || { echo "Could not read $CONFIG" >&2; exit 1; }
PASS=$(get WIFI_PASSWORD) || exit 1
COUNTRY=$(get WIFI_COUNTRY) || exit 1

if [ -z "$SSID" ]; then
  echo "WIFI_SSID is empty in $CONFIG; leaving Wi-Fi unchanged"
  exit 0
fi

for _ in $(seq 30); do
  nmcli general status >/dev/null 2>&1 && break
  sleep 1
done

# Only set the country when it changes: raspi-config rewrites the boot partition to do it.
if [ -n "$COUNTRY" ] && command -v raspi-config >/dev/null &&
  [ "$(raspi-config nonint get_wifi_country 2>/dev/null)" != "$COUNTRY" ]; then
  raspi-config nonint do_wifi_country "$COUNTRY"
fi
rfkill unblock wifi 2>/dev/null || true

fail() { echo "Failed to configure Wi-Fi '$SSID': $1" >&2; exit 1; }
if nmcli -t -f NAME connection show | grep -qxF "$CON"; then
  nmcli connection modify "$CON" 802-11-wireless.ssid "$SSID" || fail "set SSID"
else
  nmcli connection add type wifi con-name "$CON" ifname wlan0 ssid "$SSID" || fail "create connection"
fi
if [ -n "$PASS" ]; then
  # nmcli rejects PSKs outside 8-63 characters.
  nmcli connection modify "$CON" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASS" || fail "set password"
else
  nmcli connection modify "$CON" remove 802-11-wireless-security || fail "set open network"
fi
nmcli connection modify "$CON" connection.autoconnect yes connection.autoconnect-priority 10 || fail "enable autoconnect"

# autoconnect keeps retrying if this fails (e.g. router still booting).
nmcli --wait 15 connection up "$CON" || echo "Could not connect to '$SSID' yet; NetworkManager will keep trying"
