"""
Digital Marionette — Blender Addon
Receives finger delta data via UDP and drives mapped Empty objects.

Install: Edit > Preferences > Add-ons > Install > select this file > Enable
Panel:   3D Viewport > N panel > Marionette tab
"""

bl_info = {
    "name":        "Digital Marionette",
    "author":      "Digital Marionette Project",
    "version":     (0, 1, 0),
    "blender":     (3, 6, 0),
    "location":    "View3D > Sidebar > Marionette",
    "description": "Real-time hand puppeteering via MediaPipe UDP stream",
    "category":    "Animation",
}

import bpy
import json
import socket
import threading
from bpy.props import (FloatProperty, IntProperty, PointerProperty,
                       StringProperty)
from bpy.types import Object, Panel, Operator, PropertyGroup

# ── Finger definitions ─────────────────────────────────────────────────────
# Order within each hand, thumb→pinky, for display layout
FINGERS = ["thumb", "index", "middle", "ring", "pinky"]

HAND_LABELS = {
    "left":  "Left Hand",
    "right": "Right Hand",
}

# Fingers actually sent by the tracker (subset — used to dim unmapped slots)
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

    # 10 finger → Empty mappings
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


# ── Runtime state (module-level, not stored in blend file) ──────────────────
_runtime = {
    "running":     False,
    "sock":        None,
    "thread":      None,
    "latest":      {},   # {key: [dx,dy,dz]}
    "lock":        threading.Lock(),
    "rest_pos":    {},   # {key: Vector} — captured when operator starts
    "smooth_pos":  {},   # {key: Vector} — current lerped position
}


def _listen(sock):
    """Background thread: receive UDP packets, store latest deltas."""
    while _runtime["running"]:
        try:
            data, _ = sock.recvfrom(4096)
            payload = json.loads(data.decode())
            with _runtime["lock"]:
                _runtime["latest"] = payload
        except (OSError, json.JSONDecodeError):
            pass


# ── Modal operator ──────────────────────────────────────────────────────────
class MARIONETTE_OT_start(Operator):
    bl_idname  = "marionette.start"
    bl_label   = "Start"
    bl_description = "Begin listening for hand tracking data"

    def modal(self, context, event):
        if not _runtime["running"]:
            return {"CANCELLED"}

        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        props = context.scene.marionette
        sens  = props.sensitivity
        α     = props.smoothing  # lerp weight toward old position

        with _runtime["lock"]:
            latest = dict(_runtime["latest"])

        for hand in ("left", "right"):
            for finger in FINGERS:
                key   = f"{hand}_{finger}"
                prop  = key  # matches PropertyGroup attribute names
                obj   = getattr(props, prop, None)
                if obj is None:
                    continue

                rest = _runtime["rest_pos"].get(key)
                if rest is None:
                    continue

                if key in latest:
                    dx, dy, dz = latest[key]
                    # Image x → Blender X, image y (down=+) → Blender -Z,
                    # MediaPipe depth z → Blender Y (toward/away camera)
                    target = (
                        rest[0] + dx * sens,
                        rest[1] - dz * sens,   # depth
                        rest[2] - dy * sens,   # image-y inverted → world Z
                    )
                else:
                    # No data for this finger — hold rest position
                    target = rest

                cur = _runtime["smooth_pos"].get(key, rest)
                new_pos = (
                    cur[0] * α + target[0] * (1 - α),
                    cur[1] * α + target[1] * (1 - α),
                    cur[2] * α + target[2] * (1 - α),
                )
                _runtime["smooth_pos"][key] = new_pos
                obj.location = new_pos

        # Trigger viewport redraw
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()

        return {"PASS_THROUGH"}

    def execute(self, context):
        if _runtime["running"]:
            self.report({"WARNING"}, "Already running")
            return {"CANCELLED"}

        props = context.scene.marionette

        # Capture rest positions of all mapped empties
        _runtime["rest_pos"].clear()
        _runtime["smooth_pos"].clear()
        for hand in ("left", "right"):
            for finger in FINGERS:
                key = f"{hand}_{finger}"
                obj = getattr(props, key, None)
                if obj:
                    pos = tuple(obj.location)
                    _runtime["rest_pos"][key]   = pos
                    _runtime["smooth_pos"][key] = pos

        # Open UDP socket
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

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.016, window=context.window)  # ~60fps
        wm.modal_handler_add(self)

        self.report({"INFO"}, f"Marionette listening on port {props.port}")
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        context.window_manager.event_timer_remove(self._timer)
        _stop_runtime()


class MARIONETTE_OT_stop(Operator):
    bl_idname  = "marionette.stop"
    bl_label   = "Stop"
    bl_description = "Stop listening for hand tracking data"

    def execute(self, context):
        _stop_runtime()
        self.report({"INFO"}, "Marionette stopped")
        return {"FINISHED"}


def _stop_runtime():
    _runtime["running"] = False
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
        layout = context.layout
        props  = context.scene.marionette
        active = _runtime["running"]

        # ── Transport ──────────────────────────────────────────────────
        row = layout.row(align=True)
        row.scale_y = 1.4
        start = row.operator("marionette.start", icon="PLAY")
        stop  = row.operator("marionette.stop",  icon="SNAP_FACE")
        row.enabled = True

        sub = layout.row()
        sub.alert = active
        sub.label(text="● LIVE" if active else "○ Stopped",
                  icon="RADIOBUT_ON" if active else "RADIOBUT_OFF")

        layout.separator()

        # ── Settings ───────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Settings", icon="SETTINGS")
        col = box.column(align=True)
        col.prop(props, "port")
        col.prop(props, "sensitivity", slider=True)
        col.prop(props, "smoothing",   slider=True)

        layout.separator()

        # ── Finger mapping — two-hand graphic ──────────────────────────
        box = layout.box()
        box.label(text="Finger Mapping", icon="HAND")

        # Column headers
        header = box.row()
        header.label(text="LEFT HAND",  icon="TRIA_LEFT")
        header.label(text="RIGHT HAND", icon="TRIA_RIGHT")

        box.separator(factor=0.4)

        # One row per finger, left column | right column
        ICONS = {
            "thumb":  "TRIA_RIGHT",
            "index":  "LAYER_ACTIVE",
            "middle": "LAYER_ACTIVE",
            "ring":   "LAYER_USED",
            "pinky":  "LAYER_USED",
        }

        for finger in FINGERS:
            row = box.row(align=False)

            # ── Left side ──
            left_col = row.column(align=True)
            left_col.scale_x = 1.0
            key_l = f"left_{finger}"
            tracked_l = key_l in TRACKED
            sub_l = left_col.row(align=True)
            sub_l.alert = False
            if not tracked_l:
                sub_l.enabled = False   # dim untracked fingers
            lbl = finger.capitalize()
            sub_l.label(text=lbl)
            sub_l.prop(props, key_l, icon="OBJECT_DATA")

            # ── Divider ──
            row.separator(factor=1.5)

            # ── Right side ──
            right_col = row.column(align=True)
            key_r = f"right_{finger}"
            tracked_r = key_r in TRACKED
            sub_r = right_col.row(align=True)
            if not tracked_r:
                sub_r.enabled = False
            sub_r.prop(props, key_r, icon="OBJECT_DATA")
            sub_r.label(text=lbl)

        box.separator(factor=0.4)
        note = box.row()
        note.label(text="Greyed = not sent by tracker yet", icon="INFO")


# ── Registration ─────────────────────────────────────────────────────────────
CLASSES = [
    MarionetteProperties,
    MARIONETTE_OT_start,
    MARIONETTE_OT_stop,
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
