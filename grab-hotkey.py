#!/usr/bin/env python3
"""Interactive hotkey grabber for wisprflow.

Opens a small window; the next key combination you press becomes the dictation
shortcut (rebinding whatever was set before). Uses GTK so the captured accelerator
is exact and layout-correct (matches how GNOME itself resolves shortcuts).

Run:  python3 grab-hotkey.py       (Esc in the window cancels)
Prints one of: BOUND <accel> | CANCELLED | BIND FAILED ... to stdout.
"""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# lone modifier / lock keys — ignore these and wait for a "real" key
IGNORE = {
    "Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
    "Super_L", "Super_R", "Meta_L", "Meta_R", "Hyper_L", "Hyper_R",
    "ISO_Level3_Shift", "ISO_Level5_Shift", "Mode_switch", "Caps_Lock", "Num_Lock",
}

PROMPT = (
    "<big><b>Press your dictation shortcut</b></big>\n\n"
    "The next key combination you press will be bound\n"
    "to start/stop dictation.\n\n"
    "<small>Esc to cancel · avoid combos already used by GNOME</small>"
)


class Grabber(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.wisprflow.grabhotkey")

    def do_activate(self):
        win = Gtk.ApplicationWindow(application=self,
                                    title="wisprflow — set dictation shortcut")
        win.set_default_size(480, 200)
        self.label = Gtk.Label()
        self.label.set_wrap(True)
        self.label.set_justify(Gtk.Justification.CENTER)
        self.label.set_margin_top(24)
        self.label.set_margin_bottom(24)
        self.label.set_margin_start(24)
        self.label.set_margin_end(24)
        self.label.set_markup(PROMPT)
        win.set_child(self.label)
        ctrl = Gtk.EventControllerKey()
        ctrl.connect("key-pressed", self.on_key)
        win.add_controller(ctrl)
        self.win = win
        win.present()

    def on_key(self, _ctrl, keyval, _keycode, state):
        name = Gdk.keyval_name(keyval) or ""
        if name == "Escape":
            print("CANCELLED", flush=True)
            self.quit()
            return True
        if name in IGNORE:
            return True  # keep waiting for a non-modifier key
        mods = state & Gtk.accelerator_get_default_mod_mask()
        accel = Gtk.accelerator_name(keyval, mods)
        # Reject keys GNOME's shortcut system can't actually bind: special/vendor keys
        # whose accelerator string doesn't round-trip through the parser.
        parses = bool(accel) and Gtk.accelerator_parse(accel)[0]
        if (not accel) or (not parses) or name.startswith("0x"):
            print(f"REJECTED unbindable {name}", flush=True)
            self.label.set_markup(
                "<big><b>That key can’t be a GNOME shortcut</b></big>\n"
                "<small>(special / multimedia / vendor key)</small>\n\n"
                "Press a normal key <b>with Ctrl / Alt / Super</b>,\n"
                "or a function key (F1–F12).  <small>Esc to cancel.</small>")
            return True
        # Reject a bare printable key (no modifier) — it would be captured system-wide,
        # so you could never type that character again.
        if not mods and Gdk.keyval_to_unicode(keyval) != 0:
            print(f"REJECTED bare-printable {name}", flush=True)
            self.label.set_markup(
                "<big><b>Add a modifier</b></big>\n\n"
                "A bare letter/number would be grabbed system-wide.\n"
                "Hold <b>Ctrl / Alt / Super</b> and press a key\n"
                "(or use a function key).  <small>Esc to cancel.</small>")
            return True
        try:
            subprocess.run([os.path.join(HERE, "set-hotkey.sh"), accel],
                           check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            print(f"BIND FAILED {accel}: {e.stderr or e}", flush=True)
            self.label.set_markup(f"<b>Could not bind {GLib.markup_escape_text(accel)}</b>\n"
                                  "<small>see terminal; press another combo or Esc</small>")
            return True
        human = Gtk.accelerator_get_label(keyval, mods)
        print(f"BOUND {accel}", flush=True)
        self.label.set_markup(
            f"<big>✓ Dictation shortcut set to</big>\n<big><b>{GLib.markup_escape_text(human)}</b></big>"
            "\n\n<small>closing…</small>")
        GLib.timeout_add(1400, self.quit)
        return True


if __name__ == "__main__":
    Grabber().run(None)
