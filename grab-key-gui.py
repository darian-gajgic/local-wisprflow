#!/usr/bin/env python3
"""GUI + evdev hotkey grabber.

Shows a window (so you know when to press) while a background thread reads the RAW evdev
keycode from all keyboard/hotkey devices — which works even for special/vendor keys that
GNOME can't name or bind. Press your key ~3 times. Also logs the GTK keysym for diagnostics.

Prints:  EVDEV_WINNER code=<n> name=<KEY_*> device='<name>' path=/dev/input/eventN
   or on timeout, a diagnostic dump of every evdev key + GTK keysym it saw.
"""
import threading
import time
from collections import Counter
from select import select

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402
import evdev  # noqa: E402
from evdev import ecodes  # noqa: E402

MODIFIERS = {
    ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL, ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT,
    ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT, ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA,
}
NEEDED = 3     # presses of the same key to win
TIMEOUT = 20   # seconds


def keyname(code):
    n = ecodes.KEY.get(code, ecodes.BTN.get(code, code))
    return n[0] if isinstance(n, list) else str(n)


class EvReader(threading.Thread):
    def __init__(self, on_winner):
        super().__init__(daemon=True)
        self.on_winner = on_winner
        self.stop = False
        self.counts = Counter()
        self.devs = []
        for p in evdev.list_devices():
            try:
                d = evdev.InputDevice(p)
                nm = d.name.lower()
                if (ecodes.EV_KEY in d.capabilities()
                        and not any(x in nm for x in ("ydotoold", "mouse", "touchpad"))):
                    self.devs.append(d)
            except Exception:  # noqa: BLE001
                pass

    def run(self):
        if not self.devs:
            GLib.idle_add(self.on_winner, None)
            return
        fdmap = {d.fd: d for d in self.devs}
        while not self.stop:
            r, _, _ = select(list(fdmap), [], [], 0.3)
            for fd in r:
                try:
                    for ev in fdmap[fd].read():
                        if (ev.type == ecodes.EV_KEY and ev.value == 1
                                and ev.code not in MODIFIERS and not (0x100 <= ev.code < 0x160)):
                            d = fdmap[fd]
                            k = (ev.code, keyname(ev.code), d.name, d.path)
                            self.counts[k] += 1
                            if self.counts[k] >= NEEDED:
                                self.stop = True
                                GLib.idle_add(self.on_winner, k)
                                return
                except OSError:
                    pass


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.wisprflow.grabgui")
        self.reader = None
        self.gtk_keysyms = []
        self.done = False

    def do_activate(self):
        win = Gtk.ApplicationWindow(application=self, title="wisprflow — capture special key")
        win.set_default_size(500, 200)
        self.label = Gtk.Label()
        self.label.set_wrap(True)
        self.label.set_justify(Gtk.Justification.CENTER)
        for m in ("top", "bottom", "start", "end"):
            getattr(self.label, f"set_margin_{m}")(24)
        self.label.set_markup(
            f"<big><b>Press your special key {NEEDED}× now</b></big>\n\n"
            "Just that key, nothing else. This reads the raw hardware\n"
            "code, so it works even though GNOME can’t name it.")
        win.set_child(self.label)
        kc = Gtk.EventControllerKey()
        kc.connect("key-pressed", self.on_gtk_key)
        win.add_controller(kc)
        self.win = win
        win.present()
        self.reader = EvReader(self.on_winner)
        self.reader.start()
        GLib.timeout_add_seconds(TIMEOUT, self.on_timeout)

    def on_gtk_key(self, _c, keyval, _kc, _st):
        nm = Gdk.keyval_name(keyval) or ""
        self.gtk_keysyms.append(f"{nm}(0x{keyval:x})")
        return False

    def on_winner(self, k):
        if self.done:
            return
        self.done = True
        if k is None:
            print("NO_DEVICES", flush=True)
            self.label.set_markup("<b>No input devices readable</b>\n(need 'input' group)")
        else:
            code, name, dev, path = k
            print(f"EVDEV_WINNER code={code} name={name} device={dev!r} path={path}", flush=True)
            print(f"GTK_KEYSYMS {self.gtk_keysyms}", flush=True)
            self.label.set_markup(
                f"<big>✓ Captured</big>\n<b>{GLib.markup_escape_text(name)}</b> "
                f"(code {code})\non {GLib.markup_escape_text(dev)}\n\n<small>closing…</small>")
        GLib.timeout_add(1400, self.quit)

    def on_timeout(self):
        if self.done:
            return False
        self.done = True
        counts = self.reader.counts if self.reader else Counter()
        print("TIMEOUT no winner", flush=True)
        for (code, name, dev, path), c in counts.most_common():
            print(f"EVDEV_SEEN x{c} code={code} name={name} device={dev!r} path={path}", flush=True)
        print(f"GTK_KEYSYMS {self.gtk_keysyms}", flush=True)
        if self.reader:
            self.reader.stop = True
        self.quit()
        return False


if __name__ == "__main__":
    App().run(None)
