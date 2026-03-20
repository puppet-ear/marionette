#!/usr/bin/env python3
"""
Digital Marionette — Hand Tracker

Tracks index + middle fingertips per hand via MediaPipe.
State machine per hand:  NO_HAND → CALIBRATING → ACTIVE
State machine per finger: inactive → active → waiting → (return to string) → active

The "string" metaphor:
  - When you first extend a finger it immediately grabs control (active).
  - When you fold/lose a finger the Empty holds; a ghost dot marks where
    the string is hanging.  You must bring your finger back within
    ENGAGE_RADIUS pixels of that ghost to regain control.
  - On re-engagement the origin re-anchors so delta=0 → no snap.

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
import numpy as np

# ── Config ───────────────────────────────────────────────────────────────────
T_THRESHOLD      = 0.5   # seconds hand must be still before locking origin
SMOOTHING_ALPHA  = 0.8   # 0 = sluggish, 1 = raw
STILLNESS_RADIUS = 40    # pixels — max wrist drift during calibration
ENGAGE_RADIUS    = 70    # pixels — how close finger must be to re-grab a string

FINGER_TIPS = {"index": 8, "middle": 12}
FINGER_PIP  = {"index": 6, "middle": 10}

UDP_HOST = "127.0.0.1"
UDP_PORT = 5005

CALIB_RADIUS  = 22
TIP_RADIUS    = 16
ORIGIN_RADIUS = 18

HAND_COLORS = {
    "Left":  {"origin": (0, 140, 255), "string": (0, 200, 255),
               "calib":  (0, 160, 255), "tip":    (0, 180, 255)},
    "Right": {"origin": (50, 220, 50),  "string": (200, 255, 200),
               "calib":  (0, 220, 100), "tip":    (80, 255, 80)},
}
COLOR_ACTIVE  = (0, 255, 0)
COLOR_CALIB   = (0, 200, 255)
COLOR_NOHAND  = (60, 60, 60)
COLOR_DELTA   = (255, 200, 0)
COLOR_WAITING = (180, 180, 255)   # ghost / seeking colour


# ── State ────────────────────────────────────────────────────────────────────
class TrackerState:
    def __init__(self):
        self.smoothed          = {}   # name → last smoothed pixel (always updated)
        self.finger_engagement = {}   # name → "inactive" | "waiting" | "active"
        self.last_screen_pos   = {}   # name → (px, py) saved when going waiting
        self.reset()

    def reset(self):
        """Called when the hand disappears.  Calibration state clears;
        finger engagement transitions active→waiting so the user must
        find the string again after re-detection."""
        self.hand_start_time = None
        self.anchor_px       = None
        self.is_active       = False
        self.c_origin        = {}
        self.c_origin_norm   = {}
        for name in list(self.finger_engagement):
            if self.finger_engagement[name] == "active":
                self.finger_engagement[name] = "waiting"
                # last_screen_pos is already saved from the last active frame

    @property
    def status(self):
        if self.is_active:                   return "ACTIVE"
        if self.hand_start_time is not None: return "CALIBRATING"
        return "NO HAND"


# ── Helpers ──────────────────────────────────────────────────────────────────
def landmark_to_pixel(lm, w, h):
    return (int(lm.x * w), int(lm.y * h))


def lerp_point(old, new, alpha):
    return (
        int(old[0] * (1 - alpha) + new[0] * alpha),
        int(old[1] * (1 - alpha) + new[1] * alpha),
    )


def get_extended_fingers(landmarks):
    """Return names of fingers whose tip is farther from wrist than its PIP (3-D)."""
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


# ── Drawing ──────────────────────────────────────────────────────────────────
def draw_calib_dots(frame, state, current_positions, hand_label):
    """Ghost ring per fingertip that fills with colour as calibration progresses."""
    if state.status != "CALIBRATING" or not current_positions:
        return
    elapsed  = time.time() - state.hand_start_time
    progress = min(elapsed / T_THRESHOLD, 1.0)
    color    = HAND_COLORS[hand_label]["calib"]

    for px in current_positions.values():
        overlay = frame.copy()
        cv2.circle(overlay, px, CALIB_RADIUS, color, 2)
        cv2.addWeighted(overlay, 0.18, frame, 0.82, 0, frame)

        if progress > 0:
            overlay2 = frame.copy()
            cv2.circle(overlay2, px, CALIB_RADIUS, color, -1)
            cv2.addWeighted(overlay2, progress * 0.80, frame, 1 - progress * 0.80, 0, frame)


def draw_strings(frame, state, current_positions, hand_label):
    """Origin dots + string lines + active fingertip dots."""
    colors = HAND_COLORS[hand_label]
    for name, origin_px in state.c_origin.items():
        if state.finger_engagement.get(name) != "active":
            continue
        ox, oy = origin_px[0], origin_px[1]

        cv2.circle(frame, (ox, oy), ORIGIN_RADIUS, colors["origin"], 2)
        cv2.circle(frame, (ox, oy), 4, colors["origin"], -1)

        if name not in current_positions:
            continue
        cur = current_positions[name]

        cv2.circle(frame, cur, TIP_RADIUS, colors["tip"], -1)
        cv2.circle(frame, cur, TIP_RADIUS + 2, colors["tip"], 1)

        cv2.line(frame, (ox, oy), cur, colors["string"], 2)

        dx, dy = cur[0] - ox, cur[1] - oy
        dist   = np.sqrt(dx*dx + dy*dy)
        mid    = ((ox + cur[0]) // 2, (oy + cur[1]) // 2)
        cv2.putText(frame, f"{name} {dist:.0f}px", (mid[0]+5, mid[1]-5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, COLOR_DELTA, 1)


def draw_waiting_strings(frame, state, current_positions, hand_label):
    """Ghost anchor dots for fingers waiting to be re-grabbed."""
    colors = HAND_COLORS[hand_label]
    for name, last_pos in state.last_screen_pos.items():
        if state.finger_engagement.get(name) != "waiting":
            continue

        lx, ly = last_pos

        # Engagement-radius ring (shows where finger needs to go)
        overlay = frame.copy()
        cv2.circle(overlay, (lx, ly), ENGAGE_RADIUS, COLOR_WAITING, 1)
        cv2.addWeighted(overlay, 0.45, frame, 0.55, 0, frame)

        # Ghost dot at last known position
        overlay2 = frame.copy()
        cv2.circle(overlay2, (lx, ly), TIP_RADIUS, colors["tip"], -1)
        cv2.addWeighted(overlay2, 0.30, frame, 0.70, 0, frame)
        cv2.circle(frame, (lx, ly), TIP_RADIUS, COLOR_WAITING, 1)

        # Seeking line if finger is currently visible but outside radius
        if name in current_positions:
            cur  = current_positions[name]
            dist = np.sqrt((cur[0]-lx)**2 + (cur[1]-ly)**2)
            overlay3 = frame.copy()
            cv2.line(overlay3, (lx, ly), cur, COLOR_WAITING, 1)
            cv2.addWeighted(overlay3, 0.45, frame, 0.55, 0, frame)
            # Distance to string
            mid = ((lx + cur[0]) // 2, (ly + cur[1]) // 2)
            cv2.putText(frame, f"{name} {dist:.0f}px",
                        (mid[0]+4, mid[1]-4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.40, COLOR_WAITING, 1)


def draw_hud(frame, states, fps):
    cv2.putText(frame, f"FPS: {int(fps)}", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    y = 65
    for label in ("Left", "Right"):
        state  = states[label]
        status = state.status
        color  = (COLOR_ACTIVE if status == "ACTIVE"
                  else COLOR_CALIB if status == "CALIBRATING"
                  else COLOR_NOHAND)
        cv2.putText(frame, f"{label}: {status}", (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        y += 32


# ── Per-hand processing ───────────────────────────────────────────────────────
def process_hand(frame, hand_lm, state, hand_label, w, h):
    extended = get_extended_fingers(hand_lm.landmark)

    # Update smoothed positions for all tracked tips (extended or not)
    current_positions = {}
    current_norm      = {}
    for name, idx in FINGER_TIPS.items():
        lm     = hand_lm.landmark[idx]
        raw_px = landmark_to_pixel(lm, w, h)
        px     = (lerp_point(state.smoothed[name], raw_px, SMOOTHING_ALPHA)
                  if name in state.smoothed else raw_px)
        state.smoothed[name] = px
        if name in extended:
            current_positions[name] = px
            current_norm[name]      = (lm.x, lm.y, lm.z)

    # Wrist stillness check (calibration only)
    wrist_px = landmark_to_pixel(hand_lm.landmark[0], w, h)
    if state.hand_start_time is None:
        state.hand_start_time = time.time()
        state.anchor_px       = wrist_px
    elif not state.is_active:
        dx = wrist_px[0] - state.anchor_px[0]
        dy = wrist_px[1] - state.anchor_px[1]
        if np.sqrt(dx*dx + dy*dy) > STILLNESS_RADIUS:
            state.hand_start_time = time.time()
            state.anchor_px       = wrist_px

    # Lock origin after threshold
    elapsed = time.time() - state.hand_start_time
    if elapsed >= T_THRESHOLD and not state.is_active:
        for name, px in current_positions.items():
            lm = hand_lm.landmark[FINGER_TIPS[name]]
            state.c_origin[name]      = (px[0], px[1], lm.z)
            state.c_origin_norm[name] = (lm.x, lm.y, lm.z)
        state.is_active = True
        print(f"Origin locked! ({hand_label})")

    # ── Finger engagement (string-grab logic) ────────────────────────
    if state.is_active:
        # Any currently active finger that folded → goes to waiting, save screen pos
        for name in list(state.finger_engagement):
            if (state.finger_engagement[name] == "active"
                    and name not in current_positions):
                state.finger_engagement[name] = "waiting"
                if name in state.smoothed:
                    state.last_screen_pos[name] = state.smoothed[name]

        for name, px in current_positions.items():
            eng = state.finger_engagement.get(name, "inactive")
            if eng == "inactive":
                # First contact — grab immediately
                state.finger_engagement[name] = "active"
            elif eng == "waiting":
                last = state.last_screen_pos.get(name)
                if last is None:
                    state.finger_engagement[name] = "active"
                else:
                    dist = np.sqrt((px[0]-last[0])**2 + (px[1]-last[1])**2)
                    if dist <= ENGAGE_RADIUS:
                        # Found the string — re-anchor origin so delta=0 here
                        state.finger_engagement[name] = "active"
                        lm = hand_lm.landmark[FINGER_TIPS[name]]
                        state.c_origin_norm[name] = (lm.x, lm.y, lm.z)
                        # Also update pixel origin so the string line looks right
                        state.c_origin[name] = (px[0], px[1], lm.z)
            # "active" stays active

    # ── Draw ─────────────────────────────────────────────────────────
    if state.is_active:
        draw_waiting_strings(frame, state, current_positions, hand_label)
        draw_strings(frame, state, current_positions, hand_label)
    else:
        draw_calib_dots(frame, state, current_positions, hand_label)

    # ── Compute deltas (ACTIVE fingers only) ─────────────────────────
    deltas = {}
    if state.is_active:
        for name, norm in current_norm.items():
            if (state.finger_engagement.get(name) == "active"
                    and name in state.c_origin_norm):
                ox, oy, oz = state.c_origin_norm[name]
                deltas[name] = (norm[0] - ox, norm[1] - oy, norm[2] - oz)

    return current_positions, deltas


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
    print("Press 'q' to quit, 'r' to reset all origins, SPACE to pause")

    states    = {"Left": TrackerState(), "Right": TrackerState()}
    prev_time = time.time()
    paused    = False

    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"Broadcasting deltas → {UDP_HOST}:{UDP_PORT}")

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

        detected_this_frame = {}
        if results.multi_hand_landmarks and results.multi_handedness:
            for lm_list, handedness in zip(results.multi_hand_landmarks,
                                           results.multi_handedness):
                label = handedness.classification[0].label
                if is_webcam:
                    label = "Right" if label == "Left" else "Left"
                detected_this_frame[label] = lm_list

        packet      = {}
        states_info = {}
        for label, state in states.items():
            hand_key = label.lower()
            if label in detected_this_frame:
                _, deltas = process_hand(frame, detected_this_frame[label],
                                         state, label, w, h)
                for finger, delta in deltas.items():
                    packet[f"{hand_key}_{finger}"] = list(delta)
            else:
                if state.is_active:
                    print(f"Hand lost — resetting. ({label})")
                state.reset()

            for name in FINGER_TIPS:
                key = f"{hand_key}_{name}"
                states_info[key] = state.finger_engagement.get(name, "inactive")

        packet["_states"] = states_info
        udp_sock.sendto(json.dumps(packet).encode(), (UDP_HOST, UDP_PORT))

        now       = time.time()
        fps       = 1.0 / max(now - prev_time, 1e-6)
        prev_time = now
        draw_hud(frame, states, fps)

        cv2.imshow("Digital Marionette Tracker", frame)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord('q'), 27):
            break
        elif key == ord('r'):
            print("Manual reset.")
            for s in states.values():
                s.reset()
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
    parser.add_argument("--threshold", type=float, default=T_THRESHOLD)
    parser.add_argument("--smoothing", type=float, default=SMOOTHING_ALPHA)
    args = parser.parse_args()

    T_THRESHOLD     = args.threshold
    SMOOTHING_ALPHA = args.smoothing

    if args.webcam is not None:
        source = args.webcam
    elif args.video is not None:
        source = args.video
    else:
        source = "/Users/Dhruv/Desktop/GB/Code/pupeteer/pupeteer_test.mov"

    run(source)
