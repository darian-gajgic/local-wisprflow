#!/usr/bin/env python3
"""pill-shot.py — screenshot the LIVE Listening pill in each output mode (dev / verification tool).

Run with the SYSTEM python3 (needs Pillow + libX11; the project venv has neither):

    python3 pill-shot.py [outdir] [mode ...]      # default: /tmp/wf-pill  clean note raw

For each mode it drives the RUNNING daemon: `wf-toggle mode <m>`, `wf-toggle start` (so the pill
appears), reads the pill's XWayland window pixels with XGetImage, then `wf-toggle cancel` (nothing
is transcribed or typed). It ends with the mode set to `clean`. Output: <outdir>/pill_<mode>.png.

Why XGetImage: on this GNOME Wayland session every other scripted capture fails — gnome-screenshot
("Unable to capture a screenshot of any window"), Pillow's ImageGrab (routes to gnome-screenshot),
the org.gnome.Shell.Screenshot D-Bus API ("Invalid params" for outside callers) and the desktop
portal. The pill is an X11 (XWayland) window, and XWayland keeps a real pixmap for it, so reading
that window directly works and needs no permission.
"""
import ctypes
import ctypes.util
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/wf-pill"
MODES = sys.argv[2:] or ["clean", "note", "raw"]


def env_from_user_manager():
    """DISPLAY/XAUTHORITY as the daemon sees them (systemd --user environment)."""
    out = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k in ("DISPLAY", "XAUTHORITY") and k not in os.environ:
            os.environ[k] = v


def toggle(*args):
    r = subprocess.run([os.path.join(HERE, "wf-toggle"), *args], capture_output=True, text=True, timeout=10)
    return (r.stdout or r.stderr).strip()


def find_tk_window():
    tree = subprocess.run(["xwininfo", "-root", "-tree"], capture_output=True, text=True).stdout
    m = re.search(r'(0x[0-9a-f]+) "tk[^"]*": \("tk[^)]*\)\s+(\d+)x(\d+)\+', tree)
    return (int(m.group(1), 16), int(m.group(2)), int(m.group(3))) if m else None


class XImage(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int),
                ("format", ctypes.c_int), ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
                ("bitmap_unit", ctypes.c_int), ("bitmap_bit_order", ctypes.c_int),
                ("bitmap_pad", ctypes.c_int), ("depth", ctypes.c_int), ("bytes_per_line", ctypes.c_int),
                ("bits_per_pixel", ctypes.c_int), ("red_mask", ctypes.c_ulong),
                ("green_mask", ctypes.c_ulong), ("blue_mask", ctypes.c_ulong)]


def grab_window(win, w, h, path):
    from PIL import Image
    X = ctypes.CDLL(ctypes.util.find_library("X11"))
    X.XOpenDisplay.restype = ctypes.c_void_p
    X.XOpenDisplay.argtypes = [ctypes.c_char_p]
    X.XGetImage.restype = ctypes.POINTER(XImage)
    X.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                            ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]
    errors = []
    handler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda d, e: errors.append(1) or 0)
    X.XSetErrorHandler(handler)
    dpy = X.XOpenDisplay(None)
    if not dpy:
        raise RuntimeError(f"XOpenDisplay failed (DISPLAY={os.environ.get('DISPLAY')})")
    img = X.XGetImage(dpy, win, 0, 0, w, h, 0xFFFFFFFF, 2)   # AllPlanes, ZPixmap
    if not img or errors:
        raise RuntimeError(f"XGetImage failed for window {hex(win)}")
    im = img.contents
    buf = ctypes.string_at(im.data, im.bytes_per_line * im.height)
    mode = "BGRX" if im.bits_per_pixel == 32 else "BGR"
    pil = Image.frombuffer("RGB", (im.width, im.height), buf, "raw", mode, im.bytes_per_line, 1)
    pil.save(path)
    X.XCloseDisplay(dpy)
    return len(pil.getcolors(1_000_000) or [])


def main():
    env_from_user_manager()
    os.makedirs(OUT, exist_ok=True)
    if toggle("status") != "idle":
        sys.exit("daemon is not idle (recording/processing/meeting) — try again later")
    if find_tk_window():
        sys.exit("a Tk window is already on screen (stale pill?) — close it first")
    ok = True
    try:
        for m in MODES:
            print(f"[{m}] {toggle('mode', m)} / {toggle('start')}", end=" ")
            time.sleep(1.8)                       # pill up + first animation frame
            try:
                win = find_tk_window()
                if not win:
                    raise RuntimeError("no pill window appeared")
                path = os.path.join(OUT, f"pill_{m}.png")
                colors = grab_window(*win, path)
                print(f"-> {path} ({win[1]}x{win[2]}, {colors} colors)")
                ok &= colors > 3
            finally:
                toggle("cancel")
                time.sleep(0.8)
    finally:
        toggle("mode", "clean")
    print("done" if ok else "some captures look empty")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
