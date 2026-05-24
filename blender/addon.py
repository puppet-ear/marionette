"""
Marionette — Blender addon (multi-interface)

Receives /empty/<name> x y z over OSC and drives scene objects in real time.

# Adding a new interface
1. Write a _draw_<name>(layout, props) function
2. Write a _handle_<name>(name, xyz, props, scene, scale, alpha) function
3. Add an entry to _INTERFACES and to the EnumProperty items list below.
   That's it.

Install: Edit > Preferences > Add-ons > Install > select this file > Enable
Panel:   3D Viewport > N panel > Marionette tab
Stack:   fingers.html → relay.py → OSC UDP:7700 → here
"""

bl_info = {
    "name":        "Marionette",
    "author":      "Marionette Project",
    "version":     (2, 0, 0),
    "blender":     (3, 6, 0),
    "location":    "View3D > Sidebar > Marionette",
    "description": "Real-time hand / input puppeteering via OSC",
    "category":    "Animation",
}

import bpy
import math
import socket
import struct
import threading
import time
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty
from bpy.types import Object, Panel, Operator, PropertyGroup


# ── OSC parser ────────────────────────────────────────────────────────────────

def _read_str(data, offset):
    end = data.index(0, offset)
    s   = data[offset:end].decode("utf-8", errors="replace")
    return s, offset + ((end - offset + 4) // 4 * 4)


def _parse_osc(data):
    try:
        addr, off = _read_str(data, 0)
        if not addr.startswith("/"):
            return None
        if off >= len(data) or data[off:off + 1] != b",":
            return addr, []
        tags, off = _read_str(data, off)
        tags = tags[1:]
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


# ── Coordinate mapping ────────────────────────────────────────────────────────

def _norm_to_blender(x, y, z, scale):
    return (
         (0.5 - x) * scale,    # negate: camera-left = your right
        -(z * scale * 0.4),
        -(y - 0.5) * scale,
    )


# ── Shared runtime state ──────────────────────────────────────────────────────

_rt = {
    "running":          False,
    "sock":             None,
    "thread":           None,
    "latest":           {},           # {name: (x, y, z)}  raw normalised
    "lock":             threading.Lock(),
    "smooth":           {},           # {name: (bx, by, bz)}
    "count":            0,
    "last":             "",
    "last_ts":          0.0,
    "overlay_handle":   None,
    "relay_check_ts":   0.0,
    "relay_check_val":  "stopped",
}


def _listen(sock):
    while _rt["running"]:
        try:
            data, _ = sock.recvfrom(4096)
            result  = _parse_osc(data)
            if result is None:
                continue
            addr, args = result
            if addr.startswith("/empty/") and len(args) >= 3:
                name = addr[7:]
                with _rt["lock"]:
                    _rt["latest"][name] = tuple(args[:3])
                    _rt["count"]       += 1
                    _rt["last"]         = f"{addr}  [{args[0]:.3f}  {args[1]:.3f}  {args[2]:.3f}]"
                    _rt["last_ts"]      = time.time()
        except OSError:
            pass


# ── Viewport overlay ──────────────────────────────────────────────────────────

_FINGER_ORDER = ["thumb", "index", "middle", "ring", "pinky"]
_FINGER_LABEL = ["T", "I", "M", "R", "P"]
_OV_W, _OV_H, _OV_PAD = 340, 270, 16


def _filled_circle(shader, cx, cy, r, n=16):
    from gpu_extras.batch import batch_for_shader
    import math
    perim = [(cx + r * math.cos(2 * math.pi * i / n),
              cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    batch_for_shader(shader, 'TRI_FAN', {"pos": [(cx, cy)] + perim + [perim[0]]}).draw(shader)


def _stroke_circle(shader, cx, cy, r, n=16):
    from gpu_extras.batch import batch_for_shader
    import math
    perim = [(cx + r * math.cos(2 * math.pi * i / n),
              cy + r * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]
    batch_for_shader(shader, 'LINE_STRIP', {"pos": perim}).draw(shader)


def _overlay_draw():
    import blf
    import gpu
    from gpu_extras.batch import batch_for_shader

    with _rt["lock"]:
        latest = dict(_rt["latest"])

    x0, y0 = _OV_PAD, _OV_PAD
    W,  H  = _OV_W,   _OV_H
    INK = (0.05, 0.05, 0.05)

    def to_px(xyz):
        x, y, z = xyz
        return (x0 + (1.0 - x) * W, y0 + (1.0 - y) * H)

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    shader.bind()

    # White background
    bg = [(x0, y0), (x0+W, y0), (x0+W, y0+H), (x0, y0+H)]
    shader.uniform_float("color", (1.0, 1.0, 1.0, 0.92))
    batch_for_shader(shader, 'TRI_FAN', {"pos": bg}).draw(shader)

    # Border
    shader.uniform_float("color", (*INK, 0.18))
    gpu.state.line_width_set(1.0)
    batch_for_shader(shader, 'LINE_STRIP', {"pos": bg + [bg[0]]}).draw(shader)

    # "waiting" state
    for hand in ("left", "right"):
        pts = [to_px(latest[f"{hand}_{f}"]) if f"{hand}_{f}" in latest else None
               for f in _FINGER_ORDER]
        present = [p for p in pts if p]
        if not present:
            continue

        cx = sum(p[0] for p in present) / len(present)
        cy = sum(p[1] for p in present) / len(present)

        # Spokes: palm centroid → each tip
        spokes = [v for p in present for v in ((cx, cy), p)]
        shader.uniform_float("color", (*INK, 0.12))
        gpu.state.line_width_set(1.0)
        batch_for_shader(shader, 'LINES', {"pos": spokes}).draw(shader)

        # Arcs: adjacent fingertips (bone lines)
        arcs = [v for a, b in zip(pts, pts[1:]) if a and b for v in (a, b)]
        if arcs:
            shader.uniform_float("color", (*INK, 0.75))
            gpu.state.line_width_set(1.5)
            batch_for_shader(shader, 'LINES', {"pos": arcs}).draw(shader)

        # Fingertip joints: white fill + dark stroke
        for p in present:
            shader.uniform_float("color", (1.0, 1.0, 1.0, 1.0))
            _filled_circle(shader, p[0], p[1], 5)
            shader.uniform_float("color", (*INK, 1.0))
            gpu.state.line_width_set(1.5)
            _stroke_circle(shader, p[0], p[1], 5)

        # Finger labels
        blf.size(0, 9)
        blf.color(0, *INK, 0.55)
        for lbl, p in zip(_FINGER_LABEL, pts):
            if p:
                blf.position(0, p[0] + 7, p[1] - 3, 0)
                blf.draw(0, lbl)

        # Palm centroid — drawn last so always on top
        shader.uniform_float("color", (*INK, 1.0))
        _filled_circle(shader, cx, cy, 4)

    gpu.state.blend_set('NONE')
    gpu.state.line_width_set(1.0)


def _stop():
    _rt["running"] = False
    if _rt["overlay_handle"]:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_rt["overlay_handle"], 'WINDOW')
        except Exception:
            pass
        _rt["overlay_handle"] = None
    if _rt["sock"]:
        try:
            _rt["sock"].close()
        except OSError:
            pass
        _rt["sock"] = None
    if _rt["thread"]:
        _rt["thread"].join(timeout=1.0)
        _rt["thread"] = None
    if _rt["overlay_handle"]:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_rt["overlay_handle"], 'WINDOW')
        except Exception:
            pass
        _rt["overlay_handle"] = None


def _relay_running(ws_port=8765):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect(("localhost", ws_port))
        s.close()
        return True
    except OSError:
        return False


def _relay_status(ws_port=8765):
    proc = _rt.get("relay_proc")
    if proc and proc.poll() is None:
        return "running (launched by Blender)"
    now = time.time()
    if now - _rt["relay_check_ts"] < 1.0:
        return _rt["relay_check_val"]
    result = "running (external)" if _relay_running(ws_port) else "stopped"
    _rt["relay_check_ts"]  = now
    _rt["relay_check_val"] = result
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Interface: Fingers
#
# Maps /empty/<hand>_<finger>  x y z  onto scene objects.
# Objects can be assigned explicitly via the picker, or auto-resolved by name.
# ══════════════════════════════════════════════════════════════════════════════

_FINGERS = ["thumb", "index", "middle", "ring", "pinky"]
_HANDS   = ["left", "right"]


class FingersProperties(PropertyGroup):
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


def _draw_fingers(layout, props):
    fp = props.fingers

    box = layout.box()
    box.label(text="finger001", icon="HAND")

    header = box.row()
    header.column().label(text="← LEFT")
    header.column().label(text="RIGHT →")
    box.separator(factor=0.3)

    for finger in _FINGERS:
        lbl = finger.capitalize()
        row = box.row(align=False)

        lc = row.column(align=True)
        lc.prop(fp, f"left_{finger}",  icon="OBJECT_DATA", text=lbl)

        row.separator(factor=0.5)

        rc = row.column(align=True)
        rc.prop(fp, f"right_{finger}", icon="OBJECT_DATA", text=lbl)

    box.separator(factor=0.5)
    col = box.column(align=True)
    col.prop(props, "scale",     slider=True)
    col.prop(props, "smoothing", slider=True)


def _handle_fingers(name, xyz, props, scene, scale, alpha):
    fp  = props.fingers
    obj = getattr(fp, name, None)
    if obj is None:
        obj = scene.objects.get(name)
    if obj is None:
        return

    target  = _norm_to_blender(*xyz, scale)
    cur     = _rt["smooth"].get(name, tuple(obj.location))
    new_pos = tuple(cur[i] * alpha + target[i] * (1 - alpha) for i in range(3))
    _rt["smooth"][name] = new_pos
    obj.location = new_pos


# ══════════════════════════════════════════════════════════════════════════════
# Interface: Joystick  (placeholder)
# ══════════════════════════════════════════════════════════════════════════════

def _draw_joystick(layout, props):
    box = layout.box()
    box.label(text="Joystick interface — coming soon", icon="INFO")


def _handle_joystick(name, xyz, props, scene, scale, alpha):
    pass


# ══════════════════════════════════════════════════════════════════════════════
# Interface: Audio  (placeholder)
# ══════════════════════════════════════════════════════════════════════════════

def _draw_audio(layout, props):
    box = layout.box()
    box.label(text="Audio interface — coming soon", icon="INFO")


def _handle_audio(name, xyz, props, scene, scale, alpha):
    pass


# ── Interface registry ────────────────────────────────────────────────────────
# To add a new interface: write _draw_X and _handle_X above, then add here
# and to the EnumProperty items list below.

_INTERFACES = {
    "finger001": {"draw": _draw_fingers,  "handle": _handle_fingers},
    "joystick":  {"draw": _draw_joystick, "handle": _handle_joystick},
    "audio":     {"draw": _draw_audio,    "handle": _handle_audio},
}


# ── Core properties ───────────────────────────────────────────────────────────
# FingersProperties is declared above and must appear in CLASSES before this.

class MarionetteProperties(PropertyGroup):
    interface: EnumProperty(
        name="Interface",
        items=[
            ("finger001", "finger001", "Hand finger tracking"),
            ("joystick",  "joystick",  "Joystick / gamepad control"),
            ("audio",     "audio",     "Audio-reactive control"),
        ],
        default="finger001",
    )

    ws_port: IntProperty(
        name="WS Port",
        default=8765, min=1024, max=65535,
        description="WebSocket port the relay listens on. Must match the port field in the browser.")

    osc_port: IntProperty(
        name="OSC Port",
        default=7700, min=1024, max=65535,
        description="OSC/UDP port Blender listens on. Must match the relay's OSC port. Auto-increments if busy.")

    scale: FloatProperty(
        name="Scale", default=3.0, min=0.1, max=20.0, step=10,
        description="Blender units for full finger travel (0→1 normalised)")

    smoothing: FloatProperty(
        name="Smoothing", default=0.15, min=0.0, max=0.99,
        description="0 = instant, 0.99 = very sluggish")

    fingers: PointerProperty(type=FingersProperties)

    debug_expanded: BoolProperty(name="Debug", default=False)


# ── Operators ─────────────────────────────────────────────────────────────────

class MARIONETTE_OT_start(Operator):
    bl_idname      = "marionette.start"
    bl_label       = "Start"
    bl_description = "Begin listening for OSC data on the configured port"

    def modal(self, context, event):
        if not _rt["running"]:
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        props  = context.scene.marionette
        iface  = _INTERFACES.get(props.interface)
        scale  = props.scale
        alpha  = props.smoothing
        scene  = context.scene

        with _rt["lock"]:
            latest = dict(_rt["latest"])

        if iface:
            for name, xyz in latest.items():
                iface["handle"](name, xyz, props, scene, scale, alpha)

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

        # OSC port: try requested port, auto-increment up to 10 times if busy
        found = None
        for candidate in range(props.osc_port, props.osc_port + 10):
            try:
                sock.bind(("0.0.0.0", candidate))
                found = candidate
                break
            except OSError:
                continue

        if found is None:
            self.report({"ERROR"},
                f"OSC ports {props.osc_port}–{props.osc_port+9} all busy. "
                f"Change OSC Port manually and update the relay to match.")
            sock.close()
            return {"CANCELLED"}

        if found != props.osc_port:
            self.report({"WARNING"},
                f"OSC port {props.osc_port} was busy — now using {found}. "
                f"Update the relay's OSC port to {found}.")
            props.osc_port = found

        _rt.update(running=True, sock=sock, latest={}, smooth={}, count=0, last="")
        t = threading.Thread(target=_listen, args=(sock,), daemon=True)
        t.start()
        _rt["thread"] = t

        handle = bpy.types.SpaceView3D.draw_handler_add(
            _overlay_draw, (), 'WINDOW', 'POST_PIXEL')
        _rt["overlay_handle"] = handle

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.016, window=context.window)
        wm.modal_handler_add(self)

        self.report({"INFO"}, f"Listening — OSC port {found}")
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


# ── Panel ─────────────────────────────────────────────────────────────────────

class MARIONETTE_PT_main(Panel):
    bl_label       = "marionettes"
    bl_idname      = "MARIONETTE_PT_main"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "marionettes"

    def draw(self, context):
        layout = self.layout
        props  = context.scene.marionette
        active = _rt["running"]

        # ── Start / Stop ──────────────────────────────────────────────────────
        row = layout.row()
        row.scale_y = 1.8
        if active:
            row.alert = True
            row.operator("marionette.stop", text="Stop", icon="CANCEL")
        else:
            row.operator("marionette.start", text="Start", icon="PLAY")

        # ── Connection (inline, no box) ───────────────────────────────────────
        ports_row = layout.row(align=True)
        ports_row.prop(props, "ws_port")
        ports_row.prop(props, "osc_port")
        status  = _relay_status(props.ws_port)
        running = "running" in status
        row = layout.row()
        row.label(
            text=f"rrelay: {status}",
            icon="SEQUENCE_COLOR_04" if running else "SEQUENCE_COLOR_01",
        )
        if not running:
            layout.label(text="launch rrelay, then press Start", icon="INFO")

        layout.separator()

        # ── Interface selector + content ──────────────────────────────────────
        layout.prop(props, "interface", text="Interface")
        layout.separator()
        iface = _INTERFACES.get(props.interface)
        if iface:
            iface["draw"](layout, props)

        layout.separator()

        # ── Debug (collapsible) ───────────────────────────────────────────────
        dbox = layout.box()
        hdr  = dbox.row()
        hdr.prop(props, "debug_expanded",
                 icon="TRIA_DOWN" if props.debug_expanded else "TRIA_RIGHT",
                 icon_only=True, emboss=False)
        hdr.label(text="debug", icon="CONSOLE")

        if props.debug_expanded:
            with _rt["lock"]:
                latest = dict(_rt["latest"])
            col = dbox.column(align=True)
            if latest:
                for hand in ("left", "right"):
                    for finger in _FINGER_ORDER:
                        xyz = latest.get(f"{hand}_{finger}")
                        if xyz:
                            col.label(text=f"{hand[0]}_{finger[:3]}  "
                                          f"{xyz[0]:.3f}  {xyz[1]:.3f}  {xyz[2]:.3f}")
            else:
                col.label(text="no data", icon="ERROR")


# ── Registration ──────────────────────────────────────────────────────────────
# FingersProperties must come before MarionetteProperties (PointerProperty ref).

CLASSES = [
    FingersProperties,
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
