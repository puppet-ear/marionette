"""
Digital Marionette — Blender Addon
Receives absolute normalised fingertip positions via UDP and maps them
directly to Empty world coordinates.

Mapping (MediaPipe → Blender):
    finger_x  (0=left,  1=right) → Blender  X  centred at 0
    finger_y  (0=top,   1=bot)   → Blender -Z  (image Y flipped)
    finger_z  (depth, ~-0.1–0.1) → Blender  Y  (toward/away camera)

Scale property controls how many Blender units = full finger travel.

Install: Edit > Preferences > Add-ons > Install > select this file > Enable
Panel:   3D Viewport > N panel > Marionette tab
"""

bl_info = {
    "name":        "Digital Marionette",
    "author":      "Digital Marionette Project",
    "version":     (0, 3, 0),
    "blender":     (3, 6, 0),
    "location":    "View3D > Sidebar > Marionette",
    "description": "Real-time hand puppeteering via MediaPipe UDP stream",
    "category":    "Animation",
}

import bpy
import json
import socket
import threading
from bpy.props import (FloatProperty, IntProperty, PointerProperty)
from bpy.types import Object, Panel, Operator, PropertyGroup

# ── Finger definitions ─────────────────────────────────────────────────────
FINGERS = ["thumb", "index", "middle", "ring", "pinky"]
TRACKED = {"left_index", "left_middle", "right_index", "right_middle"}


# ── Scene properties ────────────────────────────────────────────────────────
class MarionetteProperties(PropertyGroup):
    port: IntProperty(
        name="UDP Port", default=5005, min=1024, max=65535)

    scale: FloatProperty(
        name="Scale", default=3.0, min=0.1, max=20.0, step=10,
        description="Blender units covered by full finger travel (0→1)")

    smoothing: FloatProperty(
        name="Smoothing", default=0.15, min=0.0, max=0.99,
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
    "running":      False,
    "sock":         None,
    "thread":       None,
    "latest":       {},   # {key: [x, y, z]}  absolute normalised
    "lock":         threading.Lock(),
    "smooth_pos":   {},   # {key: tuple}  current lerped world position
    "last_raw":     "",
    "packet_count": 0,
}


def _listen(sock):
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


def _norm_to_blender(x, y, z, scale):
    """Map MediaPipe normalised coords to Blender world offset."""
    bx =  (x - 0.5) * scale          # left–right
    by = -(z        * scale * 0.4)   # depth (z range is small, ~±0.1)
    bz = -(y - 0.5) * scale          # up–down (image Y is flipped)
    return (bx, by, bz)


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
        scale = props.scale
        α     = props.smoothing

        with _runtime["lock"]:
            latest = dict(_runtime["latest"])

        for hand in ("left", "right"):
            for finger in FINGERS:
                key = f"{hand}_{finger}"
                obj = getattr(props, key, None)
                if obj is None:
                    continue

                cur = _runtime["smooth_pos"].get(key, tuple(obj.location))

                if key in latest:
                    x, y, z = latest[key]
                    target = _norm_to_blender(x, y, z, scale)
                else:
                    target = cur   # hold last position

                new_pos = (
                    cur[0] * α + target[0] * (1 - α),
                    cur[1] * α + target[1] * (1 - α),
                    cur[2] * α + target[2] * (1 - α),
                )
                _runtime["smooth_pos"][key] = new_pos
                obj.location = new_pos

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
    bl_idname      = "marionette.listen"
    bl_label       = "Listen Once"
    bl_description = "Open port briefly and capture one packet for debugging"

    def execute(self, context):
        if _runtime["running"]:
            with _runtime["lock"]:
                raw   = _runtime["last_raw"]
                count = _runtime["packet_count"]
            if raw:
                self.report({"INFO"}, f"[{count} pkts] Last: {raw[:120]}")
            else:
                self.report({"WARNING"}, "Running but no packets yet")
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
            self.report({"WARNING"}, "No packet in 2s — is hand_tracker.py running?")
        finally:
            sock.close()

        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
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
        try:
            self._draw(context)
        except Exception as e:
            self.layout.label(text=f"Panel error: {e}", icon="ERROR")

    def _draw(self, context):
        layout = self.layout
        props  = context.scene.marionette
        active = _runtime["running"]

        btn_row = layout.row()
        btn_row.scale_y = 1.8
        if active:
            btn_row.alert = True
            btn_row.operator("marionette.stop", text="  Stop", icon="CANCEL")
        else:
            btn_row.operator("marionette.start", text="  Start", icon="PLAY")

        layout.separator()

        box = layout.box()
        box.label(text="Settings", icon="PREFERENCES")
        col = box.column(align=True)
        col.prop(props, "port")
        col.prop(props, "scale",     slider=True)
        col.prop(props, "smoothing", slider=True)

        layout.separator()

        box = layout.box()
        box.label(text="Finger Mapping", icon="HAND")

        header = box.row()
        header.column().label(text="← LEFT HAND")
        header.column().label(text="RIGHT HAND →")
        box.separator(factor=0.3)

        for finger in FINGERS:
            key_l     = f"left_{finger}"
            key_r     = f"right_{finger}"
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

        dbox = layout.box()
        dbox.label(text="Debug", icon="CONSOLE")
        with _runtime["lock"]:
            raw   = _runtime["last_raw"]
            count = _runtime["packet_count"]

        dbox.operator("marionette.listen", text="Listen on port 5005",
                      icon="TRIA_DOWN")

        if count:
            dbox.label(text=f"Packets: {count}")
            try:
                parsed = json.loads(raw)
                keys   = list(parsed.keys())
                dbox.label(text="Keys: " + (", ".join(keys) if keys
                           else "(none — no extended fingers)"))
                for k, v in list(parsed.items())[:2]:
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
