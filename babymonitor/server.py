#!/usr/bin/env python3
"""Baby monitor: MJPEG stream from the Raspberry Pi camera, running only while watched."""
import io
import logging
import os
import socketserver
import threading
from http import server

log = logging.getLogger("babymonitor")

DEFAULT_CONFIG = "/boot/firmware/babymonitor.conf"
DEFAULTS = {"PORT": "8000", "WIDTH": "1280", "HEIGHT": "720", "MAX_VIEWERS": "5"}
FRAME_TIMEOUT = 5  # seconds without a frame before a viewer is dropped
WRITE_TIMEOUT = 10  # seconds a stalled/dead client may block a write

PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Baby Monitor</title>
<style>html,body{margin:0;height:100%;background:#000}
img{display:block;width:100%;height:100%;object-fit:contain}</style></head>
<body><img src="/stream.mjpg" alt="Live camera"></body></html>
"""


def load_config(path):
    """Parse KEY=value lines literally: no shell expansion, one layer of quotes stripped."""
    cfg = dict(DEFAULTS)
    try:
        f = open(path, encoding="utf-8")
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
                    log.exception("camera failed to stop cleanly")
                with self.condition:
                    self.frame = None
                log.info("camera stopped")


class PiCamera:
    def __init__(self, width, height):
        from picamera2 import Picamera2  # only on the Pi; tests use a fake camera

        self.picam2 = Picamera2()
        self.picam2.configure(self.picam2.create_video_configuration(main={"size": (width, height)}))

    def start(self, output):
        from picamera2.encoders import JpegEncoder
        from picamera2.outputs import FileOutput

        self.picam2.start_recording(JpegEncoder(), FileOutput(output))

    def stop(self):
        self.picam2.stop_recording()


class StreamingHandler(server.BaseHTTPRequestHandler):
    output = None  # set per server by make_server

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)
        elif self.path == "/stream.mjpg":
            self.stream()
        else:
            self.send_error(404)

    def stream(self):
        if not self.output.add_viewer():
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
                frame = self.output.wait_frame(FRAME_TIMEOUT)
                if frame is None:
                    log.warning("no frame from camera for %ss, dropping viewer", FRAME_TIMEOUT)
                    break
                self.wfile.write(b"--FRAME\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(b"Content-Length: %d\r\n\r\n" % len(frame))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except OSError as e:  # includes socket timeouts from dead or stalled clients
            log.info("viewer %s left: %s", self.client_address[0], e)
        finally:
            self.output.remove_viewer()
            self.close_connection = True


class StreamingServer(server.ThreadingHTTPServer):
    def server_bind(self):
        # Skip HTTPServer's reverse-DNS getfqdn(), which can stall startup before the network is up.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "babymonitor", self.server_address[1]


def make_server(output, port):
    handler = type("Handler", (StreamingHandler,), {"output": output})
    return StreamingServer(("", port), handler)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config(os.environ.get("BABYMONITOR_CONFIG", DEFAULT_CONFIG))
    camera = PiCamera(int(cfg["WIDTH"]), int(cfg["HEIGHT"]))
    output = StreamingOutput(camera, int(cfg["MAX_VIEWERS"]))
    httpd = make_server(output, int(cfg["PORT"]))
    log.info("serving on port %s", cfg["PORT"])
    httpd.serve_forever()


if __name__ == "__main__":
    main()
