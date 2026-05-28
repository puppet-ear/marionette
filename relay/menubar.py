#!/usr/bin/env python3
"""rrelay — Marionette relay as a macOS menu bar app.

Runs the WebSocket→OSC relay in a background thread so the whole app
ships as a single .app bundle (no external Python subprocess needed).

Usage (dev):
    pip install rumps websockets python-osc
    python menubar.py

Build:
    ./build.sh
"""

import asyncio
import json
import logging
import os
import pathlib
import sys
import threading
import time

import rumps

# ── logging ──────────────────────────────────────────────────────────────────

LOG_DIR = pathlib.Path.home() / "Library" / "Logs" / "Strings"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("rrelay")

# ── relay core (runs in background thread) ────────────────────────────────────

_relay_loop: asyncio.AbstractEventLoop | None = None
_relay_thread: threading.Thread | None = None
_relay_log_fh = None


async def _relay_main(ws_port: int, osc_port: int) -> None:
    from pythonosc.udp_client import SimpleUDPClient
    import websockets

    async def handler(ws):
        osc = SimpleUDPClient("127.0.0.1", osc_port)
        frame_count = 0
        addr = getattr(ws, "remote_address", "?")
        log.info(f"[+] browser connected  {addr}")

        async for raw in ws:
            try:
                packet = json.loads(raw)
                if not packet:
                    continue
                for name, val in packet.items():
                    if name.startswith("__"):
                        osc.send_message(f"/control/{name[2:]}", float(val))
                    else:
                        osc.send_message(f"/empty/{name}", [float(v) for v in val])
                frame_count += 1
            except Exception as e:
                log.warning(f"frame error: {e}")

        log.info(f"[-] browser disconnected  {addr}  ({frame_count} frames)")

    log.info(f"relay  ws://localhost:{ws_port}  →  osc://127.0.0.1:{osc_port}")
    async with websockets.serve(handler, "localhost", ws_port):
        await asyncio.Future()  # run until loop is stopped


def _relay_thread_fn(ws_port: int, osc_port: int) -> None:
    global _relay_loop
    loop = asyncio.new_event_loop()
    _relay_loop = loop
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_relay_main(ws_port, osc_port))
    except Exception as e:
        log.error(f"relay error: {e}")
    finally:
        loop.close()
        _relay_loop = None


def _start_relay(ws_port: int, osc_port: int) -> None:
    global _relay_thread, _relay_log_fh
    log_path = LOG_DIR / f"relay_{int(time.time())}.log"
    _relay_log_fh = open(log_path, "w")
    fh_handler = logging.FileHandler(log_path)
    fh_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s"))
    log.addHandler(fh_handler)
    log.info(f"session log → {log_path}")

    _relay_thread = threading.Thread(
        target=_relay_thread_fn,
        args=(ws_port, osc_port),
        daemon=True,
        name="relay",
    )
    _relay_thread.start()


def _stop_relay() -> None:
    global _relay_loop, _relay_thread, _relay_log_fh
    if _relay_loop:
        _relay_loop.call_soon_threadsafe(_relay_loop.stop)
    if _relay_thread:
        _relay_thread.join(timeout=3)
        _relay_thread = None
    # remove file handler added during start
    for h in log.handlers[:]:
        if isinstance(h, logging.FileHandler):
            h.close()
            log.removeHandler(h)
    if _relay_log_fh:
        _relay_log_fh.close()
        _relay_log_fh = None


def _relay_running() -> bool:
    return _relay_thread is not None and _relay_thread.is_alive()


# ── menu bar app ──────────────────────────────────────────────────────────────

class RRelayApp(rumps.App):
    def __init__(self):
        super().__init__("rrelay", quit_button=None)
        self.ws_port = 8765
        self.osc_port = 7700

        self.status_item = rumps.MenuItem("stopped")
        self.toggle_item = rumps.MenuItem("Start", callback=self.toggle)
        self.ws_item    = rumps.MenuItem(f"WS Port: {self.ws_port}",  callback=self.set_ws_port)
        self.osc_item   = rumps.MenuItem(f"OSC Port: {self.osc_port}", callback=self.set_osc_port)
        self.log_item   = rumps.MenuItem("Open Logs", callback=self.open_logs)
        self.kill_item  = rumps.MenuItem("Kill port…", callback=self.kill_port)
        quit_item       = rumps.MenuItem("Quit", callback=self._quit)

        self.menu = [
            self.status_item,
            None,
            self.toggle_item,
            None,
            self.ws_item,
            self.osc_item,
            None,
            self.kill_item,
            self.log_item,
            quit_item,
        ]

        self._poll_timer = rumps.Timer(self._poll, 2)
        self._poll_timer.start()

    def toggle(self, _):
        if _relay_running():
            _stop_relay()
            self._update_status(False)
            log.info("relay stopped by user")
        else:
            try:
                _start_relay(self.ws_port, self.osc_port)
                self._update_status(True)
                log.info(f"relay started  ws:{self.ws_port}  osc:{self.osc_port}")
            except Exception as e:
                log.error(f"failed to start relay: {e}")
                rumps.notification("rrelay", "Failed to start", str(e))

    def _poll(self, _):
        if not _relay_running() and self.toggle_item.title == "Stop":
            self._update_status(False)
            log.warning("relay stopped unexpectedly")
            rumps.notification("rrelay", "Relay stopped", "Open Logs for details.")

    def _update_status(self, running: bool):
        self.title = "🟢 rrelay" if running else "rrelay"
        self.status_item.title = "running" if running else "stopped"
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
            except ValueError:
                rumps.alert("Invalid port number.")

    def kill_port(self, _):
        import subprocess
        result = subprocess.run(
            ["lsof", "-ti", f":{self.ws_port}"],
            capture_output=True, text=True,
        )
        pids = result.stdout.strip().split()
        if not pids:
            rumps.alert(f"Nothing on port {self.ws_port}.")
            return
        lines = []
        for pid in pids:
            info = subprocess.run(
                ["ps", "-p", pid, "-o", "pid=,comm="],
                capture_output=True, text=True,
            ).stdout.strip()
            lines.append(info or pid)
        resp = rumps.alert(
            title=f"Kill port {self.ws_port}?",
            message=f"These processes are using it:\n\n{chr(10).join(lines)}\n\nKill them?",
            ok="Kill",
            cancel="Cancel",
        )
        if resp == 1:
            for pid in pids:
                subprocess.run(["kill", "-9", pid])
            log.info(f"killed pids {pids} on port {self.ws_port}")

    def open_logs(self, _):
        os.system(f"open '{LOG_DIR}'")

    def _quit(self, _):
        if _relay_running():
            _stop_relay()
        rumps.quit_application()


if __name__ == "__main__":
    RRelayApp().run()
