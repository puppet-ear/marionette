# Blender Multi-Interface Addon

**Status: Fingers implemented. Joystick and Audio are placeholders.**

## Architecture

Everything lives in `blender/addon.py` — a single file, easy to install.

### Adding a new interface (3 steps)

1. Write `_draw_<name>(layout, props)` and `_handle_<name>(name, xyz, props, scene, scale, alpha)`
2. Add an entry to `_INTERFACES` dict
3. Add an entry to the `EnumProperty` items list in `MarionetteProperties`

That's it. The panel refreshes automatically when the user changes the dropdown.

### Interface contract

```python
def _draw_<name>(layout, props):
    # Draw any Blender UI controls for this interface.
    # props.fingers, props.joystick, etc. hold per-interface sub-properties.
    pass

def _handle_<name>(name, xyz, props, scene, scale, alpha):
    # Called once per OSC packet name from /empty/<name>.
    # Modify scene objects, update smoothing state, etc.
    pass
```

### Sub-properties for a new interface

If your interface needs Blender-side properties (e.g. object pickers):

1. Define a `PropertyGroup` subclass (e.g. `JoystickProperties`)
2. Add it to `CLASSES` **before** `MarionetteProperties`
3. Add `joystick: PointerProperty(type=JoystickProperties)` to `MarionetteProperties`
4. Access it in your draw/handle functions via `props.joystick`

The registration order constraint (sub-group before parent) is why CLASSES is ordered explicitly.

### Current interfaces

| ID | Status | Panel content |
|----|--------|---------------|
| `fingers` | ✅ Working | Finger mapping (10 pickers, left/right), live xyz readout |
| `joystick` | 🔲 Placeholder | "Coming soon" label |
| `audio` | 🔲 Placeholder | "Coming soon" label |

### OSC wire format

```
/empty/<name>  ,fff  x y z
```

`name` is e.g. `left_index`, `right_thumb`. Sent by `transport/relay.py` from the browser interface.

### File layout

```
blender/
  addon.py          ← everything here

transport/
  relay.py          ← WebSocket → OSC bridge (run separately if using Blender)

interfaces/
  fingers.html      ← browser interface

main.py             ← static file server + HTTP CSV logger (no OSC)
```

`main.py` and `relay.py` are independent paths:
- **Recording only**: `python3 main.py` → browser → HTTP POST → CSV
- **Blender puppeteering**: `python3 main.py` + `python3 transport/relay.py` → browser → WS → OSC → Blender

## Smoke test checklist (manual, in Blender)

- [ ] Install addon: Edit > Preferences > Add-ons > Install > `blender/addon.py` > Enable
- [ ] Panel visible: 3D Viewport > N panel > Marionette tab
- [ ] Dropdown shows: Fingers / Joystick / Audio
- [ ] Switching to Joystick shows "coming soon" label
- [ ] Switching to Audio shows "coming soon" label
- [ ] Switching back to Fingers shows the finger mapping table (5 rows, left + right pickers each)
- [ ] Add Empties named `left_index`, `right_thumb` → they appear auto-resolved when tracking starts
- [ ] Or: assign objects explicitly via the pickers
- [ ] Run `python3 main.py` + `python3 transport/relay.py`, open fingers.html
- [ ] Click Start → packet count increments, live xyz values appear under finger mapping
- [ ] Empties move in sync with fingers
- [ ] Click Stop → movement freezes
