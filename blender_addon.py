"""
Digital Marionette — Blender Addon
Receives finger delta data via UDP and drives mapped Empty objects.
Includes a GPU HUD overlay in the 3D viewport.

Install: Edit > Preferences > Add-ons > Install > select this file > Enable
Panel:   3D Viewport > N panel > Marionette tab
"""

bl_info = {
    "name":        "Digital Marionette",
    "author":      "Digital Marionette Project",
    "version":     (0, 2, 0),
    "blender":     (3, 6, 0),
    "location":    "View3D > Sidebar > Marionette",
    "description": "Real-time hand puppeteering via MediaPipe UDP stream",
    "category":    "Animation",
}

import math
import bpy
import gpu
import blf
import json
import socket
import threading
from gpu_extras.batch import batch_for_shader
from bpy_extras import view3d_utils
from bpy.props import (FloatProperty, IntProperty, PointerProperty,
                       StringProperty)
from bpy.types import Object, Panel, Operator, PropertyGroup

# ── Finger definitions ─────────────────────────────────────────────────────
FINGERS = ["thumb", "index", "middle", "ring", "pinky"]
TRACKED = {"left_index", "left_middle", "right_index", "right_middle"}


# ── Scene properties ────────────────────────────────────────────────────────
class MarionetteProperties(PropertyGroup):
    port: IntProperty(
        name="UDP Port", default=5005, min=1024, max=65535)

    sensitivity: FloatProperty(
        name="Sensitivity", default=1.0, min=0.01, max=10.0, step=10)

    smoothing: FloatProperty(
        name="Smoothing", default=0.2, min=0.0, max=0.99,
        description="0 = instant, 0.99 = very sluggish")

    left_thumb:   PointerProperty(type=Object, name="")
    left_index:   PointerProperty(type=Object, name="")
    left_middle:  PointerProperty(type=Object, name="")
    left_ring:    PointerProperty(type=Object, name="")
    left_pinky:   PointerProperty(type=Object, name="")

    right_thumb:  PointerProperty(type=Object, name="")
    right_index:  PointerProperty(type=Object, name="")
    right_middle: PointerProperty(type=Object, name="")
    right_ring:   PointerProperty(type=Object, name="")
    right_pinky:  PointerProperty(type=Object, name="")


# ── Runtime state ──────────────────────────────────────────────────────────
_runtime = {
    "running":       False,
    "sock":          None,
    "thread":        None,
    "latest":        {},   # {key: [dx,dy,dz]}
    "lock":          threading.Lock(),
    "smooth_pos":    {},   # {key: tuple} — current lerped world position
    "session_base":  {},   # {key: tuple} — empty position on finger re-appear
    "prev_keys":     set(),
    "last_raw":      "",
    "packet_count":  0,
    "finger_states": {},   # {key: "inactive"|"waiting"|"active"}
    "hud_handle":    None,
}


def _listen(sock):
    """Background thread: receive UDP packets, store latest deltas."""
    while _runtime["running"]:
        try:
            data, _ = sock.recvfrom(4096)
            raw = data.decode()
            payload = json.loads(raw)
            with _runtime["lock"]:
                _runtime["latest"]       = payload
                _runtime["last_raw"]     = raw
                _runtime["packet_count"] += 1
        except (OSError, json.JSONDecodeError):
            pass


# ── HUD drawing ─────────────────────────────────────────────────────────────
_hud_shader = None   # lazily initialised inside OpenGL context


def _get_shader():
    global _hud_shader
    if _hud_shader is None:
        _hud_shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    return _hud_shader


def _hud_circle(cx, cy, r, color, filled=False, n=24):
    shader = _get_shader()
    pts = [(cx + r * math.cos(2 * math.pi * i / n),
            cy + r * math.sin(2 * math.pi * i / n))
           for i in range(n)]
    if filled:
        verts = [(cx, cy)] + pts + [pts[0]]
        batch = batch_for_shader(shader, "TRI_FAN", {"pos": verts})
    else:
        verts = pts + [pts[0]]
        batch = batch_for_shader(shader, "LINE_STRIP", {"pos": verts})
    shader.uniform_float("color", color)
    batch.draw(shader)



# State colours (RGBA)
_STATE_COLOR = {
    "active":   (0.20, 0.95, 0.40, 0.90),   # green
    "waiting":  (1.00, 0.55, 0.10, 0.85),   # orange
    "inactive": (0.55, 0.55, 0.55, 0.30),   # dim gray
}

# Friendly short labels for the HUD
_FINGER_LABEL = {
    "left_index":   "L.idx",
    "left_middle":  "L.mid",
    "right_index":  "R.idx",
    "right_middle": "R.mid",
}


def _draw_hud():
    """GPU draw callback — fires every viewport redraw."""
    context = bpy.context
    try:
        region = context.region
        rv3d   = context.region_data
    except Exception:
        return
    if rv3d is None:
        return

    with _runtime["lock"]:
        running   = _runtime["running"]
        pkt_count = _runtime["packet_count"]
        fstates   = dict(_runtime["finger_states"])

    props = getattr(getattr(context, "scene", None), "marionette", None)

    gpu.state.blend_set("ALPHA")
    font_id = 0

    # ── Connection status badge (top-left) ────────────────────────────
    bx, by = 14, region.height - 28
    blf.size(font_id, 14)
    if running and pkt_count > 0:
        badge_color = (0.20, 0.90, 0.40, 1.0)
        label = f"● CONNECTED  ({pkt_count} pkts)"
    elif running:
        badge_color = (1.00, 0.70, 0.10, 1.0)
        label = "● WAITING FOR DATA"
    else:
        badge_color = (0.55, 0.55, 0.55, 0.70)
        label = "● MARIONETTE STOPPED"

    blf.color(font_id, *badge_color)
    blf.position(font_id, bx, by, 0)
    blf.draw(font_id, label)

    # ── Projected empty dots (string anchors) ────────────────────────
    if props:
        for key in ("left_index", "left_middle", "right_index", "right_middle"):
            obj = getattr(props, key, None)
            if obj is None:
                continue

            loc2d = view3d_utils.location_3d_to_region_2d(
                region, rv3d, obj.location)
            if loc2d is None:
                continue

            cx, cy = int(loc2d.x), int(loc2d.y)
            fs     = fstates.get(key, "inactive")
            color  = _STATE_COLOR[fs]
            r      = 11 if fs != "inactive" else 7

            # Filled dot
            _hud_circle(cx, cy, r, color, filled=True)
            # Outer ring (softer)
            _hud_circle(cx, cy, r + 4, (*color[:3], color[3] * 0.35), filled=False)

            # "Waiting" pulse ring to draw attention
            if fs == "waiting":
                _hud_circle(cx, cy, r + 10, (*color[:3], 0.20), filled=False)

            # Label
            lbl = _FINGER_LABEL.get(key, key)
            blf.size(font_id, 11)
            blf.color(font_id, *color[:3], min(color[3] + 0.1, 1.0))
            blf.position(font_id, cx + r + 5, cy - 5, 0)
            blf.draw(font_id, lbl)

    gpu.state.blend_set("NONE")


# ── Modal operator ──────────────────────────────────────────────────────────
class MARIONETTE_OT_start(Operator):
    bl_idname      = "marionette.start"
    bl_label       = "Start"
    bl_description = "Begin listening for hand tracking data"

    def modal(self, context, event):
        if not _runtime["running"]:
            return {"CANCELLED"}

        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        props = context.scene.marionette
        sens  = props.sensitivity
        α     = props.smoothing

        with _runtime["lock"]:
            latest = dict(_runtime["latest"])

        # Extract per-finger engagement states from tracker
        fstates = latest.pop("_states", {})
        _runtime["finger_states"] = fstates

        active_keys = set(latest.keys())
        prev_keys   = _runtime["prev_keys"]

        for hand in ("left", "right"):
            for finger in FINGERS:
                key = f"{hand}_{finger}"
                obj = getattr(props, key, None)
                if obj is None:
                    continue

                cur = _runtime["smooth_pos"].get(key, tuple(obj.location))

                if key in active_keys:
                    if key not in prev_keys:
                        # Finger just re-appeared — anchor here so no snap
                        _runtime["session_base"][key] = cur

                    base       = _runtime["session_base"][key]
                    dx, dy, dz = latest[key]
                    target = (
                        base[0] + dx * sens,
                        base[1] - dz * sens,
                        base[2] - dy * sens,
                    )
                else:
                    # No data → hold current position
                    target = cur

                new_pos = (
                    cur[0] * α + target[0] * (1 - α),
                    cur[1] * α + target[1] * (1 - α),
                    cur[2] * α + target[2] * (1 - α),
                )
                _runtime["smooth_pos"][key] = new_pos
                obj.location = new_pos

        _runtime["prev_keys"] = active_keys

        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()

        return {"PASS_THROUGH"}

    def execute(self, context):
        if _runtime["running"]:
            self.report({"WARNING"}, "Already running")
            return {"CANCELLED"}

        props = context.scene.marionette

        _runtime["smooth_pos"].clear()
        _runtime["session_base"].clear()
        _runtime["prev_keys"] = set()
        _runtime["finger_states"] = {}
        for hand in ("left", "right"):
            for finger in FINGERS:
                key = f"{hand}_{finger}"
                obj = getattr(props, key, None)
                if obj:
                    _runtime["smooth_pos"][key] = tuple(obj.location)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.5)
        try:
            sock.bind(("0.0.0.0", props.port))
        except OSError as e:
            self.report({"ERROR"}, f"Cannot bind port {props.port}: {e}")
            sock.close()
            return {"CANCELLED"}

        _runtime["running"] = True
        _runtime["sock"]    = sock
        _runtime["latest"]  = {}

        t = threading.Thread(target=_listen, args=(sock,), daemon=True)
        t.start()
        _runtime["thread"] = t

        # Register HUD draw callback
        handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_hud, (), "WINDOW", "POST_PIXEL")
        _runtime["hud_handle"] = handle

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.016, window=context.window)
        wm.modal_handler_add(self)

        self.report({"INFO"}, f"Marionette listening on port {props.port}")
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        context.window_manager.event_timer_remove(self._timer)
        _stop_runtime()


class MARIONETTE_OT_stop(Operator):
    bl_idname      = "marionette.stop"
    bl_label       = "Stop"
    bl_description = "Stop listening for hand tracking data"

    def execute(self, context):
        _stop_runtime()
        self.report({"INFO"}, "Marionette stopped")
        return {"FINISHED"}


class MARIONETTE_OT_listen(Operator):
    """One-shot debug: capture next UDP packet."""
    bl_idname      = "marionette.listen"
    bl_label       = "Listen Once"
    bl_description = "Open port 5005 briefly and capture one packet for debugging"

    def execute(self, context):
        if _runtime["running"]:
            with _runtime["lock"]:
                raw   = _runtime["last_raw"]
                count = _runtime["packet_count"]
            if raw:
                self.report({"INFO"}, f"[{count} pkts] Last: {raw[:120]}")
            else:
                self.report({"WARNING"},
                            "Running but no packets received yet")
            return {"FINISHED"}

        props = context.scene.marionette
        sock  = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(2.0)
        try:
            sock.bind(("0.0.0.0", props.port))
        except OSError as e:
            self.report({"ERROR"}, f"Cannot bind port {props.port}: {e}")
            sock.close()
            return {"CANCELLED"}

        try:
            data, addr = sock.recvfrom(4096)
            raw = data.decode()
            _runtime["last_raw"]     = raw
            _runtime["packet_count"] += 1
            self.report({"INFO"}, f"Got packet from {addr}: {raw[:120]}")
        except socket.timeout:
            _runtime["last_raw"] = ""
            self.report({"WARNING"}, "No packet in 2 s — is hand_tracker.py running?")
        finally:
            sock.close()

        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
        return {"FINISHED"}


def _stop_runtime():
    _runtime["running"] = False
    if _runtime.get("hud_handle"):
        bpy.types.SpaceView3D.draw_handler_remove(
            _runtime["hud_handle"], "WINDOW")
        _runtime["hud_handle"] = None
    if _runtime["sock"]:
        try:
            _runtime["sock"].close()
        except OSError:
            pass
        _runtime["sock"] = None
    if _runtime["thread"]:
        _runtime["thread"].join(timeout=1.0)
        _runtime["thread"] = None


# ── UI Panel ────────────────────────────────────────────────────────────────
class MARIONETTE_PT_main(Panel):
    bl_label       = "Marionette"
    bl_idname      = "MARIONETTE_PT_main"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "Marionette"

    def draw(self, context):
        try:
            self._draw(context)
        except Exception as e:
            self.layout.label(text=f"Panel error: {e}", icon="ERROR")

    def _draw(self, context):
        layout = self.layout
        props  = context.scene.marionette
        active = _runtime["running"]

        # ── Toggle button ───────────────────────────────────────────
        btn_row = layout.row()
        btn_row.scale_y = 1.8
        if active:
            btn_row.alert = True
            btn_row.operator("marionette.stop", text="  Stop", icon="CANCEL")
        else:
            btn_row.operator("marionette.start", text="  Start", icon="PLAY")

        layout.separator()

        # ── Settings ─────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Settings", icon="PREFERENCES")
        col = box.column(align=True)
        col.prop(props, "port")
        col.prop(props, "sensitivity", slider=True)
        col.prop(props, "smoothing",   slider=True)

        layout.separator()

        # ── Finger mapping ───────────────────────────────────────────
        box = layout.box()
        box.label(text="Finger Mapping", icon="HAND")

        header = box.row()
        header.column().label(text="← LEFT HAND")
        header.column().label(text="RIGHT HAND →")
        box.separator(factor=0.3)

        for finger in FINGERS:
            key_l    = f"left_{finger}"
            key_r    = f"right_{finger}"
            tracked_l = key_l in TRACKED
            tracked_r = key_r in TRACKED
            lbl       = finger.capitalize()

            row = box.row(align=False)

            left_col = row.column(align=True)
            left_row = left_col.row(align=True)
            left_row.enabled = tracked_l
            left_row.label(text=lbl)
            left_row.prop(props, key_l, icon="OBJECT_DATA")

            row.separator(factor=0.8)

            right_col = row.column(align=True)
            right_row = right_col.row(align=True)
            right_row.enabled = tracked_r
            right_row.prop(props, key_r, icon="OBJECT_DATA")
            right_row.label(text=lbl)

        box.separator(factor=0.3)
        box.label(text="Dimmed = not tracked by script", icon="INFO")

        layout.separator()

        # ── Debug ────────────────────────────────────────────────────
        dbox = layout.box()
        dbox.label(text="Debug", icon="CONSOLE")
        with _runtime["lock"]:
            raw   = _runtime["last_raw"]
            count = _runtime["packet_count"]

        dbox.operator("marionette.listen", text="Listen on port 5005",
                      icon="TRIA_DOWN")

        if count:
            dbox.label(text=f"Packets received: {count}")
            try:
                parsed = json.loads(raw)
                keys   = [k for k in parsed.keys() if not k.startswith("_")]
                dbox.label(text="Active: " + (", ".join(keys) if keys
                           else "(none)"))
                for k, v in list(parsed.items())[:2]:
                    if not k.startswith("_"):
                        dbox.label(text=f"  {k}: [{v[0]:.3f}, {v[1]:.3f}, {v[2]:.3f}]")
            except Exception:
                dbox.label(text=raw[:80])
        else:
            dbox.label(text="No packets yet", icon="ERROR")


# ── Registration ─────────────────────────────────────────────────────────────
CLASSES = [
    MarionetteProperties,
    MARIONETTE_OT_start,
    MARIONETTE_OT_stop,
    MARIONETTE_OT_listen,
    MARIONETTE_PT_main,
]


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.marionette = PointerProperty(type=MarionetteProperties)


def unregister():
    _stop_runtime()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.marionette


if __name__ == "__main__":
    register()
