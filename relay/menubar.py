#!/usr/bin/env python3
"""Strings — Marionette relay as a macOS menu bar app.

Starts/stops relay.py as a subprocess. Relay stdout+stderr stream to a
timestamped log file in ~/Library/Logs/Strings/.

Usage:
    pip install rumps websockets python-osc
    python menubar.py
"""

import logging
import os
import pathlib
import subprocess
import sys
import time

import rumps

HERE = pathlib.Path(__file__).parent
RELAY_PY = HERE / "relay.py"

LOG_DIR = pathlib.Path.home() / "Library" / "Logs" / "Strings"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("strings")


class StringsApp(rumps.App):
    def __init__(self):
        super().__init__("Strings", quit_button=None)
        self.ws_port = 8765
        self.osc_port = 7700
        self._proc = None
        self._log_fh = None

        self.status_item = rumps.MenuItem("● Stopped")
        self.toggle_item = rumps.MenuItem("Start", callback=self.toggle)
        self.ws_item = rumps.MenuItem(f"WS Port: {self.ws_port}", callback=self.set_ws_port)
        self.osc_item = rumps.MenuItem(f"OSC Port: {self.osc_port}", callback=self.set_osc_port)
        self.log_item = rumps.MenuItem("Open Logs", callback=self.open_logs)
        quit_item = rumps.MenuItem("Quit", callback=rumps.quit_application)

        self.menu = [
            self.status_item,
            None,
            self.toggle_item,
            None,
            self.ws_item,
            self.osc_item,
            None,
            self.log_item,
            quit_item,
        ]

        self._poll_timer = rumps.Timer(self._poll, 2)
        self._poll_timer.start()

    def toggle(self, _):
        if self._proc and self._proc.poll() is None:
            self._stop()
        else:
            self._start()

    def _start(self):
        log_path = LOG_DIR / f"relay_{int(time.time())}.log"
        self._log_fh = open(log_path, "w")
        try:
            self._proc = subprocess.Popen(
                [sys.executable, str(RELAY_PY),
                 "--ws-port", str(self.ws_port),
                 "--osc-port", str(self.osc_port)],
                stdout=self._log_fh,
                stderr=self._log_fh,
            )
        except Exception as e:
            log.error(f"Failed to start relay: {e}")
            rumps.notification("Strings", "Failed to start", str(e))
            self._log_fh.close()
            self._log_fh = None
            return
        self._update_status(True)
        log.info(f"Relay started (pid {self._proc.pid})  ws:{self.ws_port}  osc:{self.osc_port}  log→{log_path}")

    def _stop(self):
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
        self._update_status(False)
        log.info("Relay stopped")

    def _poll(self, _):
        if self._proc and self._proc.poll() is not None:
            rc = self._proc.returncode
            self._proc = None
            if self._log_fh:
                self._log_fh.close()
                self._log_fh = None
            self._update_status(False)
            log.warning(f"Relay exited unexpectedly (rc={rc})")
            rumps.notification("Strings", "Relay stopped", f"Exit code {rc}. Open Logs for details.")

    def _update_status(self, running):
        self.status_item.title = "● Running" if running else "● Stopped"
        self.toggle_item.title = "Stop" if running else "Start"

    def set_ws_port(self, _):
        r = rumps.Window(
            "WebSocket port — browser connects here.\nMust match the port field in Marionette.",
            "WS Port",
            default_text=str(self.ws_port),
            dimensions=(200, 20),
        ).run()
        if r.clicked:
            try:
                self.ws_port = int(r.text)
                self.ws_item.title = f"WS Port: {self.ws_port}"
                log.info(f"WS port → {self.ws_port}")
            except ValueError:
                rumps.alert("Invalid port number.")

    def set_osc_port(self, _):
        r = rumps.Window(
            "OSC port — Blender listens here.\nMust match Blender → Marionette tab → OSC Port.",
            "OSC Port",
            default_text=str(self.osc_port),
            dimensions=(200, 20),
        ).run()
        if r.clicked:
            try:
                self.osc_port = int(r.text)
                self.osc_item.title = f"OSC Port: {self.osc_port}"
                log.info(f"OSC port → {self.osc_port}")
            except ValueError:
                rumps.alert("Invalid port number.")

    def open_logs(self, _):
        os.system(f"open '{LOG_DIR}'")


if __name__ == "__main__":
    StringsApp().run()
