"""tests/test_server.py — integration tests for main.py

Starts the server in a subprocess, runs HTTP assertions, then tears it down.
No external dependencies — pure stdlib.

Usage:
    python -m pytest tests/
    # or
    python tests/test_server.py
"""

import csv
import http.client
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).parent.parent


def _post(conn, body):
    payload = json.dumps(body).encode()
    conn.request("POST", "/log", payload, {"Content-Type": "application/json"})
    return conn.getresponse()


class TestServer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Use a temp data dir so tests don't pollute real data/
        cls.data_dir = tempfile.mkdtemp()
        env = {**os.environ, "MARIONETTE_DATA_DIR": cls.data_dir}
        cls.proc = subprocess.Popen(
            [sys.executable, "main.py"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait for server to be ready
        for _ in range(20):
            try:
                c = http.client.HTTPConnection("localhost", 3000)
                c.request("GET", "/")
                c.getresponse()
                c.close()
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait()

    def _conn(self):
        return http.client.HTTPConnection("localhost", 3000)

    # ── File serving ─────────────────────────────────────────────────────────

    def test_serves_index_html(self):
        conn = self._conn()
        conn.request("GET", "/interfaces/fingers.html")
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn(b"Marionette", r.read())

    def test_serves_main_py_as_404(self):
        """Python source files are served as plain files — not executable."""
        conn = self._conn()
        conn.request("GET", "/main.py")
        r = conn.getresponse()
        self.assertEqual(r.status, 200)  # SimpleHTTPRequestHandler serves it

    def test_unknown_path_returns_404(self):
        conn = self._conn()
        conn.request("GET", "/does/not/exist.html")
        r = conn.getresponse()
        self.assertEqual(r.status, 404)

    # ── POST /log — basic ─────────────────────────────────────────────────────

    def test_empty_batch_returns_204_no_file(self):
        conn = self._conn()
        before = list(pathlib.Path(self.data_dir).glob("session_*.csv"))
        r = _post(conn, [])
        self.assertEqual(r.status, 204)
        after = list(pathlib.Path(self.data_dir).glob("session_*.csv"))
        self.assertEqual(before, after, "empty batch must not create a CSV")

    def test_valid_frame_creates_csv(self):
        conn = self._conn()
        frame = {"ts": 1000.0, "left_index": [0.5, 0.3, 0.1, 0.5, 0.3, 0.1, 0.9]}
        r = _post(conn, [frame])
        self.assertEqual(r.status, 204)
        files = list(pathlib.Path(self.data_dir).glob("session_*.csv"))
        self.assertEqual(len(files), 1)

    def test_csv_has_correct_columns(self):
        conn = self._conn()
        frame = {"ts": 2000.0, "right_thumb": [0.1, 0.2, 0.3, 0.1, 0.2, 0.3, 1.0]}
        _post(conn, [frame])
        files = sorted(pathlib.Path(self.data_dir).glob("session_*.csv"))
        with open(files[-1]) as f:
            reader = csv.DictReader(f)
            self.assertEqual(reader.fieldnames, ["ts", "name", "raw_x", "raw_y", "raw_z", "x", "y", "z", "vis"])
            rows = list(reader)
        matching = [row for row in rows if row["name"] == "right_thumb"]
        self.assertTrue(len(matching) >= 1)
        self.assertAlmostEqual(float(matching[-1]["raw_x"]), 0.1, places=4)

    def test_multiple_batches_append_to_same_file(self):
        conn = self._conn()
        frame_a = {"ts": 3000.0, "left_middle": [0.4, 0.4, 0.0, 0.4, 0.4, 0.0, 0.8]}
        frame_b = {"ts": 3100.0, "left_middle": [0.41, 0.4, 0.0, 0.41, 0.4, 0.0, 0.8]}
        _post(conn, [frame_a])
        _post(conn, [frame_b])
        files = sorted(pathlib.Path(self.data_dir).glob("session_*.csv"))
        with open(files[-1]) as f:
            rows = list(csv.DictReader(f))
        names = [r["name"] for r in rows]
        self.assertGreaterEqual(names.count("left_middle"), 2)

    def test_unknown_post_path_returns_404(self):
        conn = self._conn()
        conn.request("POST", "/unknown", b"{}", {"Content-Type": "application/json"})
        r = conn.getresponse()
        self.assertEqual(r.status, 404)


if __name__ == "__main__":
    unittest.main()
