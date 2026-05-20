# Marionette

Real-time hand puppeteering for Blender. A MediaPipe tracker watches your hands through a webcam (or video file) and streams fingertip positions over UDP to a Blender addon, which drives Empty objects in the 3D viewport in real time.

## Components

- **`hand_tracker.py`** — Python script using MediaPipe + OpenCV. Detects up to two hands, identifies which fingers are extended, and broadcasts normalized fingertip positions as JSON over UDP.
- **`blender_addon.py`** — Blender addon. Listens on the UDP port, maps incoming positions into Blender world space, and updates 10 user-assigned Empty objects (thumb / index / middle / ring / pinky × left / right) every frame.

## How it works

```
webcam ──► hand_tracker.py ──UDP:5005──► blender_addon.py ──► Empty objects
          (MediaPipe)        (JSON)       (modal operator)
```

The tracker sends absolute normalized coordinates `[0,1]` per fingertip. The addon maps them directly to Blender world coordinates:

| MediaPipe axis | Blender axis | Notes |
|---|---|---|
| `x` (0=left, 1=right) | `+X` | centered at 0 |
| `y` (0=top, 1=bottom) | `-Z` | image Y is flipped |
| `z` (depth) | `+Y` | toward/away from camera, small range |

Scale and smoothing are tunable from the addon panel.

## Usage

**1. Tracker** — needs Python with `mediapipe` and `opencv-python`:

```bash
python hand_tracker.py --webcam 0      # live webcam
python hand_tracker.py --video path/to/clip.mov
```

**2. Blender addon** — Blender 3.6+:

1. `Edit > Preferences > Add-ons > Install` and pick `blender_addon.py`.
2. Open the `Marionette` tab in the 3D viewport sidebar (press `N`).
3. Assign an Empty to each finger slot you want to drive.
4. Press **Start**.

The **Debug** section shows packet count and the latest payload — useful for confirming the tracker is reaching Blender.

## Requirements

- Python 3.x with `mediapipe`, `opencv-python`
- Blender 3.6+
- Tracker and Blender on the same machine (UDP defaults to `127.0.0.1:5005`)
