#!/usr/bin/env python3
"""Capture the low-level evdev (keycode + device) of a physical key.

For keys GNOME's shortcut system can't bind (special/multimedia/vendor keys), we watch
the raw input device instead. This prints the first non-modifier key you press as:
    CAPTURED code=<n> name=<KEY_*> device='<name>' path=/dev/input/eventN
Needs membership in the 'input' group (already granted). Ctrl+C or 30s timeout to cancel.
"""
import sys
import time
from collections import Counter
from select import select

try:
    import evdev
    from evdev import ecodes
except ImportError:
    sys.exit("evdev-not-installed")

MODIFIERS = {
    ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL, ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT,
    ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT, ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA,
}


def keyname(code: int) -> str:
    n = ecodes.KEY.get(code, ecodes.BTN.get(code, code))
    return n[0] if isinstance(n, list) else str(n)


def main() -> int:
    devs = []
    EXCLUDE = ("touchpad", "mouse", "ydotoold")  # pointer / our own virtual kbd
    for p in evdev.list_devices():
        try:
            d = evdev.InputDevice(p)
            if ecodes.EV_KEY in d.capabilities() and not any(x in d.name.lower() for x in EXCLUDE):
                devs.append(d)
        except Exception:  # noqa: BLE001
            pass
    if not devs:
        print("NO_DEVICES (need 'input' group / re-login)", flush=True)
        return 1
    print(f"Watching {len(devs)} devices. Press your SPECIAL KEY 4x...", flush=True)
    fdmap = {d.fd: d for d in devs}
    counts = Counter()
    end = time.time() + 30
    while time.time() < end:
        r, _, _ = select(list(fdmap), [], [], 0.5)
        for fd in r:
            try:
                for ev in fdmap[fd].read():
                    # accept any key-down except modifiers and mouse/touch buttons (BTN_*)
                    if (ev.type == ecodes.EV_KEY and ev.value == 1
                            and ev.code not in MODIFIERS and not (0x100 <= ev.code < 0x160)):
                        d = fdmap[fd]
                        counts[(ev.code, keyname(ev.code), d.name, d.path)] += 1
            except OSError:
                pass
        if counts and counts.most_common(1)[0][1] >= 3:
            break  # clear winner
    if not counts:
        print("NONE (no keys captured)", flush=True)
        return 1
    for (code, kn, dname, dpath), c in counts.most_common():
        print(f"COUNT x{c} code={code} name={kn} device={dname!r} path={dpath}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
