#!/usr/bin/env python3
"""
Digital Marionette — Hand Tracker (Step 1: Detection & Visualization)

Tracks thumb/index/middle fingertips per hand via MediaPipe.
State machine per hand: NO_HAND → CALIBRATING → ACTIVE
Hands must be within the central detection zone to be tracked.
Webcam is mirrored for natural interaction.

Usage:
    python hand_tracker.py                  # use test video
    python hand_tracker.py --webcam 0       # use webcam (mirrored)
"""

import argparse
import time
import cv2
import mediapipe as mp
import numpy as np

# ── Config ───────────────────────────────────────────────────────────────────
T_THRESHOLD      = 0.5   # seconds hand must be still before locking origin
SMOOTHING_ALPHA  = 0.8   # 0→sluggish, 1→raw
STILLNESS_RADIUS = 40    # pixels — max wrist drift during calibration
ZONE_FRAC        = 0.70  # fraction of screen for detection zone (centred)

# Only track these three fingers
FINGER_TIPS = {"thumb": 4, "index": 8, "middle": 12}
FINGER_PIP  = {"thumb": 3, "index": 6, "middle": 10}

# Marker sizes
CALIB_RADIUS = 22   # calibration progress dot
TIP_RADIUS   = 16   # active fingertip dot
ORIGIN_RADIUS = 18  # locked origin dot

# Per-hand colours (BGR): Left=orange, Right=green
HAND_COLORS = {
    "Left":  {"origin": (0, 140, 255), "string": (0, 200, 255), "calib": (0, 160, 255), "tip": (0, 180, 255)},
    "Right": {"origin": (50, 220, 50), "string": (200, 255, 200), "calib": (0, 220, 100), "tip": (80, 255, 80)},
}
COLOR_ACTIVE = (0, 255, 0)
COLOR_CALIB  = (0, 200, 255)
COLOR_NOHAND = (60, 60, 60)
COLOR_DELTA  = (255, 200, 0)


# ── State ────────────────────────────────────────────────────────────────────
class TrackerState:
    def __init__(self):
        self.smoothed = {}
        self.reset()

    def reset(self):
        self.hand_start_time = None
        self.anchor_px       = None
        self.is_active       = False
        self.c_origin        = {}
        self.c_origin_norm   = {}

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
    """Return names of fingers whose tip is farther from wrist than its PIP (3D)."""
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


def get_zone(w, h):
    """Return (x1, y1, x2, y2) of the centred detection zone."""
    margin_x = int(w * (1 - ZONE_FRAC) / 2)
    margin_y = int(h * (1 - ZONE_FRAC) / 2)
    return margin_x, margin_y, w - margin_x, h - margin_y


def px_in_zone(px, w, h):
    """True if a pixel position is inside the detection zone."""
    x1, y1, x2, y2 = get_zone(w, h)
    return x1 <= px[0] <= x2 and y1 <= px[1] <= y2


# ── Drawing ──────────────────────────────────────────────────────────────────
def draw_zone(frame, w, h, any_active):
    """Draw the detection zone rectangle."""
    x1, y1, x2, y2 = get_zone(w, h)
    color     = (0, 200, 80) if any_active else (100, 100, 100)
    thickness = 2 if any_active else 1

    overlay = frame.copy()
    cv2.rectangle(overlay, (x1, y1), (x2, y2), color, -1)
    cv2.addWeighted(overlay, 0.04, frame, 0.96, 0, frame)
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, thickness)

    label = "DETECTION ZONE"
    cv2.putText(frame, label, (x1 + 8, y1 + 22),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1)


def draw_calib_dots(frame, state, current_positions, hand_label):
    """Ghost ring per fingertip that fills with colour as calibration progresses."""
    if state.status != "CALIBRATING" or not current_positions:
        return
    elapsed  = time.time() - state.hand_start_time
    progress = min(elapsed / T_THRESHOLD, 1.0)
    color    = HAND_COLORS[hand_label]["calib"]

    for px in current_positions.values():
        # ghost ring always visible
        overlay = frame.copy()
        cv2.circle(overlay, px, CALIB_RADIUS, color, 2)
        cv2.addWeighted(overlay, 0.18, frame, 0.82, 0, frame)

        # filled dot fades in with progress
        if progress > 0:
            overlay2 = frame.copy()
            cv2.circle(overlay2, px, CALIB_RADIUS, color, -1)
            cv2.addWeighted(overlay2, progress * 0.80, frame, 1 - progress * 0.80, 0, frame)


def draw_strings(frame, state, current_positions, hand_label):
    """Origin dots + string lines + current fingertip dots."""
    colors = HAND_COLORS[hand_label]
    for name, origin_px in state.c_origin.items():
        ox, oy = origin_px[0], origin_px[1]

        # origin dot (always drawn while active)
        cv2.circle(frame, (ox, oy), ORIGIN_RADIUS, colors["origin"], 2)
        cv2.circle(frame, (ox, oy), 4, colors["origin"], -1)

        if name not in current_positions:
            continue
        cur = current_positions[name]

        # current fingertip dot
        cv2.circle(frame, cur, TIP_RADIUS, colors["tip"], -1)
        cv2.circle(frame, cur, TIP_RADIUS + 2, colors["tip"], 1)

        # string line
        cv2.line(frame, (ox, oy), cur, colors["string"], 2)

        # delta label at midpoint
        dx, dy = cur[0] - ox, cur[1] - oy
        dist   = np.sqrt(dx*dx + dy*dy)
        mid    = ((ox + cur[0]) // 2, (oy + cur[1]) // 2)
        cv2.putText(frame, f"{name} {dist:.0f}px", (mid[0]+5, mid[1]-5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, COLOR_DELTA, 1)


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

    current_positions = {}
    for name, idx in FINGER_TIPS.items():
        lm     = hand_lm.landmark[idx]
        raw_px = landmark_to_pixel(lm, w, h)
        px     = (lerp_point(state.smoothed[name], raw_px, SMOOTHING_ALPHA)
                  if name in state.smoothed else raw_px)
        state.smoothed[name] = px
        if name in extended and px_in_zone(px, w, h):
            current_positions[name] = px

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

    elapsed = time.time() - state.hand_start_time
    if elapsed >= T_THRESHOLD and not state.is_active:
        for name, px in current_positions.items():
            lm = hand_lm.landmark[FINGER_TIPS[name]]
            state.c_origin[name]      = (px[0], px[1], lm.z)
            state.c_origin_norm[name] = (lm.x, lm.y, lm.z)
        state.is_active = True
        print(f"Origin locked! ({hand_label})")

    if state.is_active:
        draw_strings(frame, state, current_positions, hand_label)
    else:
        draw_calib_dots(frame, state, current_positions, hand_label)

    return current_positions


# ── Main loop ────────────────────────────────────────────────────────────────
def run(source):
    is_webcam = isinstance(source, int)
    is_video  = isinstance(source, str)

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

    while cap.isOpened():
        if not paused:
            ok, frame = cap.read()
            if not ok:
                break

        # Mirror webcam so it feels like looking in a mirror
        if is_webcam:
            frame = cv2.flip(frame, 1)

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False
        results = hands.process(rgb)

        # Build map: label → landmarks, filtered to detection zone
        detected_this_frame = {}
        if results.multi_hand_landmarks and results.multi_handedness:
            for lm_list, handedness in zip(results.multi_hand_landmarks,
                                           results.multi_handedness):
                label = handedness.classification[0].label
                # For mirrored webcam, MediaPipe's Left/Right are swapped visually
                if is_webcam:
                    label = "Right" if label == "Left" else "Left"
                detected_this_frame[label] = lm_list

        # Process / reset each hand
        for label, state in states.items():
            if label in detected_this_frame:
                process_hand(frame, detected_this_frame[label], state, label, w, h)
            else:
                if state.is_active:
                    print(f"Hand lost — resetting. ({label})")
                state.reset()

        # Detection zone overlay
        any_active = any(s.is_active for s in states.values())
        draw_zone(frame, w, h, any_active)

        # HUD
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
        elif key == ord(' ') and is_video:
            paused = not paused
            print("Paused." if paused else "Resumed.")

    cap.release()
    cv2.destroyAllWindows()
    hands.close()
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
