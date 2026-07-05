#!/usr/bin/env python3
"""wisprflow key listener.

Triggers `wf-toggle` whenever a specific evdev key is pressed — used for special/vendor
keys (here KEY_PRESENTATION, code 425) that GNOME's shortcut system can't bind. Reads raw
input devices (needs the 'input' group). Only fires on the initial key-down (value==1), so
holding the key doesn't spam toggles. Robust to device hot-plug via periodic re-enumeration.

DUPLICATE COLLAPSE (fixes "sometimes a press isn't registered"): on this laptop the
KEY_PRESENTATION key is reported by MORE THAN ONE input device at once — the AT keyboard AND
the Acer vendor hotkey device both emit it for a single physical press. Two key-downs -> two
`wf-toggle` calls -> the toggles cancel (start-then-stop, or stop-then-restart), so the press
looks "lost". We collapse those duplicates into ONE toggle two ways:
  * a real key RELEASE (value 0) re-arms us — so a genuine second press (which is always
    preceded by releasing the first) is NEVER swallowed, no matter how fast it comes; and
  * a short hard floor (MIN_GAP) plus a wider re-arm window (WF_DEBOUNCE_MS) drop the
    duplicate key-downs of ONE press, which arrive within a few ms of each other and before
    any release.
This is timing-robust (the hard floor collapses simultaneous dups regardless of event order)
AND it can't eat a legit fast stop/start (the release always re-arms first).

Configure with WF_KEYCODE (default 425) and WF_DEBOUNCE_MS (default 250; the re-arm window).
"""
import os
import subprocess
import time
from select import select

import evdev
from evdev import ecodes

KEYCODE = int(os.environ.get("WF_KEYCODE", "425"))  # 425 = KEY_PRESENTATION
DEBOUNCE_S = max(0.0, float(os.environ.get("WF_DEBOUNCE_MS", "250")) / 1000.0)
MIN_GAP_S = 0.08   # hard floor: same-press duplicate emissions are always closer than this
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
    print(f"wf-keylistener: watching keycode {KEYCODE} ({key}) -> {TOGGLE} "
          f"(debounce {int(DEBOUNCE_S * 1000)}ms)", flush=True)
    devs = open_devices()
    if devs:
        print("  on: " + ", ".join(f"{d.name!r}({d.path})" for d in devs), flush=True)
        if len(devs) > 1:
            print(f"  note: {len(devs)} devices report this key — collapsing their "
                  "duplicate emissions into one toggle", flush=True)
    last_fire = 0.0    # monotonic time of the last accepted trigger (shared across all devices)
    armed = True       # re-armed by a key release; a genuine next press is then never suppressed
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
                    if ev.type != ecodes.EV_KEY or ev.code != KEYCODE:
                        continue
                    if ev.value == 0:            # key release -> re-arm (a genuine next press is safe)
                        armed = True
                        continue
                    if ev.value != 1:            # ignore autorepeat (value 2)
                        continue
                    now = time.monotonic()
                    dt = now - last_fire
                    # Suppress only DUPLICATE emissions of the same physical press: within the hard
                    # floor (ordering-robust), or still un-re-armed (no release yet) inside the window.
                    if dt < MIN_GAP_S or (not armed and dt < DEBOUNCE_S):
                        print(f"  (duplicate key-down from {d.name!r} @ {dt * 1000:.0f}ms — "
                              "suppressed)", flush=True)
                        continue
                    armed = False
                    last_fire = now
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
