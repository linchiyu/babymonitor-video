#!/usr/bin/env python3
"""Baby monitor: MJPEG stream from the Raspberry Pi camera, running only while watched."""
import io
import logging
import os
import socketserver
import sys
import threading
import time
from http import server
from urllib.parse import urlsplit

log = logging.getLogger("babymonitor")

DEFAULT_CONFIG = "/boot/firmware/babymonitor.conf"
DEFAULTS = {"PORT": "80", "WIDTH": "1280", "HEIGHT": "720", "MAX_VIEWERS": "5"}
FRAME_TIMEOUT = 5  # seconds without a frame before a viewer is dropped
WRITE_TIMEOUT = 10  # seconds a stalled/dead client may block a write

PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Baby Monitor</title>
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}
img{position:absolute;top:50%;left:50%;width:100vw;height:100vh;object-fit:contain;
transform:translate(-50%,-50%) rotate(var(--r,0deg)) scaleX(var(--f,1))}
img.side{width:100vh;height:100vw}
#c{position:fixed;right:12px;bottom:12px;display:flex;gap:8px}
button{font:20px sans-serif;padding:10px 14px;border:0;border-radius:8px;background:#fff3;color:#fff}
label{display:flex;align-items:center;gap:6px;font:14px sans-serif;color:#fff;padding:0 10px;border-radius:8px;background:#fff3}
#m{position:fixed;inset:0;pointer-events:none;box-shadow:inset 0 0 0 10px red;opacity:0;transition:opacity .3s}
#m.on{opacity:1}
#t{position:fixed;left:12px;top:12px;font:16px/1.4 sans-serif;color:#fff;text-shadow:0 0 4px #000}</style></head>
<body><img id="v" src="/stream.mjpg" alt="Live camera"><div id="m"></div><div id="t"></div>
<div id="c"><button id="flip" aria-label="Flip">&#8646;</button><button id="rot" aria-label="Rotate 90 degrees">&#8635;</button>
<label>Motion <input id="sens" type="range" min="1" max="10" value="5" aria-label="Motion sensitivity"></label></div>
<script>
// Reconnect if the stream drops (Pi rebooted, Wi-Fi blip, camera restart).
// ponytail: relies on the browser firing onerror; a stream that ends cleanly may just freeze.
const v = document.getElementById("v");
v.onerror = () => setTimeout(() => { v.src = "/stream.mjpg?" + Date.now(); }, 2000);
// Flip/rotate are per-device view settings, remembered in this browser.
let r = 90, f = 1;  // default: camera is mounted sideways
try { r = +(localStorage.r ?? 90); f = +localStorage.f || 1; } catch (e) {}
function apply() {
  v.style.setProperty("--r", r + "deg");
  v.style.setProperty("--f", f);
  v.classList.toggle("side", r % 180 !== 0);
  try { localStorage.r = r; localStorage.f = f; } catch (e) {}
}
document.getElementById("flip").onclick = () => { f = -f; apply(); };
document.getElementById("rot").onclick = () => { r = (r + 90) % 360; apply(); };
apply();
// Clock and time since the Pi booted (it is switched on when the baby falls asleep).
const t = document.getElementById("t");
let bootAt = null;
const pad = n => String(n).padStart(2, "0");
function syncUptime() {
  fetch("/uptime").then(r => r.text()).then(s => { bootAt = Date.now() - parseFloat(s) * 1000; }).catch(() => {});
}
setInterval(() => {
  let up = "";
  if (bootAt !== null) {
    const s = Math.floor((Date.now() - bootAt) / 1000);
    up = "<br>Asleep " + Math.floor(s / 3600) + ":" + pad(Math.floor(s / 60) % 60) + ":" + pad(s % 60);
  }
  t.innerHTML = new Date().toLocaleString() + up;
}, 1000);
syncUptime();
setInterval(syncUptime, 60000);  // picks up a Pi reboot
// Motion: compare a small grayscale copy of each frame against a slowly updated background.
// A pixel counts as moving only if it beats the frame's own noise (90th-percentile change) in two
// checks in a row (JPEG/colour flicker hits random pixels each frame, a real movement stays put)
const MIN_DIFF = 8, NOISE_X = 2, BG_RATE = 0.2, HOLD_MS = 2000;
const cv = document.createElement("canvas"), cx = cv.getContext("2d", { willReadFrequently: true });
const m = document.getElementById("m"), sens = document.getElementById("sens");
cv.width = 80; cv.height = 60;  // small on purpose: averaging blurs away JPEG block noise
let bg = null, wasMoving = null, warmup = 8, lastMotion = 0;  // 8 checks = 2 s to learn the scene
try { sens.value = localStorage.sens ?? sens.value; } catch (e) {}
sens.oninput = () => { try { localStorage.sens = sens.value; } catch (e) {} };
// Share of the picture that must move: 2% at sensitivity 1 down to ~0.02% (one pixel) at 10.
const movedShare = () => 0.02 * Math.pow(0.6, sens.value - 1);
function motion(gray, share) {
  if (!bg) { bg = Float32Array.from(gray); wasMoving = new Uint8Array(gray.length); return false; }
  const diff = new Float32Array(gray.length);
  for (let i = 0; i < gray.length; i++) { diff[i] = Math.abs(gray[i] - bg[i]); bg[i] += (gray[i] - bg[i]) * BG_RATE; }
  const limit = Math.max(MIN_DIFF, NOISE_X * diff.slice().sort()[Math.floor(diff.length * 0.9)]);
  const hit = new Uint8Array(diff.length);
  for (let i = 0; i < diff.length; i++) {
    const now = diff[i] > limit ? 1 : 0;
    hit[i] = now & wasMoving[i];
    wasMoving[i] = now;
  }
  // ...and has a moving neighbour: a real movement is a patch, flicker is lone pixels.
  const W = cv.width;
  let moved = 0;
  for (let i = 0; i < hit.length; i++)
    if (hit[i] && (hit[i - 1] || hit[i + 1] || hit[i - W] || hit[i + W])) moved++;
  if (warmup > 0) { warmup--; return false; }
  return moved > diff.length * share;
}
setInterval(() => {
  if (!v.complete || !v.naturalWidth) return;
  cx.drawImage(v, 0, 0, cv.width, cv.height);
  const d = cx.getImageData(0, 0, cv.width, cv.height).data, gray = new Float32Array(d.length / 4);
  for (let i = 0; i < gray.length; i++) gray[i] = (d[i * 4] + d[i * 4 + 1] + d[i * 4 + 2]) / 3;
  if (motion(gray, movedShare())) lastMotion = Date.now();
  m.classList.toggle("on", Date.now() - lastMotion < HOLD_MS);
}, 250);
</script></body></html>
"""

def load_config(path):
    """Parse KEY=value lines literally: no shell expansion, one layer of quotes stripped."""
    cfg = dict(DEFAULTS)
    try:
        f = open(path, encoding="utf-8-sig")  # tolerate a BOM from Windows editors
    except FileNotFoundError:
        return cfg
    with f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            cfg[key.strip()] = value
    return cfg


class StreamingOutput(io.BufferedIOBase):
    """Latest JPEG frame, plus a viewer ref-count that starts/stops the camera."""

    def __init__(self, camera, max_viewers):
        self.camera = camera
        self.max_viewers = max_viewers
        self.frame = None
        self.condition = threading.Condition()
        # Separate from `condition`: camera.stop() joins encoder threads blocked in write().
        self.viewer_lock = threading.Lock()
        self.viewers = 0

    def write(self, buf):
        with self.condition:
            self.frame = bytes(buf)
            self.condition.notify_all()
        return len(buf)

    def wait_frame(self, timeout):
        with self.condition:
            return self.frame if self.condition.wait(timeout) else None

    def add_viewer(self):
        """Register a viewer; False if over the cap or the camera would not start."""
        with self.viewer_lock:
            if self.viewers >= self.max_viewers:
                return False
            if self.viewers == 0:
                try:
                    self.camera.start(self)
                except Exception:
                    log.exception("camera failed to start")
                    return False
                log.info("camera started")
            self.viewers += 1
            return True

    def remove_viewer(self):
        with self.viewer_lock:
            self.viewers -= 1
            if self.viewers == 0:
                try:
                    self.camera.stop()
                except Exception:
                    # A camera stuck mid-recording makes every later start fail; let systemd
                    # (Restart=always) rebuild it from a fresh process.
                    log.exception("camera failed to stop; restarting service")
                    os._exit(1)
                with self.condition:
                    self.frame = None
                log.info("camera stopped")


class PiCamera:
    def __init__(self, width, height):
        from picamera2 import Picamera2  # only on the Pi; tests use a fake camera

        # NoIR module (no infrared filter): the stock tuning's white balance turns everything blue,
        # the NoIR tuning uses grey-world white balance instead.
        # ponytail: hard-coded to the IMX219 NoIR; another camera module needs its own tuning file.
        self.picam2 = Picamera2(tuning=Picamera2.load_tuning_file("imx219_noir.json"))
        self.picam2.configure(self.picam2.create_video_configuration(main={"size": (width, height)}))

    def start(self, output):
        from picamera2.encoders import JpegEncoder
        from picamera2.outputs import FileOutput

        self.picam2.start_recording(JpegEncoder(), FileOutput(output))

    def stop(self):
        self.picam2.stop_recording()


class StreamingHandler(server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)
        elif path == "/uptime":
            # Seconds since boot (CLOCK_MONOTONIC counts from boot on Linux; the Pi never suspends).
            body = b"%d" % time.monotonic()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/stream.mjpg":
            self.stream()
        else:
            self.send_error(404)

    def stream(self):
        output = self.server.output
        if not output.add_viewer():
            self.send_error(503, "Camera unavailable or too many viewers")
            return
        try:
            self.connection.settimeout(WRITE_TIMEOUT)
            self.send_response(200)
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
            self.end_headers()
            while True:
                frame = output.wait_frame(FRAME_TIMEOUT)
                if frame is None:
                    log.warning("no frame from camera for %ss, dropping viewer", FRAME_TIMEOUT)
                    break
                header = b"--FRAME\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame)
                self.wfile.write(header + frame + b"\r\n")  # wfile is unbuffered: one syscall per frame
        except OSError as e:  # includes socket timeouts from dead or stalled clients
            log.info("viewer %s left: %s", self.client_address[0], e)
        finally:
            output.remove_viewer()
            self.close_connection = True


class StreamingServer(server.ThreadingHTTPServer):
    def server_bind(self):
        # Skip HTTPServer's reverse-DNS getfqdn(), which can stall startup before the network is up.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "babymonitor", self.server_address[1]


def make_server(output, port):
    httpd = StreamingServer(("", port), StreamingHandler)
    httpd.output = output
    return httpd


def number(cfg, key):
    try:
        return int(cfg[key])
    except ValueError:
        log.error("invalid %s=%r in config, using %s", key, cfg[key], DEFAULTS[key])
        return int(DEFAULTS[key])


def main():
    cfg = load_config(os.environ.get("BABYMONITOR_CONFIG", DEFAULT_CONFIG))
    if sys.argv[1:2] == ["--get"]:  # lets the shell scripts share this parser instead of `source`
        print(cfg.get(sys.argv[2], ""))
        return
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    port = number(cfg, "PORT")
    camera = PiCamera(number(cfg, "WIDTH"), number(cfg, "HEIGHT"))
    output = StreamingOutput(camera, number(cfg, "MAX_VIEWERS"))
    httpd = make_server(output, port)
    log.info("serving on port %s", port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
