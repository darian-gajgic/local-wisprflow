#!/usr/bin/env python3
"""wisprflow on-screen overlay — lightweight indicators.

Modes (argv):
  listening        animated waveform pill + a clickable "Meeting" button; runs until SIGTERM
  meeting          "recording meeting" pill (pulsing red dot); runs until SIGTERM
  done  [TEXT]     green check + short text, auto-closes after ~1s

Clicking "Meeting" sends the `meeting` command to the daemon's Unix socket, which switches the
session to dual-channel meeting transcription (then the daemon swaps this overlay for the
meeting one). Borderless, bottom-center, always-on-top, non-focus-stealing (X11 override-redirect
via XWayland). tkinter only (project venv). Small + low-fps so it costs almost nothing.
"""
import math
import os
import signal
import socket
import sys
import time
import tkinter as tk

MODE = sys.argv[1] if len(sys.argv) > 1 else "listening"
TEXT = sys.argv[2] if len(sys.argv) > 2 else ""

BG = "#17171b"
ACCENT = "#5b8cff"      # waveform blue
MUTE = "#9aa0a6"
OKC = "#43d17a"
REC = "#ff5c5c"         # meeting record red
FG = "#eaeaec"
BTN = "#2b2f3a"
FPS = 24

root = tk.Tk()
root.overrideredirect(True)
root.attributes("-topmost", True)
try:
    root.attributes("-alpha", 0.97)
except tk.TclError:
    pass

sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
scale = max(1.0, round(sw / 1920.0))
W = int((300 if MODE in ("listening", "meeting") else 260) * scale)
H = int(64 * scale)
x, y = (sw - W) // 2, sh - H - int(96 * scale)
root.geometry(f"{W}x{H}+{x}+{y}")
root.configure(bg=BG)
cv = tk.Canvas(root, width=W, height=H, bg=BG, highlightthickness=0, bd=0)
cv.pack(fill="both", expand=True)


def round_rect(c, x1, y1, x2, y2, r, **kw):
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


round_rect(cv, 1, 1, W - 1, H - 1, int(18 * scale), fill=BG, outline="#2c2c33")


def close(*_):
    try:
        root.destroy()
    except Exception:
        pass


signal.signal(signal.SIGTERM, close)
signal.signal(signal.SIGINT, close)


def watchdog():
    """Close if the daemon (our parent) dies, so no orphan pill lingers."""
    if os.getppid() != _ppid0:
        close()
        return
    root.after(1000, watchdog)


_ppid0 = os.getppid()


def send_meeting(*_):
    try:
        rt = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(os.path.join(rt, "wf-daemon.sock"))
        s.sendall(b"meeting")
        s.recv(64)
        s.close()
    except Exception:
        pass
    close()  # daemon will relaunch this overlay in "meeting" mode


if MODE == "done":
    cv.create_text(int(30 * scale), H / 2, text="✓", fill=OKC,
                   font=("Sans", int(20 * scale), "bold"))
    cv.create_text(int(52 * scale), H / 2, text=(TEXT or "Inserted"), anchor="w",
                   fill=FG, font=("Sans", int(11 * scale)), width=W - int(60 * scale))
    root.after(1000, close)

elif MODE == "meeting":
    dotx, cy = int(30 * scale), H // 2
    cv.create_text(int(52 * scale), int(H * 0.38), text="Meeting — recording", anchor="w",
                   fill=FG, font=("Sans", int(11 * scale), "bold"))
    cv.create_text(int(52 * scale), int(H * 0.66), text="press your key to stop", anchor="w",
                   fill=MUTE, font=("Sans", int(9 * scale)))
    t0 = time.time()

    def pulse():
        cv.delete("dot")
        a = 0.5 + 0.5 * math.sin((time.time() - t0) * 4.0)
        rr = int((6 + a * 4) * scale)
        cv.create_oval(dotx - rr, cy - rr, dotx + rr, cy + rr, fill=REC, outline="", tags="dot")
        root.after(int(1000 / FPS), pulse)

    pulse()
    watchdog()

else:  # listening
    # left: animated waveform; right: clickable "Meeting" button
    bw, gap, nb = int(6 * scale), int(9 * scale), 5
    total = nb * bw + (nb - 1) * gap
    wf_cx = int(W * 0.30)
    x0 = wf_cx - total // 2
    cy = int(H * 0.42)
    cv.create_text(wf_cx, int(H * 0.80), text="Listening…", fill=MUTE,
                   font=("Sans", int(9 * scale)))
    # Meeting button (right side)
    bx1, bx2 = int(W * 0.56), int(W * 0.95)
    by1, by2 = int(H * 0.24), int(H * 0.76)
    round_rect(cv, bx1, by1, bx2, by2, int(12 * scale), fill=BTN, outline="#3a4050", tags="mbtn")
    cv.create_text((bx1 + bx2) // 2, (by1 + by2) // 2, text="👥  Meeting", fill=FG,
                   font=("Sans", int(10 * scale)), tags="mbtn")
    cv.tag_bind("mbtn", "<Button-1>", send_meeting)
    cv.tag_bind("mbtn", "<Enter>", lambda e: cv.config(cursor="hand2"))
    phases = [i * 0.9 for i in range(nb)]
    t0 = time.time()

    def tick():
        cv.delete("bar")
        t = time.time() - t0
        for i in range(nb):
            amp = 0.28 + 0.72 * (0.5 + 0.5 * math.sin(t * 6.0 + phases[i]))
            bh = int(6 + amp * (H * 0.42))
            bx = x0 + i * (bw + gap)
            round_rect(cv, bx, cy - bh // 2, bx + bw, cy + bh // 2, bw // 2,
                       fill=ACCENT, outline="", tags="bar")
        root.after(int(1000 / FPS), tick)

    tick()
    watchdog()

root.mainloop()
