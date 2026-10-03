# babymonitor-video

Live video from a Raspberry Pi camera in any browser on your local network. The camera only runs while someone has the page open.

## Setup

1. **Flash the SD card** with Raspberry Pi OS (Bookworm or newer, Lite is fine) using Raspberry Pi Imager. In *OS customisation*, set a hostname (e.g. `babymonitor`), enable SSH, and enter your Wi‑Fi. The Pi needs network for this first install.
2. **Connect the camera** to the Pi's camera port and boot the Pi.
3. **Install** (over SSH):
   ```bash
   sudo apt-get install -y git
   git clone <this-repo-url> babymonitor-video
   cd babymonitor-video
   sudo ./scripts/install.sh
   ```
4. **Open** `http://babymonitor.local/` (or `http://<pi-ip>/`) from a phone or computer on the same network.

Everything starts automatically on every boot.

## Wi‑Fi

Wi‑Fi comes from `babymonitor.conf` on the SD card's boot partition (`/boot/firmware/babymonitor.conf` on the Pi). You can edit it from any computer by plugging in the SD card:

```ini
WIFI_SSID=My Home Wi-Fi
WIFI_PASSWORD=my password
WIFI_COUNTRY=US
```

Reboot the Pi afterwards. It joins that network on every boot. Values are read literally, so special characters in passwords are fine. Leave `WIFI_SSID` empty to keep whatever Wi‑Fi is already set up.

The same file also sets `PORT`, `WIDTH`, `HEIGHT`, and `MAX_VIEWERS`.

## Troubleshooting

- **No picture:** run `rpicam-hello --list-cameras`. If it lists no camera, check the ribbon cable orientation.
- **Logs:** `journalctl -u babymonitor -f`. You'll see `camera started` when a viewer connects and `camera stopped` a few seconds after the last one closes the page.
- **Wi‑Fi:** `journalctl -u babymonitor-wifi` and `nmcli connection show --active`.

## Security

There is no password on the stream. Anyone on your local network who knows the address can watch. Do not expose port 80 to the internet.

## Development

```bash
python3 -m unittest discover -s babymonitor -v   # uses a fake camera, no Pi needed
```
