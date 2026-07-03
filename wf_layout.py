#!/usr/bin/env python3
"""Layout-aware typing for local-wisprflow.

`ydotool type` emits US-layout keycodes, so it mistypes on non-US layouts (German QWERTZ:
y<->z, ?->_, etc.) — which is why we'd switched to clipboard paste. But paste needs a per-app
paste chord (Ctrl+V in GUIs, Ctrl+Shift+V in terminals like Hermes/Claude Code), and the
focused window can't be detected reliably on GNOME Wayland.

So instead we TYPE, but with the CORRECT keycodes for the user's actual layout: we read the
current XKB layout and, via libxkbcommon (ctypes — no dev headers, no sudo), map each character
to the (evdev keycode, shift/AltGr level) that produces it. ydotool then emits those raw
keycodes, which the focused app interprets under its layout to the intended character. This
works identically in EVERY app — terminal or GUI — with no paste chord and no clipboard use.
"""
from __future__ import annotations

import ast
import ctypes
import subprocess
import unicodedata

# evdev modifier keycodes
LSHIFT = 42
ALTGR = 100          # Right Alt / ISO_Level3_Shift on de+nodeadkeys
SPECIAL = {" ": 57, "\n": 28, "\t": 15}   # space, enter, tab

# characters that aren't on a physical keyboard -> nearest typeable equivalent
NORMALIZE = {
    "—": "-", "–": "-", "‑": "-",       # em/en/non-breaking dash
    "‘": "'", "’": "'", "‚": "'",        # curly single quotes
    "“": '"', "”": '"', "„": '"',        # curly double quotes
    "…": "...", " ": " ", " ": " ", " ": " ",
}


def get_current_layout():
    """(layout, variant) from the GNOME input source, e.g. ('de', 'nodeadkeys'). Falls back to us."""
    try:
        out = subprocess.check_output(
            ["gsettings", "get", "org.gnome.desktop.input-sources", "sources"], text=True)
        for typ, val in ast.literal_eval(out.strip()):
            if typ == "xkb":
                return (val.split("+", 1) + [None])[:2] if "+" in val else (val, None)
    except Exception:  # noqa: BLE001
        pass
    return "us", None


def build_charmap(layout: str, variant):
    """char -> (evdev_keycode, level) for the given XKB layout, via libxkbcommon."""
    lib = ctypes.CDLL("libxkbcommon.so.0")

    class RN(ctypes.Structure):
        _fields_ = [("rules", ctypes.c_char_p), ("model", ctypes.c_char_p),
                    ("layout", ctypes.c_char_p), ("variant", ctypes.c_char_p),
                    ("options", ctypes.c_char_p)]

    lib.xkb_context_new.restype = ctypes.c_void_p
    lib.xkb_context_new.argtypes = [ctypes.c_int]
    lib.xkb_keymap_new_from_names.restype = ctypes.c_void_p
    lib.xkb_keymap_new_from_names.argtypes = [ctypes.c_void_p, ctypes.POINTER(RN), ctypes.c_int]
    lib.xkb_keymap_min_keycode.restype = ctypes.c_uint32
    lib.xkb_keymap_min_keycode.argtypes = [ctypes.c_void_p]
    lib.xkb_keymap_max_keycode.restype = ctypes.c_uint32
    lib.xkb_keymap_max_keycode.argtypes = [ctypes.c_void_p]
    lib.xkb_keymap_key_get_syms_by_level.restype = ctypes.c_int
    lib.xkb_keymap_key_get_syms_by_level.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint32))]
    lib.xkb_keysym_to_utf8.restype = ctypes.c_int
    lib.xkb_keysym_to_utf8.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t]

    ctx = lib.xkb_context_new(0)
    rn = RN(None, None, layout.encode(), variant.encode() if variant else None, None)
    km = lib.xkb_keymap_new_from_names(ctx, ctypes.byref(rn), 0)
    if not km:
        raise RuntimeError(f"could not compile keymap for {layout}+{variant}")
    charmap = {}
    buf = ctypes.create_string_buffer(8)
    for kc in range(lib.xkb_keymap_min_keycode(km), lib.xkb_keymap_max_keycode(km) + 1):
        for lvl in range(4):
            syms = ctypes.POINTER(ctypes.c_uint32)()
            n = lib.xkb_keymap_key_get_syms_by_level(km, kc, 0, lvl, ctypes.byref(syms))
            if n >= 1 and lib.xkb_keysym_to_utf8(syms[0], buf, 8) > 0:
                ch = buf.value.decode("utf8", "ignore")
                if len(ch) == 1 and ch not in charmap:
                    charmap[ch] = (kc - 8, lvl)   # evdev keycode = xkb keycode - 8
    return charmap


def key_events(charmap, text):
    """ydotool `key` args (evdev 'code:state' strings) to type `text` on the mapped layout.
    Returns (events, skipped) — skipped = chars that couldn't be typed at all."""
    ev, skipped = [], []
    for ch in text:
        ch = NORMALIZE.get(ch, ch)
        if not ch:
            continue
        if ch in SPECIAL:
            k = SPECIAL[ch]
            ev += [f"{k}:1", f"{k}:0"]
            continue
        m = charmap.get(ch)
        if m is None:
            # last resort: strip accents (é -> e) if the base char is typeable
            base = "".join(c for c in unicodedata.normalize("NFKD", ch)
                           if not unicodedata.combining(c))
            m = charmap.get(base) if len(base) == 1 else None
            if m is None:
                skipped.append(ch)
                continue
        kc, lvl = m
        pre, post = [], []
        if lvl in (1, 3):
            pre.append(f"{LSHIFT}:1")
            post = [f"{LSHIFT}:0"] + post
        if lvl in (2, 3):
            pre.append(f"{ALTGR}:1")
            post = [f"{ALTGR}:0"] + post
        ev += pre + [f"{kc}:1", f"{kc}:0"] + post
    return ev, skipped
