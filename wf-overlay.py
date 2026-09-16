#!/usr/bin/env python3
"""wisprflow on-screen overlay — polished, DPI-aware, multi-monitor-correct indicators.

Modes (argv):
  listening        animated waveform + "Listening", plus three clickable buttons
                   (MeetingMode / output mode Clean->Notes->Raw / language); runs until SIGTERM
  meeting          "Meeting — recording" pill (pulsing red dot); runs until SIGTERM
  done  [TEXT]     green check + short text, auto-closes after ~1s

Environment:
  WF_MODE=<m>           active output mode: clean | note | raw (colors + labels the mode button)
  WF_NOTE_MODE=1        legacy: same as WF_MODE=note
  WF_LANG=<l>           active session language: en | de | ro
  WF_OVERLAY_SCALE=<f>  override the auto DPI scale (float); otherwise derived from screen DPI
  WF_OVERLAY_PREVIEW=<p> dev only: render one frame to PostScript at <p> and exit (no window)

Clicking MeetingMode sends `meeting` to the daemon (switch to dual-channel meeting capture).
Clicking the mode button sends `mode` — the daemon cycles clean -> note -> raw and replies
"mode <name>"; the mode persists for every dictation until changed. Borderless, bottom-center of the PRIMARY monitor, always-on-top, and
non-focus-stealing (X11 override-redirect via XWayland). tkinter only (project venv);
low-fps so it costs almost nothing.

Sizing/placement fix: tkinter's winfo_screenwidth() reports the whole VIRTUAL desktop across
all monitors (e.g. 7680 on a dual-4K setup), which made the old `round(sw/1920)` scale explode
and centered the pill across the monitor seam. We now scale from the real screen DPI and pin
the pill to the primary monitor's rectangle (parsed from xrandr).
"""
import math
import os
import re
import signal
import socket
import subprocess
import sys
import time
import tkinter as tk
import tkinter.font as tkfont

MODE = sys.argv[1] if len(sys.argv) > 1 else "listening"
TEXT = sys.argv[2] if len(sys.argv) > 2 else ""
MODE = os.environ.get("WF_MODE") or ("note" if os.environ.get("WF_NOTE_MODE") == "1" else "clean")
MODE_ORDER = ["clean", "note", "raw"]
MODE_LABEL = {"clean": "✨  Clean", "note": "📝  Notes", "raw": "🔤  Raw"}
MODE_SUB = {"clean": "press your key to stop",
            "note": "NoteMode · one line per sentence",
            "raw": "RawMode · exact words, no punctuation"}
LANG = os.environ.get("WF_LANG", "en")          # "en" | "de" | "ro" — active session language
LANG_LABEL = {"en": "🌐 EN", "de": "🌐 DE", "ro": "🌐 RO"}
PREVIEW = os.environ.get("WF_OVERLAY_PREVIEW")  # dev: postscript path, no live window

# ---- palette (dark, glassy — sits with the desktop's Win7-Aero theme) -------------------
BG      = "#0c0d11"    # window backdrop (rectangular corners; near-black + slight transparency)
CARD    = "#191c24"    # pill body
CARD_HI = "#262a35"    # top bevel highlight
BORDER  = "#39415a"
ACCENT  = "#5b93ff"    # brand blue
ACCENT2 = "#93b8ff"    # waveform highlight
OKC     = "#3ddc84"    # done check
REC     = "#ff5c5c"    # meeting record dot
FG      = "#f2f4f9"
SUBTLE  = "#96a0b4"
BTN     = "#242835"
BTN_HOV = "#2e3342"
BTN_BRD = "#404862"
NOTE_BG = "#264a86"    # mode / language button when active (non-default)
NOTE_HOV = "#2d569b"
NOTE_BRD = "#5b93ff"

FPS = 30


# ---- geometry: scale from DPI, place on the primary monitor -----------------------------
def primary_monitor(fallback):
    """(w, h, x, y) of the primary monitor from xrandr; falls back to the whole screen."""
    try:
        out = subprocess.check_output(["xrandr", "--query"], text=True, timeout=2)
    except Exception:
        return fallback
    first = None
    for line in out.splitlines():
        if " connected" not in line:
            continue
        m = re.search(r"(\d+)x(\d+)\+(\d+)\+(\d+)", line)
        if not m:
            continue
        rect = tuple(int(v) for v in m.groups())
        if " connected primary" in line:
            return rect
        if first is None:
            first = rect
    return first or fallback


root = tk.Tk()
root.withdraw()   # stay hidden until fully configured -> never flash at the wrong size/spot

try:
    dpi_scale = root.winfo_fpixels("1i") / 96.0
except Exception:
    dpi_scale = 1.0
# Fallback if xrandr is unavailable: assume ONE monitor no wider than ~16:10 of its height, so we
# never center across a multi-monitor seam (winfo_screenwidth is the whole virtual desktop width).
_sw, _sh = root.winfo_screenwidth(), root.winfo_screenheight()
MW, MH, OX, OY = primary_monitor((min(_sw, int(_sh * 16 / 10)), _sh, 0, 0))

scale = dpi_scale
if scale < 1.05:                 # DPI not reporting HiDPI -> resolution heuristic on this monitor
    scale = MH / 1080.0
try:
    scale = float(os.environ.get("WF_OVERLAY_SCALE", scale))   # dev override
except ValueError:
    pass
scale = max(0.75, min(4.0, scale))   # clamp AFTER the override so a bad value can't zero the UI


def S(v):
    return int(round(v * scale))


# ---- fonts: negative size == pixels, so text tracks our pixel-space layout exactly -------
def pick_family():
    try:
        fams = set(tkfont.families(root))
    except Exception:
        return "Sans"
    for f in ("Inter", "Cantarell", "Ubuntu", "Segoe UI", "Noto Sans", "DejaVu Sans", "Sans"):
        if f in fams:
            return f
    return "Sans"


FAM = pick_family()
F_TITLE = tkfont.Font(root=root, family=FAM, size=-S(14), weight="bold")
F_SUB   = tkfont.Font(root=root, family=FAM, size=-S(9))
F_BTN   = tkfont.Font(root=root, family=FAM, size=-S(11), weight="bold")
F_DONE  = tkfont.Font(root=root, family=FAM, size=-S(12))


def round_rect(c, x1, y1, x2, y2, r, **kw):
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


# ---- per-mode base sizes (unscaled units) -----------------------------------------------
if MODE == "listening":
    BASE_W, BASE_H = 372, 123
elif MODE == "meeting":
    BASE_W, BASE_H = 264, 62
elif MODE == "processing":
    BASE_W, BASE_H = 244, 62
else:  # done
    BASE_W, BASE_H = 300, 58

W, H = S(BASE_W), S(BASE_H)
MARGIN = S(60)                         # gap above the taskbar / screen bottom
x = OX + (MW - W) // 2
y = OY + MH - H - MARGIN

root.overrideredirect(True)
root.attributes("-topmost", True)
try:
    root.attributes("-alpha", 0.98)
except tk.TclError:
    pass
root.geometry(f"{W}x{H}+{x}+{y}")
root.configure(bg=BG)
cv = tk.Canvas(root, width=W, height=H, bg=BG, highlightthickness=0, bd=0)
cv.pack(fill="both", expand=True)


def draw_card():
    """The rounded pill body: soft border + a thin top bevel for a glassy, non-cheap feel."""
    pad = S(2)
    r = S(16)
    round_rect(cv, pad, pad, W - pad, H - pad, r, fill=CARD, outline=BORDER, width=max(1, S(1)))
    # top bevel highlight line, inset a touch
    cv.create_line(pad + r, pad + max(1, S(1)), W - pad - r, pad + max(1, S(1)),
                   fill=CARD_HI, width=max(1, S(1)))


draw_card()


def close(*_):
    try:
        root.destroy()
    except Exception:
        pass


signal.signal(signal.SIGTERM, close)
signal.signal(signal.SIGINT, close)

_ppid0 = os.getppid()


def watchdog():
    """Close if the daemon (our parent) dies, so no orphan pill lingers."""
    if os.getppid() != _ppid0:
        close()
        return
    root.after(1000, watchdog)


def send_cmd(cmd: bytes) -> str:
    """Send one command to the daemon socket; return its reply (or '' on failure)."""
    try:
        rt = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(os.path.join(rt, "wf-daemon.sock"))
        s.sendall(cmd)
        reply = s.recv(64).decode("utf-8", "replace")
        s.close()
        return reply
    except Exception:
        return ""


# =========================================================================================
# DONE — green check + short text, auto-closes
# =========================================================================================
if MODE == "done":
    cx = S(30)
    cv.create_oval(cx - S(12), H / 2 - S(12), cx + S(12), H / 2 + S(12),
                   fill="", outline=OKC, width=max(1, S(2)))
    cv.create_text(cx, H / 2, text="✓", fill=OKC, font=tkfont.Font(root=root, family=FAM,
                   size=-S(14), weight="bold"))
    cv.create_text(S(54), H / 2, text=(TEXT or "Inserted"), anchor="w", fill=FG,
                   font=F_DONE, width=W - S(66))
    if not PREVIEW:
        root.after(1000, close)


# =========================================================================================
# MEETING — pulsing red dot + status text
# =========================================================================================
elif MODE == "meeting":
    dotx, cy = S(32), H // 2
    cv.create_text(S(56), int(H * 0.36), text="Meeting", anchor="w", fill=FG, font=F_TITLE)
    cv.create_text(S(56), int(H * 0.68), text="recording · press your key to stop",
                   anchor="w", fill=SUBTLE, font=F_SUB)
    t0 = time.time()

    def pulse():
        cv.delete("dot")
        a = 0.5 + 0.5 * math.sin((time.time() - t0) * 4.0)
        # soft outer glow ring + solid core
        rr = int(S(7) + a * S(4))
        cv.create_oval(dotx - rr - S(3), cy - rr - S(3), dotx + rr + S(3), cy + rr + S(3),
                       fill="", outline="#5a2323", width=max(1, S(1)), tags="dot")
        cv.create_oval(dotx - rr, cy - rr, dotx + rr, cy + rr, fill=REC, outline="", tags="dot")
        root.after(int(1000 / FPS), pulse)

    pulse()
    watchdog()


# =========================================================================================
# PROCESSING — spinner + "Processing…" (shown after recording stops, while transcribing)
# =========================================================================================
elif MODE == "processing":
    cx, cy = S(30), H // 2
    rr = S(11)
    cv.create_text(S(52), H / 2, text="Processing…", anchor="w", fill=FG, font=F_TITLE)
    t0 = time.time()

    def spin():
        cv.delete("spin")
        ang = (time.time() - t0) * 320.0 % 360      # rotating 3/4 arc
        cv.create_arc(cx - rr, cy - rr, cx + rr, cy + rr, start=ang, extent=270,
                      style="arc", outline=ACCENT, width=max(2, S(2)), tags="spin")
        root.after(int(1000 / FPS), spin)

    spin()
    watchdog()


# =========================================================================================
# LISTENING — waveform + label + MeetingMode / output-mode / language buttons
# =========================================================================================
else:
    mode = [MODE if MODE in MODE_LABEL else "clean"]   # mutable so the click handler can cycle it
    lang = [LANG if LANG in LANG_LABEL else "en"]   # active language (mutable for click handler)

    PADX = S(14)
    wave_w = S(46)
    wave_cx = PADX + wave_w // 2
    wave_cy = H // 2
    lbl_x = PADX + wave_w + S(16)

    btn_w = S(156)
    btn_h = S(28)
    bgap = S(9)
    bx2 = W - PADX
    bx1 = bx2 - btn_w
    stack_h = btn_h * 3 + bgap * 2
    top = (H - stack_h) // 2
    meet_y1, meet_y2 = top, top + btn_h
    note_y1, note_y2 = top + btn_h + bgap, top + btn_h + bgap + btn_h
    lang_y1, lang_y2 = top + 2 * (btn_h + bgap), top + 2 * (btn_h + bgap) + btn_h

    # IMPORTANT: the button/label items are created ONCE here and only ever RECONFIGURED
    # (itemconfigure) on hover/toggle — never deleted+recreated. Recreating an item that the
    # cursor is over, from inside its own <Enter> handler, makes tkinter re-fire <Leave>+<Enter>
    # forever (a redraw storm that pegs the CPU and freezes clicks). Reconfiguring in place has
    # no such feedback loop.

    # ---- left label (title + subtitle that names the active output mode) ----
    cv.create_text(lbl_x, int(H * 0.40), text="Listening", anchor="w", fill=FG, font=F_TITLE)
    sub_id = cv.create_text(lbl_x, int(H * 0.68), text="", anchor="w", font=F_SUB)

    def set_label():
        m = mode[0]
        cv.itemconfigure(sub_id, text=MODE_SUB[m], fill=(ACCENT2 if m != "clean" else SUBTLE))

    # ---- buttons (created once; hover/toggle only recolor via itemconfigure) ----
    round_rect(cv, bx1, meet_y1, bx2, meet_y2, S(9), fill=BTN, outline=BTN_BRD,
               width=max(1, S(1)), tags=("meet", "meet_bg"))
    cv.create_text((bx1 + bx2) // 2, (meet_y1 + meet_y2) // 2, text="👥  MeetingMode",
                   fill=FG, font=F_BTN, tags=("meet", "meet_tx"))
    round_rect(cv, bx1, note_y1, bx2, note_y2, S(9), fill=BTN, outline=BTN_BRD,
               width=max(1, S(1)), tags=("mode", "mode_bg"))
    cv.create_text((bx1 + bx2) // 2, (note_y1 + note_y2) // 2, text="", font=F_BTN,
                   tags=("mode", "mode_tx"))
    round_rect(cv, bx1, lang_y1, bx2, lang_y2, S(9), fill=BTN, outline=BTN_BRD,
               width=max(1, S(1)), tags=("lang", "lang_bg"))
    cv.create_text((bx1 + bx2) // 2, (lang_y1 + lang_y2) // 2, text="", font=F_BTN,
                   tags=("lang", "lang_tx"))

    def set_meet(hover=False):
        cv.itemconfigure("meet_bg", fill=(BTN_HOV if hover else BTN))

    def set_mode(hover=False):
        m = mode[0]
        on = m != "clean"   # clean is the default — only Notes/Raw get accent treatment
        fill = (NOTE_HOV if hover else NOTE_BG) if on else (BTN_HOV if hover else BTN)
        cv.itemconfigure("mode_bg", fill=fill, outline=(NOTE_BRD if on else BTN_BRD))
        cv.itemconfigure("mode_tx", text=MODE_LABEL[m], fill=(FG if on else SUBTLE))

    def set_lang(hover=False):
        cur = lang[0]
        active = cur != "en"   # EN is the default — only DE/RO get accent treatment
        fill = (NOTE_HOV if hover else NOTE_BG) if active else (BTN_HOV if hover else BTN)
        cv.itemconfigure("lang_bg", fill=fill, outline=(NOTE_BRD if active else BTN_BRD))
        cv.itemconfigure("lang_tx", text=LANG_LABEL.get(cur, "🌐 EN"),
                         fill=(FG if active else SUBTLE))

    def on_meeting(*_):
        send_cmd(b"meeting")
        close()   # daemon relaunches this overlay in meeting mode

    def on_mode(*_):
        reply = send_cmd(b"mode")
        # reply looks like "mode note"
        new = reply.split()[-1] if reply.startswith("mode ") else None
        if new in MODE_LABEL:
            mode[0] = new
        else:
            # optimistic fallback: cycle locally if the reply was lost
            mode[0] = MODE_ORDER[(MODE_ORDER.index(mode[0]) + 1) % len(MODE_ORDER)]
        set_mode(hover=True)
        set_label()

    def on_lang(*_):
        reply = send_cmd(b"lang")
        # reply looks like "lang de"
        new = reply.split()[-1] if reply.startswith("lang ") else None
        if new in LANG_LABEL:
            lang[0] = new
        else:
            # optimistic fallback: cycle locally
            order = ["en", "de", "ro"]
            lang[0] = order[(order.index(lang[0]) + 1) % len(order)]
        set_lang(hover=True)

    set_label()
    set_meet()
    set_mode()
    set_lang()

    cv.tag_bind("meet", "<Button-1>", on_meeting)
    cv.tag_bind("meet", "<Enter>", lambda e: (set_meet(True), cv.config(cursor="hand2")))
    cv.tag_bind("meet", "<Leave>", lambda e: (set_meet(False), cv.config(cursor="")))
    cv.tag_bind("mode", "<Button-1>", on_mode)
    cv.tag_bind("mode", "<Enter>", lambda e: (set_mode(True), cv.config(cursor="hand2")))
    cv.tag_bind("mode", "<Leave>", lambda e: (set_mode(False), cv.config(cursor="")))
    cv.tag_bind("lang", "<Button-1>", on_lang)
    cv.tag_bind("lang", "<Enter>", lambda e: (set_lang(True), cv.config(cursor="hand2")))
    cv.tag_bind("lang", "<Leave>", lambda e: (set_lang(False), cv.config(cursor="")))

    # ---- animated waveform ----
    nb = 7
    bw = S(5)
    gap = S(4)
    total = nb * bw + (nb - 1) * gap
    x0 = wave_cx - total // 2
    phases = [i * 0.8 for i in range(nb)]
    t0 = time.time()

    def tick():
        cv.delete("bar")
        t = time.time() - t0
        for i in range(nb):
            amp = 0.22 + 0.78 * (0.5 + 0.5 * math.sin(t * 6.0 + phases[i]))
            bh = int(S(6) + amp * (H * 0.44))
            bx = x0 + i * (bw + gap)
            col = ACCENT if i % 2 == 0 else ACCENT2
            round_rect(cv, bx, wave_cy - bh // 2, bx + bw, wave_cy + bh // 2, bw // 2,
                       fill=col, outline="", tags="bar")
        root.after(int(1000 / FPS), tick)

    tick()
    watchdog()


# ---- preview (dev): dump one frame to PostScript and exit; else run the UI ---------------
if PREVIEW:
    root.update()
    try:
        cv.postscript(file=PREVIEW, colormode="color", x=0, y=0, width=W, height=H,
                      pagewidth=W, pageheight=H)
    except Exception as e:
        sys.stderr.write(f"preview failed: {e!r}\n")
    close()
else:
    root.deiconify()
    root.mainloop()
