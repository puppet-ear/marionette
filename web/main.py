#!/usr/bin/env python3
"""serve.py — one command to run Marionette

Serves project files at http://localhost:3000 and writes every tracked
frame to a timestamped CSV in data/ the moment the first hand appears.

No dependencies — pure stdlib.
Usage: python3 serve.py
"""

import csv
import json
import os
import pathlib
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT     = 3000
DATA_DIR = pathlib.Path(os.environ.get("MARIONETTE_DATA_DIR", pathlib.Path(__file__).parent / "data"))
DATA_DIR.mkdir(exist_ok=True)

_csv_file   = None
_csv_writer = None
_session_path = None


def _ensure_writer():
    global _csv_file, _csv_writer, _session_path
    if _csv_writer is not None:
        return
    _session_path = DATA_DIR / f"session_{int(time.time())}.csv"
    _csv_file     = open(_session_path, "w", newline="")
    _csv_writer   = csv.writer(_csv_file)
    _csv_writer.writerow(["ts", "name", "raw_x", "raw_y", "raw_z", "x", "y", "z", "vis"])
    print(f"  logging  → {_session_path}")


class Handler(SimpleHTTPRequestHandler):

    def do_POST(self):
        if self.path != "/log":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        frames = json.loads(self.rfile.read(length))

        if frames:
            _ensure_writer()
            for frame in frames:
                ts = frame.pop("ts", time.time())
                for name, vals in frame.items():
                    # vals: [raw_x, raw_y, raw_z, filt_x, filt_y, filt_z, vis]
                    _csv_writer.writerow([f"{ts:.4f}", name] + [f"{v:.6f}" for v in vals])
            _csv_file.flush()

        self.send_response(204)
        self.end_headers()

    def log_message(self, *_):
        pass  # silence per-request noise


class Server(ThreadingHTTPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    os.chdir(pathlib.Path(__file__).parent)
    print(f"  serving  → http://localhost:{PORT}")
    print(f"  open       http://localhost:{PORT}/interfaces/fingers.html")
    print()
    Server(("localhost", PORT), Handler).serve_forever()
