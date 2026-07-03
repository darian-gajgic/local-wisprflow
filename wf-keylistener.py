#!/usr/bin/env python3
"""wisprflow key listener.

Triggers `wf-toggle` whenever a specific evdev key is pressed — used for special/vendor
keys (here KEY_PRESENTATION, code 425) that GNOME's shortcut system can't bind. Reads raw
input devices (needs the 'input' group). Only fires on the initial key-down (value==1), so
holding the key doesn't spam toggles. Robust to device hot-plug via periodic re-enumeration.

Configure the key with the WF_KEYCODE env var (default 425).
"""
import os
import subprocess
import time
from select import select

import evdev
from evdev import ecodes

KEYCODE = int(os.environ.get("WF_KEYCODE", "425"))  # 425 = KEY_PRESENTATION
HERE = os.path.dirname(os.path.abspath(__file__))
TOGGLE = os.path.join(HERE, "wf-toggle")
EXCLUDE = ("mouse", "touchpad", "ydotoold")
RESCAN_SECS = 10


def open_devices():
    """Open every input device that actually carries the target keycode."""
    devs = []
    for p in evdev.list_devices():
        try:
            d = evdev.InputDevice(p)
            nm = d.name.lower()
            caps = d.capabilities()
            if (ecodes.EV_KEY in caps
                    and KEYCODE in caps.get(ecodes.EV_KEY, [])
                    and not any(x in nm for x in EXCLUDE)):
                devs.append(d)
        except Exception:  # noqa: BLE001
            pass
    return devs


def close_all(devs):
    for d in devs:
        try:
            d.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    key = ecodes.KEY.get(KEYCODE, KEYCODE)
    print(f"wf-keylistener: watching keycode {KEYCODE} ({key}) -> {TOGGLE}", flush=True)
    devs = open_devices()
    if devs:
        print("  on: " + ", ".join(f"{d.name!r}({d.path})" for d in devs), flush=True)
    last_scan = time.time()
    while True:
        if not devs:
            time.sleep(2)
            devs = open_devices()
            continue
        fdmap = {d.fd: d for d in devs}
        try:
            r, _, _ = select(list(fdmap), [], [], 5.0)
        except (OSError, ValueError):
            close_all(devs)
            devs = open_devices()
            continue
        for fd in r:
            d = fdmap.get(fd)
            try:
                for ev in d.read():
                    if ev.type == ecodes.EV_KEY and ev.code == KEYCODE and ev.value == 1:
                        print("trigger -> wf-toggle", flush=True)
                        subprocess.Popen([TOGGLE], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
            except OSError:
                close_all(devs)        # a device vanished -> re-enumerate
                devs = open_devices()
                break
        if time.time() - last_scan > RESCAN_SECS:
            close_all(devs)
            devs = open_devices()
            last_scan = time.time()


if __name__ == "__main__":
    raise SystemExit(main())
