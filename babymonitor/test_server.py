import http.client
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import server


class FakeCamera:
    """Writes frames from a worker thread while started, like picamera2's encoder."""

    def __init__(self, frame=b"jpeg", produce=True, fail_starts=0):
        self.frame = frame
        self.produce = produce
        self.fail_starts = fail_starts
        self.starts = 0
        self.stops = 0
        self.stop_write_ok = None
        self._running = threading.Event()
        self._thread = None

    def start(self, output):
        if self.fail_starts:
            self.fail_starts -= 1
            raise RuntimeError("camera busy")
        self.starts += 1
        self.output = output
        self._running.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while self._running.is_set():
            if self.produce:
                self.output.write(self.frame)
            time.sleep(0.01)

    def stop(self):
        # picamera2's stop_recording joins encoder threads that may be inside write();
        # prove write() is not blocked by whatever lock stop() is called under.
        t = threading.Thread(target=self.output.write, args=(b"last",))
        t.start()
        t.join(1)
        self.stop_write_ok = not t.is_alive()
        self._running.clear()
        self._thread.join()
        self.stops += 1


def wait_for(pred, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


class ServerTest(unittest.TestCase):
    def start_server(self, camera, max_viewers=5):
        self.output = server.StreamingOutput(camera, max_viewers)
        self.httpd = server.make_server(self.output, port=0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def open_stream(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/stream.mjpg")
        resp = conn.getresponse()
        # The response holds the socket; close both so the server sees the disconnect.
        conn.close = lambda: (resp.close(), http.client.HTTPConnection.close(conn))
        return conn, resp

    def test_index_and_404(self):
        self.start_server(FakeCamera())
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn(b'src="/stream.mjpg"', resp.read())
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/?cachebust=1")  # the page's reconnect adds a query string
        self.assertEqual(conn.getresponse().status, 200)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/nope")
        self.assertEqual(conn.getresponse().status, 404)

    def test_camera_idle_until_watched_and_stops_after(self):
        cam = FakeCamera(frame=b"FRAMEDATA")
        self.start_server(cam)
        time.sleep(0.1)
        self.assertEqual(cam.starts, 0)
        conn, resp = self.open_stream()
        self.assertEqual(resp.status, 200)
        self.assertIn("multipart/x-mixed-replace", resp.getheader("Content-Type"))
        self.assertIn(b"FRAMEDATA", resp.read(200))
        self.assertEqual(cam.starts, 1)
        conn.close()
        self.assertTrue(wait_for(lambda: cam.stops == 1))
        self.assertEqual(self.output.viewers, 0)
        self.assertTrue(cam.stop_write_ok, "write() blocked during stop -> deadlock")

    def test_two_viewers_share_one_camera_session(self):
        cam = FakeCamera()
        self.start_server(cam)
        c1, r1 = self.open_stream()
        r1.read(50)
        c2, r2 = self.open_stream()
        r2.read(50)
        self.assertEqual(cam.starts, 1)
        c1.close()
        self.assertTrue(wait_for(lambda: self.output.viewers == 1))
        self.assertEqual(cam.stops, 0)
        c2.close()
        self.assertTrue(wait_for(lambda: cam.stops == 1))
        self.assertEqual(cam.starts, 1)

    def test_no_frames_ends_stream_and_stops_camera(self):
        cam = FakeCamera(produce=False)
        self.start_server(cam)
        old, server.FRAME_TIMEOUT = server.FRAME_TIMEOUT, 0.2
        self.addCleanup(setattr, server, "FRAME_TIMEOUT", old)
        conn, resp = self.open_stream()
        self.assertTrue(wait_for(lambda: cam.stops == 1))
        self.assertEqual(self.output.viewers, 0)
        conn.close()

    def test_start_failure_returns_503_and_retries_next_viewer(self):
        cam = FakeCamera(fail_starts=1)
        self.start_server(cam)
        conn, resp = self.open_stream()
        self.assertEqual(resp.status, 503)
        self.assertEqual(self.output.viewers, 0)
        conn.close()
        conn, resp = self.open_stream()
        self.assertEqual(resp.status, 200)
        self.assertEqual(cam.starts, 1)
        conn.close()

    def test_viewer_cap(self):
        self.start_server(FakeCamera(), max_viewers=1)
        c1, r1 = self.open_stream()
        r1.read(10)
        c2, r2 = self.open_stream()
        self.assertEqual(r2.status, 503)
        c1.close()
        c2.close()

    def test_stalled_client_times_out_and_camera_stops(self):
        cam = FakeCamera(frame=b"x" * 256 * 1024)
        self.start_server(cam)
        old, server.WRITE_TIMEOUT = server.WRITE_TIMEOUT, 0.3
        self.addCleanup(setattr, server, "WRITE_TIMEOUT", old)
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        s.connect(("127.0.0.1", self.port))
        s.sendall(b"GET /stream.mjpg HTTP/1.1\r\nHost: x\r\n\r\n")
        # Never read: kernel buffers fill, the server's write times out.
        self.assertTrue(wait_for(lambda: cam.stops == 1, timeout=10))
        s.close()


class StopFailureTest(unittest.TestCase):
    def test_failed_stop_exits_for_systemd_restart(self):
        cam = FakeCamera()
        output = server.StreamingOutput(cam, 5)
        output.add_viewer()
        cam.stop = lambda: (_ for _ in ()).throw(RuntimeError("wedged"))
        exits = []
        old = server.os._exit
        server.os._exit = exits.append
        self.addCleanup(setattr, server.os, "_exit", old)
        output.remove_viewer()
        self.assertEqual(exits, [1])
        cam._running.clear()


class ConfigTest(unittest.TestCase):
    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", delete=False, newline="")
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_get_cli_prints_literal_value(self):
        path = self.write("WIFI_PASSWORD='a$b`c\"d'\n")
        env = dict(os.environ, BABYMONITOR_CONFIG=path)
        run = lambda key: subprocess.run(
            [sys.executable, server.__file__, "--get", key], env=env, capture_output=True, text=True, check=True
        ).stdout
        self.assertEqual(run("WIFI_PASSWORD"), 'a$b`c"d\n')
        self.assertEqual(run("MISSING"), "\n")

    def test_bom_is_ignored(self):
        cfg = server.load_config(self.write("\ufeffWIFI_SSID=home\n"))
        self.assertEqual(cfg["WIFI_SSID"], "home")

    def test_invalid_number_falls_back_to_default(self):
        self.assertEqual(server.number({"PORT": "80x0"}, "PORT"), 80)

    def test_missing_file_gives_defaults(self):
        cfg = server.load_config("/nonexistent/babymonitor.conf")
        self.assertEqual(cfg["PORT"], "80")

    def test_literal_parsing(self):
        path = self.write(
            "# comment\r\n"
            "\n"
            "WIFI_SSID=My Home Net\r\n"
            "WIFI_PASSWORD=\"pa$$`w\"o=rd\"\n"
            "WIFI_COUNTRY='GB'\n"
            "  # indented comment\n"
            "PORT=9000\n"
            "garbage line\n"
        )
        cfg = server.load_config(path)
        self.assertEqual(cfg["WIFI_SSID"], "My Home Net")
        self.assertEqual(cfg["WIFI_PASSWORD"], 'pa$$`w"o=rd')
        self.assertEqual(cfg["WIFI_COUNTRY"], "GB")
        self.assertEqual(cfg["PORT"], "9000")
        self.assertEqual(cfg["WIDTH"], "1280")


if __name__ == "__main__":
    unittest.main()
