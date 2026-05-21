"""
Marionette — Blender OSC Listener

Receives /empty/<name> x y z over UDP (OSC format) and moves
Empty objects whose names match <name>.

No manual finger-to-Empty mapping required — just name your Empties
"left_index", "right_thumb", etc. and they move automatically.

Install: Edit > Preferences > Add-ons > Install > select this file > Enable
Panel:   3D Viewport > N panel > Marionette tab

Stack:
    hand_tracker.html  (browser)
        → WebSocket
            → relay.py
                → OSC UDP :7700
                    → this listener
                        → Empties
"""

bl_info = {
    "name":        "Marionette",
    "author":      "Marionette Project",
    "version":     (1, 0, 0),
    "blender":     (3, 6, 0),
    "location":    "View3D > Sidebar > Marionette",
    "description": "Real-time hand puppeteering via OSC",
    "category":    "Animation",
}

import bpy
import socket
import struct
import threading
from bpy.props import FloatProperty, IntProperty, PointerProperty
from bpy.types import Panel, Operator, PropertyGroup


# ── Minimal OSC parser (no external dependencies) ──────────────────────────────

def _read_str(data, offset):
    """Read a null-terminated, 4-byte-padded OSC string."""
    end = data.index(0, offset)
    s   = data[offset:end].decode("utf-8", errors="replace")
    # Advance to the next 4-byte boundary after the null terminator
    return s, offset + ((end - offset + 4) // 4 * 4)


def parse_osc(data):
    """Parse one OSC message. Returns (address, args) or None on failure."""
    try:
        addr, off = _read_str(data, 0)
        if not addr.startswith("/"):
            return None

        # Type tag string is optional
        if off >= len(data) or data[off:off + 1] != b",":
            return addr, []

        tags, off = _read_str(data, off)
        tags = tags[1:]  # strip leading ","

        args = []
        for t in tags:
            if t == "f":
                args.append(struct.unpack_from(">f", data, off)[0])
                off += 4
            elif t == "i":
                args.append(struct.unpack_from(">i", data, off)[0])
                off += 4
        return addr, args
    except Exception:
        return None


# ── Runtime state ──────────────────────────────────────────────────────────────

_rt = {
    "running": False,
    "sock":    None,
    "thread":  None,
    "latest":  {},          # {name: [x, y, z]}
    "lock":    threading.Lock(),
    "smooth":  {},          # {name: (bx, by, bz)} current lerped world pos
    "count":   0,
    "last":    "",
}


def _listen(sock):
    while _rt["running"]:
        try:
            data, _ = sock.recvfrom(4096)
            result  = parse_osc(data)
            if result is None:
                continue
            addr, args = result
            # Accept /empty/<name> with at least 3 float args
            if addr.startswith("/empty/") and len(args) >= 3:
                name = addr[7:]
                with _rt["lock"]:
                    _rt["latest"][name]  = args[:3]
                    _rt["count"]        += 1
                    _rt["last"]          = f"{addr}  [{args[0]:.3f}  {args[1]:.3f}  {args[2]:.3f}]"
        except OSError:
            pass


def _norm_to_blender(x, y, z, scale):
    """Map MediaPipe normalised coords → Blender world offset."""
    bx =  (x - 0.5) * scale
    by = -(z * scale * 0.4)
    bz = -(y - 0.5) * scale
    return (bx, by, bz)


def _stop():
    _rt["running"] = False
    if _rt["sock"]:
        try:
            _rt["sock"].close()
        except OSError:
            pass
        _rt["sock"] = None
    if _rt["thread"]:
        _rt["thread"].join(timeout=1.0)
        _rt["thread"] = None


# ── Operators ──────────────────────────────────────────────────────────────────

class MARIONETTE_OT_start(Operator):
    bl_idname      = "marionette.start"
    bl_label       = "Start"
    bl_description = "Begin listening for OSC hand data"

    def modal(self, context, event):
        if not _rt["running"]:
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        props = context.scene.marionette
        α     = props.smoothing

        with _rt["lock"]:
            latest = dict(_rt["latest"])

        for name, (x, y, z) in latest.items():
            obj = context.scene.objects.get(name)
            if obj is None:
                continue
            cur    = _rt["smooth"].get(name, tuple(obj.location))
            target = _norm_to_blender(x, y, z, props.scale)
            new_pos = tuple(cur[i] * α + target[i] * (1 - α) for i in range(3))
            _rt["smooth"][name] = new_pos
            obj.location = new_pos

        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()

        return {"PASS_THROUGH"}

    def execute(self, context):
        if _rt["running"]:
            self.report({"WARNING"}, "Already running")
            return {"CANCELLED"}

        props = context.scene.marionette
        sock  = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.5)
        try:
            sock.bind(("0.0.0.0", props.port))
        except OSError as e:
            self.report({"ERROR"}, f"Cannot bind port {props.port}: {e}")
            sock.close()
            return {"CANCELLED"}

        _rt.update(running=True, sock=sock, latest={}, smooth={}, count=0, last="")
        t = threading.Thread(target=_listen, args=(sock,), daemon=True)
        t.start()
        _rt["thread"] = t

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.016, window=context.window)
        wm.modal_handler_add(self)

        self.report({"INFO"}, f"Marionette listening on OSC port {props.port}")
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        context.window_manager.event_timer_remove(self._timer)
        _stop()


class MARIONETTE_OT_stop(Operator):
    bl_idname      = "marionette.stop"
    bl_label       = "Stop"
    bl_description = "Stop listening"

    def execute(self, context):
        _stop()
        self.report({"INFO"}, "Marionette stopped")
        return {"FINISHED"}


# ── Properties ─────────────────────────────────────────────────────────────────

class MarionetteProperties(PropertyGroup):
    port: IntProperty(
        name="OSC Port", default=7700, min=1024, max=65535)

    scale: FloatProperty(
        name="Scale", default=3.0, min=0.1, max=20.0, step=10,
        description="Blender units for full finger travel (0→1)")

    smoothing: FloatProperty(
        name="Smoothing", default=0.15, min=0.0, max=0.99,
        description="0 = instant, 0.99 = very sluggish")


# ── Panel ──────────────────────────────────────────────────────────────────────

class MARIONETTE_PT_main(Panel):
    bl_label       = "Marionette"
    bl_idname      = "MARIONETTE_PT_main"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "Marionette"

    def draw(self, context):
        layout = self.layout
        props  = context.scene.marionette
        active = _rt["running"]

        row = layout.row()
        row.scale_y = 1.8
        if active:
            row.alert = True
            row.operator("marionette.stop", text="Stop", icon="CANCEL")
        else:
            row.operator("marionette.start", text="Start", icon="PLAY")

        layout.separator()

        box = layout.box()
        box.label(text="Settings", icon="PREFERENCES")
        col = box.column(align=True)
        col.prop(props, "port")
        col.prop(props, "scale",     slider=True)
        col.prop(props, "smoothing", slider=True)

        layout.separator()

        box = layout.box()
        box.label(text="Contract", icon="INFO")
        box.label(text="/empty/<name>  x  y  z")
        box.label(text="Empty names must match OSC address.")
        box.label(text="e.g. Empty named 'left_index'")

        layout.separator()

        dbox = layout.box()
        dbox.label(text="Debug", icon="CONSOLE")
        with _rt["lock"]:
            count = _rt["count"]
            last  = _rt["last"]
        if count:
            dbox.label(text=f"packets: {count}")
            dbox.label(text=last[:60] if last else "(none)")
        else:
            dbox.label(text="no packets", icon="ERROR")


# ── Registration ───────────────────────────────────────────────────────────────

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
    _stop()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.marionette


if __name__ == "__main__":
    register()
