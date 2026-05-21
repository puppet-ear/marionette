#!/usr/bin/env python3
"""
Digital Marionette — Hand Tracker

Streams absolute normalised fingertip positions via UDP.
No calibration, no deltas — just raw (x, y, z) in [0,1] space.
Blender maps those directly to world coordinates.

Usage:
    python hand_tracker.py                  # test video
    python hand_tracker.py --webcam 0       # webcam
"""

import argparse
import json
import socket
import time
import cv2
import mediapipe as mp

# ── Config ───────────────────────────────────────────────────────────────────
SMOOTHING_ALPHA = 0.7   # display smoothing only (0 = sluggish, 1 = raw)

FINGER_TIPS = {"thumb": 4, "index": 8, "middle": 12, "ring": 16, "pinky": 20}
FINGER_PIP  = {"thumb": 3, "index": 6, "middle": 10, "ring": 14, "pinky": 18}

UDP_HOST = "127.0.0.1"
UDP_PORT = 5005

TIP_RADIUS = 16

HAND_COLORS = {
    "Left":  {"tip": (0, 180, 255), "label": (0, 200, 255)},
    "Right": {"tip": (80, 255, 80),  "label": (200, 255, 200)},
}
COLOR_NOHAND = (60, 60, 60)


# ── Helpers ──────────────────────────────────────────────────────────────────
def landmark_to_pixel(lm, w, h):
    return (int(lm.x * w), int(lm.y * h))


def lerp_point(old, new, alpha):
    return (
        int(old[0] * (1 - alpha) + new[0] * alpha),
        int(old[1] * (1 - alpha) + new[1] * alpha),
    )


def get_extended_fingers(landmarks):
    """Return names of fingers whose tip is farther from wrist than its PIP."""
    wrist = landmarks[0]
    extended = set()
    for name, tip_id in FINGER_TIPS.items():
        pip_id = FINGER_PIP[name]
        tip, pip = landmarks[tip_id], landmarks[pip_id]
        tip_d = ((tip.x-wrist.x)**2 + (tip.y-wrist.y)**2 + (tip.z-wrist.z)**2) ** 0.5
        pip_d = ((pip.x-wrist.x)**2 + (pip.y-wrist.y)**2 + (pip.z-wrist.z)**2) ** 0.5
        if tip_d > pip_d:
            extended.add(name)
    return extended


# ── Per-hand processing ───────────────────────────────────────────────────────
def process_hand(frame, hand_lm, smoothed, hand_label, w, h):
    """Draw fingertips and return absolute normalised positions for extended fingers."""
    extended = get_extended_fingers(hand_lm.landmark)
    colors   = HAND_COLORS[hand_label]
    positions = {}   # {name: (norm_x, norm_y, norm_z)}

    for name, idx in FINGER_TIPS.items():
        lm     = hand_lm.landmark[idx]
        raw_px = landmark_to_pixel(lm, w, h)
        px     = (lerp_point(smoothed[name], raw_px, SMOOTHING_ALPHA)
                  if name in smoothed else raw_px)
        smoothed[name] = px

        if name not in extended:
            continue

        # Draw
        cv2.circle(frame, px, TIP_RADIUS, colors["tip"], -1)
        cv2.circle(frame, px, TIP_RADIUS + 2, colors["tip"], 1)
        cv2.putText(frame, name, (px[0] + TIP_RADIUS + 3, px[1] - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.40, colors["label"], 1)

        positions[name] = (lm.x, lm.y, lm.z)

    return positions


# ── Main loop ────────────────────────────────────────────────────────────────
def run(source):
    is_webcam = isinstance(source, int)

    mp_hands = mp.solutions.hands
    hands = mp_hands.Hands(
        static_image_mode=False,
        max_num_hands=2,
        model_complexity=1,
        min_detection_confidence=0.4,
        min_tracking_confidence=0.6,
    )

    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"ERROR: cannot open source: {source}")
        return

    w       = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h       = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30
    print(f"Source: {source}  ({w}x{h} @ {src_fps:.0f}fps)")
    print("Press 'q' to quit, SPACE to pause (video only)")

    # Smoothing state per hand (display only)
    smoothed  = {"Left": {}, "Right": {}}
    prev_time = time.time()
    paused    = False

    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"Streaming positions → {UDP_HOST}:{UDP_PORT}")

    while cap.isOpened():
        if not paused:
            ok, frame = cap.read()
            if not ok:
                break

        if is_webcam:
            frame = cv2.flip(frame, 1)

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False
        results = hands.process(rgb)

        detected = {}
        if results.multi_hand_landmarks and results.multi_handedness:
            for lm_list, handedness in zip(results.multi_hand_landmarks,
                                           results.multi_handedness):
                label = handedness.classification[0].label
                if is_webcam:
                    label = "Right" if label == "Left" else "Left"
                detected[label] = lm_list

        packet = {}
        for label in ("Left", "Right"):
            hand_key = label.lower()
            if label in detected:
                positions = process_hand(frame, detected[label],
                                         smoothed[label], label, w, h)
                for finger, pos in positions.items():
                    packet[f"{hand_key}_{finger}"] = list(pos)

        udp_sock.sendto(json.dumps(packet).encode(), (UDP_HOST, UDP_PORT))

        # HUD
        now       = time.time()
        fps       = 1.0 / max(now - prev_time, 1e-6)
        prev_time = now
        cv2.putText(frame, f"FPS: {int(fps)}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        y = 65
        for label in ("Left", "Right"):
            detected_now = label in detected
            color = HAND_COLORS[label]["tip"] if detected_now else COLOR_NOHAND
            cv2.putText(frame, f"{label}: {'TRACKED' if detected_now else 'NO HAND'}",
                        (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
            y += 32

        cv2.imshow("Digital Marionette Tracker", frame)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord('q'), 27):
            break
        elif key == ord(' ') and not is_webcam:
            paused = not paused
            print("Paused." if paused else "Resumed.")

    cap.release()
    cv2.destroyAllWindows()
    hands.close()
    udp_sock.close()
    print("Done.")


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--webcam",    type=int,   default=None)
    parser.add_argument("--video",     type=str,   default=None)
    parser.add_argument("--smoothing", type=float, default=SMOOTHING_ALPHA)
    args = parser.parse_args()

    SMOOTHING_ALPHA = args.smoothing

    if args.webcam is not None:
        source = args.webcam
    elif args.video is not None:
        source = args.video
    else:
        source = "/Users/Dhruv/Desktop/GB/Code/marionette/pupeteer_test.mov"

    run(source)
