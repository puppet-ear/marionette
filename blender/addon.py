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
import pathlib
import socket
import struct
import subprocess
import sys
import threading
from bpy.props import EnumProperty, FloatProperty, IntProperty, PointerProperty
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
         (x - 0.5) * scale,
        -(z * scale * 0.4),
        -(y - 0.5) * scale,
    )


# ── Shared runtime state ──────────────────────────────────────────────────────

_rt = {
    "running":     False,
    "sock":        None,
    "thread":      None,
    "latest":      {},           # {name: (x, y, z)}  raw normalised
    "lock":        threading.Lock(),
    "smooth":      {},           # {name: (bx, by, bz)}
    "count":       0,
    "last":        "",
    "relay_proc":  None,         # subprocess if we launched it
    "relay_owned": False,        # True only if we started it
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
        except OSError:
            pass


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
    if _rt["relay_owned"] and _rt["relay_proc"]:
        _rt["relay_proc"].terminate()
        _rt["relay_proc"]  = None
        _rt["relay_owned"] = False


def _relay_running():
    """Check if something is already listening on the WS port."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect(("localhost", 8765))
        s.close()
        return True
    except OSError:
        return False


def _relay_status():
    proc = _rt.get("relay_proc")
    if proc and proc.poll() is None:
        return "running (launched by Blender)"
    if _relay_running():
        return "running (external)"
    return "stopped"


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

    with _rt["lock"]:
        latest = dict(_rt["latest"])

    # ── Mapping ───────────────────────────────────────────────────────────────
    box = layout.box()
    box.label(text="Finger Mapping", icon="HAND")

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

    # ── Live values (only when tracking data is present) ─────────────────────
    if latest:
        layout.separator(factor=0.5)
        live = layout.box()
        live.label(text="Live", icon="VIEWZOOM")
        col = live.column(align=True)
        for hand in _HANDS:
            for finger in _FINGERS:
                xyz = latest.get(f"{hand}_{finger}")
                if xyz:
                    col.label(text=f"{hand[0]}_{finger[:3]}  "
                                   f"{xyz[0]:.3f}  {xyz[1]:.3f}  {xyz[2]:.3f}")


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
    "fingers":  {"draw": _draw_fingers,  "handle": _handle_fingers},
    "joystick": {"draw": _draw_joystick, "handle": _handle_joystick},
    "audio":    {"draw": _draw_audio,    "handle": _handle_audio},
}


# ── Core properties ───────────────────────────────────────────────────────────
# FingersProperties is declared above and must appear in CLASSES before this.

class MarionetteProperties(PropertyGroup):
    interface: EnumProperty(
        name="Interface",
        items=[
            ("fingers",  "Fingers",  "Hand finger tracking"),
            ("joystick", "Joystick", "Joystick / gamepad control"),
            ("audio",    "Audio",    "Audio-reactive control"),
        ],
        default="fingers",
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


class MARIONETTE_OT_launch_relay(Operator):
    bl_idname      = "marionette.launch_relay"
    bl_label       = "Launch Relay"
    bl_description = "Start relay.py with the current WS and OSC ports"

    def execute(self, context):
        if _rt["relay_proc"] and _rt["relay_proc"].poll() is None:
            self.report({"WARNING"}, "Relay already running (launched by Blender)")
            return {"CANCELLED"}
        if _relay_running():
            self.report({"WARNING"}, "Relay already running externally — not launching another")
            return {"CANCELLED"}

        props      = context.scene.marionette
        relay_path = pathlib.Path(__file__).parent.parent / "transport" / "relay.py"
        if not relay_path.exists():
            self.report({"ERROR"}, f"relay.py not found at {relay_path}")
            return {"CANCELLED"}

        try:
            proc = subprocess.Popen(
                [sys.executable, str(relay_path),
                 "--ws-port",  str(props.ws_port),
                 "--osc-port", str(props.osc_port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _rt["relay_proc"]  = proc
            _rt["relay_owned"] = True
            self.report({"INFO"},
                f"Relay started  WS:{props.ws_port} → OSC:{props.osc_port}")
        except Exception as e:
            self.report({"ERROR"}, f"Failed to launch relay: {e}")
            return {"CANCELLED"}
        return {"FINISHED"}


class MARIONETTE_OT_stop_relay(Operator):
    bl_idname      = "marionette.stop_relay"
    bl_label       = "Stop Relay"
    bl_description = "Stop the relay launched by Blender"

    def execute(self, context):
        if not (_rt["relay_owned"] and _rt["relay_proc"]):
            self.report({"WARNING"}, "No relay was launched by Blender")
            return {"CANCELLED"}
        _rt["relay_proc"].terminate()
        _rt["relay_proc"]  = None
        _rt["relay_owned"] = False
        self.report({"INFO"}, "Relay stopped")
        return {"FINISHED"}


# ── Panel ─────────────────────────────────────────────────────────────────────

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

        # Start / Stop
        row = layout.row()
        row.scale_y = 1.8
        if active:
            row.alert = True
            row.operator("marionette.stop", text="Stop", icon="CANCEL")
        else:
            row.operator("marionette.start", text="Start", icon="PLAY")

        layout.separator()

        # Interface selector
        layout.prop(props, "interface", text="Interface")

        layout.separator()

        # ── Connection ────────────────────────────────────────────────────────
        cbox = layout.box()
        cbox.label(text="Connection", icon="NETWORK_DRIVE")
        col = cbox.column(align=True)
        col.prop(props, "ws_port")
        col.prop(props, "osc_port")
        cbox.separator(factor=0.5)
        cbox.label(text="Browser → WS port → Relay → OSC port → Blender",
                   icon="INFO")
        cbox.label(text="Relay also forwards OSC to other tools on same port.")
        cbox.separator(factor=0.5)
        status = _relay_status()
        relay_row = cbox.row(align=True)
        owned = _rt["relay_owned"] and _rt["relay_proc"] and _rt["relay_proc"].poll() is None
        if owned:
            relay_row.alert = True
            relay_row.operator("marionette.stop_relay",   text="Stop Relay",   icon="CANCEL")
        else:
            relay_row.operator("marionette.launch_relay", text="Launch Relay", icon="PLAY")
        cbox.label(text=f"relay: {status}")

        layout.separator()

        # ── Settings ──────────────────────────────────────────────────────────
        sbox = layout.box()
        sbox.label(text="Settings", icon="PREFERENCES")
        col = sbox.column(align=True)
        col.prop(props, "scale",     slider=True)
        col.prop(props, "smoothing", slider=True)

        layout.separator()

        # Interface-specific section — refreshes automatically on enum change
        iface = _INTERFACES.get(props.interface)
        if iface:
            iface["draw"](layout, props)

        layout.separator()

        # ── Debug ─────────────────────────────────────────────────────────────
        dbox = layout.box()
        dbox.label(text="Debug", icon="CONSOLE")
        with _rt["lock"]:
            count = _rt["count"]
            last  = _rt["last"]
        if count:
            dbox.label(text=f"packets: {count}")
            dbox.label(text=last[:60])
        else:
            dbox.label(text="no packets yet", icon="ERROR")


# ── Registration ──────────────────────────────────────────────────────────────
# FingersProperties must come before MarionetteProperties (PointerProperty ref).

CLASSES = [
    FingersProperties,
    MarionetteProperties,
    MARIONETTE_OT_start,
    MARIONETTE_OT_stop,
    MARIONETTE_OT_launch_relay,
    MARIONETTE_OT_stop_relay,
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
