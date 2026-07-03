#!/usr/bin/env python3
"""wisprflow on-screen overlay — lightweight listening indicator + brief 'done' flash.

Modes (argv):
  listening        animated waveform pill; runs until SIGTERM (daemon kills it)
  done  [TEXT]     green check + short text, auto-closes after ~1s

Borderless, bottom-center, always-on-top, and NON-focus-stealing (X11 override-redirect via
XWayland — Mutter honours it like a tooltip). tkinter only (in the project venv). Deliberately
small and low-fps so it costs almost nothing and never gets in the way of other windows.
"""
import math
import os
import signal
import sys
import time
import tkinter as tk

MODE = sys.argv[1] if len(sys.argv) > 1 else "listening"
TEXT = sys.argv[2] if len(sys.argv) > 2 else ""

# --- palette (Wispr-ish) ---
BG = "#17171b"          # near-black pill
ACCENT = "#5b8cff"      # waveform blue
MUTE = "#9aa0a6"        # secondary text
OKC = "#43d17a"         # done check green
FG = "#eaeaec"

FPS = 24
N_BARS = 5

root = tk.Tk()
root.overrideredirect(True)            # unmanaged: no decorations, no focus steal, stays put
root.attributes("-topmost", True)
try:
    root.attributes("-alpha", 0.97)
except tk.TclError:
    pass

sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
scale = max(1.0, round(sw / 1920.0))   # HiDPI: XWayland reports physical px, so scale drawing up
W, H = int(210 * scale), int(64 * scale)
MARGIN_BOTTOM = int(96 * scale)
x = (sw - W) // 2
y = sh - H - MARGIN_BOTTOM
root.geometry(f"{W}x{H}+{x}+{y}")
root.configure(bg=BG)

cv = tk.Canvas(root, width=W, height=H, bg=BG, highlightthickness=0, bd=0)
cv.pack(fill="both", expand=True)


def round_rect(c, x1, y1, x2, y2, r, **kw):
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


# rounded pill background
round_rect(cv, 1, 1, W - 1, H - 1, int(18 * scale), fill=BG, outline="#2c2c33")


def close(*_):
    try:
        root.destroy()
    except Exception:
        pass


signal.signal(signal.SIGTERM, close)
signal.signal(signal.SIGINT, close)

if MODE == "done":
    cv.create_text(int(30 * scale), H / 2, text="✓", fill=OKC,
                   font=("Sans", int(20 * scale), "bold"))
    cv.create_text(int(52 * scale), H / 2, text=(TEXT or "Inserted"), anchor="w",
                   fill=FG, font=("Sans", int(11 * scale)), width=W - int(60 * scale))
    root.after(1000, close)
else:
    # animated waveform: N bars, heights follow phase-shifted sines (smooth, pseudo-voice)
    bw, gap = int(6 * scale), int(9 * scale)
    total = N_BARS * bw + (N_BARS - 1) * gap
    x0 = (W - total) // 2
    cy = int(H * 0.44)
    phases = [i * 0.9 for i in range(N_BARS)]
    t0 = time.time()

    def tick():
        cv.delete("bar")
        t = time.time() - t0
        for i in range(N_BARS):
            amp = 0.28 + 0.72 * (0.5 + 0.5 * math.sin(t * 6.0 + phases[i]))
            bh = int((6 + amp * (H * 0.42)))
            bx = x0 + i * (bw + gap)
            round_rect(cv, bx, cy - bh // 2, bx + bw, cy + bh // 2, bw // 2,
                       fill=ACCENT, outline="", tags="bar")
        cv.create_text(W / 2, int(H * 0.80), text="Listening…", fill=MUTE,
                       font=("Sans", int(9 * scale)), tags="bar")
        root.after(int(1000 / FPS), tick)

    tick()

    # watchdog: if the daemon (our parent) dies, we get reparented -> close so no orphan pill
    _ppid0 = os.getppid()

    def _watchdog():
        if os.getppid() != _ppid0:
            close()
            return
        root.after(1000, _watchdog)

    _watchdog()

root.mainloop()
