import http.client
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from pilot.hosted import public_origin, storage_path


class HostedPilotTest(unittest.TestCase):
    def test_https_proxy_origin_and_volume_are_explicit(self):
        self.assertEqual(
            public_origin({"RAILWAY_PUBLIC_DOMAIN": "pilot.example"}), "https://pilot.example"
        )
        for bad in (
            "http://pilot.example",
            "https://pilot.example/other",
            "https://me@pilot.example",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                public_origin({"FINDER_PUBLIC_URL": bad})
        with self.assertRaises(ValueError):
            storage_path({"RAILWAY_PROJECT_ID": "example"})
        with self.assertRaises(ValueError):
            storage_path(
                {"FINDER_PILOT_DB": "/tmp/other.sqlite3", "RAILWAY_VOLUME_MOUNT_PATH": "/data"}
            )
        self.assertEqual(
            storage_path(
                {
                    "RAILWAY_VOLUME_MOUNT_PATH": "/data",
                    "FINDER_PILOT_DB": "/data/finder/private.sqlite3",
                }
            ),
            Path("/data/finder/private.sqlite3"),
        )

    def test_service_worker_and_web_restart_with_persistent_data(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "data/finder.sqlite3"
            with socket.socket() as port_socket:
                port_socket.bind(("127.0.0.1", 0))
                port = port_socket.getsockname()[1]
            env = {
                "PATH": os.environ["PATH"],
                "FINDER_PUBLIC_URL": "https://pilot.example",
                "FINDER_PILOT_DB": str(database),
                "PORT": str(port),
                "PYTHONUNBUFFERED": "1",
            }

            def get(path, host):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                try:
                    conn.request("GET", path, headers={"Host": host})
                    response = conn.getresponse()
                    response.read()
                    return response.status, response.headers
                finally:
                    conn.close()

            for run in range(2):
                proc = subprocess.Popen(
                    [sys.executable, "-m", "pilot.hosted"],
                    env=env,
                    cwd=Path(__file__).resolve().parents[2],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                try:
                    end = time.monotonic() + 8
                    while time.monotonic() < end and proc.poll() is None:
                        try:
                            if get("/healthz", "healthcheck.railway.app")[0] == 200:
                                break
                        except OSError:
                            time.sleep(0.05)
                    else:
                        self.fail("Hosted process did not become healthy")
                    status, headers = get("/login", "pilot.example")
                    self.assertEqual(status, 200)
                    self.assertIn("Secure", headers.get("Set-Cookie", ""))
                    self.assertEqual(get("/login", "other.example")[0], 400)
                    self.assertEqual(get("/login", "healthcheck.railway.app")[0], 400)
                    with sqlite3.connect(database) as db:
                        if run == 0:
                            db.execute(
                                "INSERT INTO users(username,password_hash) VALUES ('persistent','unused')"
                            )
                        self.assertEqual(
                            db.execute("SELECT username FROM users").fetchone()[0], "persistent"
                        )
                finally:
                    proc.terminate()
                    try:
                        log = proc.communicate(timeout=5)[0].decode()
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate()
                        self.fail("Hosted process did not shut down")
                self.assertEqual(proc.returncode, 0, log)


if __name__ == "__main__":
    unittest.main()
