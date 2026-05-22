#!/usr/bin/env python3
"""relay.py — WebSocket → OSC bridge + CSV logger

Receives JSON packets from the browser interface, forwards them as OSC
messages to Blender, and streams every frame to a timestamped CSV file
the moment the first hand is detected.

Packet format:  {"left_index": [x, y, z], "right_thumb": [x, y, z], …}
OSC output:     /empty/left_index  f f f
CSV columns:    ts, name, x, y, z

Usage:
    pip install websockets python-osc
    python relay.py
"""

import argparse
import asyncio
import csv
import json
import pathlib
import time
import websockets
from pythonosc.udp_client import SimpleUDPClient

parser = argparse.ArgumentParser()
parser.add_argument("--ws-port",  type=int, default=8765)
parser.add_argument("--osc-port", type=int, default=7700)
args = parser.parse_args()

WS_HOST  = "localhost"
WS_PORT  = args.ws_port
OSC_HOST = "127.0.0.1"
OSC_PORT = args.osc_port

DATA_DIR = pathlib.Path(__file__).parent.parent / "data"


def open_csv():
    DATA_DIR.mkdir(exist_ok=True)
    filename = DATA_DIR / f"session_{int(time.time())}.csv"
    fh = open(filename, "w", newline="")
    writer = csv.writer(fh)
    writer.writerow(["ts", "name", "x", "y", "z"])
    print(f"logging → {filename}")
    return fh, writer


async def handler(ws):
    osc = SimpleUDPClient(OSC_HOST, OSC_PORT)
    fh, writer = None, None

    async for raw in ws:
        try:
            packet = json.loads(raw)
            if not packet:
                continue

            # Open CSV on first frame with hand data
            if fh is None:
                fh, writer = open_csv()

            ts = time.time()
            for name, xyz in packet.items():
                osc.send_message(f"/empty/{name}", [float(v) for v in xyz])
                writer.writerow([f"{ts:.4f}", name, f"{xyz[0]:.6f}", f"{xyz[1]:.6f}", f"{xyz[2]:.6f}"])
            fh.flush()

        except Exception:
            pass

    if fh:
        fh.close()


async def main():
    print(f"relay  WS port {WS_PORT}  →  OSC port {OSC_PORT}")
    async with websockets.serve(handler, WS_HOST, WS_PORT):
        await asyncio.Future()


asyncio.run(main())
