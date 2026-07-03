#!/usr/bin/env python3
"""
local-wisprflow daemon — a fully-local "wait-then-polish" dictation service.

Pipeline (mirrors Wispr Flow conceptually, 100% offline):

    mic ─▶ record (toggle / optional energy-VAD auto-stop)
        ─▶ faster-whisper ASR (raw transcript)
        ─▶ Ollama LLM cleanup (grammar / punctuation / filler removal)
        ─▶ inject into the focused window (ydotool type, or clipboard paste)

The daemon stays resident with the Whisper model warm in VRAM and listens on a
Unix socket. A tiny client (`wf-toggle`) sends one-word commands:

    toggle | start | stop | cancel | status | ping | shutdown

Design: one recording session at a time. `toggle` starts a session when idle and
stops (finalizes) it while recording. Recording happens in a worker thread using a
blocking sounddevice read loop, so there are no PortAudio-callback threading hazards.
"""
from __future__ import annotations

import gc
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Configuration (defaults; override via ~/.config/wisprflow/config.json)
# ---------------------------------------------------------------------------
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
SOCKET_PATH = os.path.join(RUNTIME_DIR, "wf-daemon.sock")

DEFAULTS = {
    # --- ASR (faster-whisper) ---
    # DEFAULT = CPU: this box shares its 12GB GPU with a resident 14B research harness
    # (~10GB VRAM). Running whisper on CPU costs ZERO VRAM and never competes with it.
    # large-v3 int8 on this 24-thread CPU runs at ~0.47x realtime (fast enough) with full
    # accuracy. Set asr_device="cuda" ONLY when the harness is idle (holding 3GB of VRAM
    # for whisper would prevent the harness's 14B from loading).
    "asr_model": "large-v3",          # large-v3 | distil-large-v3 | medium | small ...
    "asr_device": "cpu",              # cpu | cuda | auto
    # "auto" = adaptive: run whisper on the GPU when no big LLM is loaded there (fast), and
    # fall back to CPU (releasing its VRAM) whenever another process holds a large model on
    # the GPU. A CPU model is always kept warm, so there is never any downtime while switching.
    "asr_compute_type": "int8",       # CPU compute type (used for cpu mode & the auto CPU baseline)
    "asr_gpu_compute_type": "int8_float16",  # GPU compute type (cuda mode & auto's GPU model)
    "asr_cpu_threads": 0,             # 0 = ctranslate2 default (all physical cores)
    "asr_auto_other_vram_mib": 5000,  # auto: if any OTHER single GPU process holds > this many
                                      # MiB (i.e. a big LLM like the 14B), whisper -> CPU. Small
                                      # models (3B cleaner ~2GB, the agent ~2GB) stay below it.
    "asr_auto_poll_secs": 6,          # auto: how often to re-check GPU occupancy (while on GPU)
    "gpu_idle_timeout_s": 300,        # auto: after this long with no dictation, unload whisper from
                                      # the GPU so the dGPU can auto-suspend (D3cold, 0W) to save power
    "harness_ollama_url": "http://localhost:11434",  # system Ollama — checked via /api/ps (an HTTP
                                      # call, NOT nvidia-smi) to detect the harness's 14B without
                                      # waking the sleeping dGPU
    "language": "en",                 # None (or "") => auto-detect
    "beam_size": 5,
    "initial_prompt": "",             # bias vocabulary: names/jargon, e.g. "Ollama, ctranslate2, ..."
    "vad_filter": True,               # faster-whisper's own onnx silero, trims silence at transcribe time

    # --- recording ---
    "sample_rate": 16000,
    "input_device": None,             # None = system default input (index or name string also ok)
    "block_ms": 100,
    "max_seconds": 120,               # hard safety cap on a single utterance
    "auto_stop": False,               # energy-VAD auto-stop (else press hotkey again to stop)
    "vad_rms_threshold": 0.010,       # RMS above this = speech (float32 [-1,1]); tune per mic
    "silence_ms": 900,                # stop after this much trailing silence (auto_stop only)

    # --- LLM cleanup (Ollama) ---
    # DEFAULT = qwen2.5:14b (NOT 7b): the system Ollama runs with OLLAMA_KV_CACHE_TYPE=q4_0
    # (needed to keep the harness's 14B on-GPU at 32k). 4-bit KV cache turns 7B output into
    # garbage, but 14B tolerates it and cleans up perfectly. Using the 14B also SHARES the
    # harness's already-loaded model (often warm -> fast, zero extra VRAM). Do NOT send
    # num_ctx here -> that would force a reload and fight the harness's 32k instance.
    "llm_enable": True,
    "ollama_url": "http://localhost:11434",
    "llm_model": "qwen2.5:14b",
    "llm_temperature": 0.2,
    "llm_timeout": 60,
    "llm_keep_alive": "5m",           # match OLLAMA_KEEP_ALIVE; don't shorten the harness's window
    "llm_system": (
        "You are a dictation post-processor. Rewrite the user's raw speech transcript "
        "into clean, well-punctuated text. Fix grammar, capitalization, and remove filler "
        "words (um, uh, you know, like). Preserve the speaker's meaning and wording. "
        "Do not answer questions or add commentary. Output ONLY the corrected text — "
        "no preamble, no quotes, no explanations."
    ),

    # --- injection ---
    # "type"  = LAYOUT-AWARE typing (maps chars to the correct keycodes for your XKB layout via
    #           libxkbcommon) — correct on any layout AND works in every app (terminal or GUI) with
    #           no paste chord. Recommended. "paste" = wl-copy + a paste chord (needs the right chord
    #           per app: ctrl+v for GUIs, ctrl+shift+v for terminals). "clipboard" = just copy.
    "inject_method": "type",
    "paste_chord": "ctrl+v",          # ctrl+v | ctrl+shift+v (terminals) | shift+insert
    "ydotool_bin": "ydotool",
    "ydotool_socket": os.path.join(RUNTIME_DIR, ".ydotool_socket"),
    "key_delay_ms": 4,                # per-keystroke delay for `ydotool type`
    "trailing_space": True,           # append a space so consecutive dictations don't run together

    # --- meeting mode (dual-channel: mic = "Me", system-audio monitor = "Client") ---
    "meeting_dir": "~/wf-meetings",   # timestamped transcript .md files go here
    "meeting_vad_floor": 0.02,        # energy-VAD speech threshold (RMS) — tune per mic/room
    "meeting_silence_ms": 700,        # trailing silence that closes an utterance
    "meeting_min_speech_ms": 300,     # ignore speech blips shorter than this
    "meeting_max_seg_s": 24,          # force-flush a monologue after this many seconds
    "meeting_beam_size": 3,           # transcription beam for meetings (quality vs speed)

    # --- feedback ---
    "overlay": True,                  # animated on-screen listening pill + 1s "inserted" flash
    "notify": False,                  # desktop notifications via notify-send (overlay replaces these)
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    path = Path.home() / ".config" / "wisprflow" / "config.json"
    if path.exists():
        try:
            user = json.loads(path.read_text())
            cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
            log(f"loaded config overrides from {path}: {sorted(user.keys())}")
        except Exception as e:  # noqa: BLE001
            log(f"WARNING: could not read {path}: {e!r}; using defaults")
    return cfg


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def notify(cfg: dict, title: str, body: str = "") -> None:
    if not cfg.get("notify"):
        return
    try:
        subprocess.Popen(
            ["notify-send", "-a", "wisprflow", "-t", "1500", title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------
IDLE, RECORDING, PROCESSING, MEETING = "idle", "recording", "processing", "meeting"

# ydotool key sequences as evdev codes (leftctrl=29, leftshift=42, v=47, insert=110).
# These are PHYSICAL keys — identical on US / German-QWERTZ / any layout — so pasting inserts
# the clipboard's exact Unicode text regardless of keyboard layout. (By contrast `ydotool type`
# emits US-layout keycodes and mistypes on non-US layouts, e.g. y<->z and ?->_ on German.)
PASTE_CHORDS = {
    "ctrl+v":       ["29:1", "47:1", "47:0", "29:0"],
    "ctrl+shift+v": ["29:1", "42:1", "47:1", "47:0", "42:0", "29:0"],  # most terminals
    "shift+insert": ["42:1", "110:1", "110:0", "42:0"],
}


class Daemon:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.state = IDLE
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.cancel_flag = False
        self.asr = None
        self.asr_device = None  # label after load: cpu | cuda | auto(cpu) | auto(cuda)
        self._cpu_model = None
        self._gpu_model = None
        self.active_device = None
        self.model_lock = threading.Lock()  # guards model swaps vs. an in-flight transcription
        self._last_activity = 0.0           # monotonic time of the last dictation (for idle-unload)
        self._wake_monitor = threading.Event()  # nudges the auto monitor to re-evaluate now
        self._srv = None
        self._shutdown_requested = False
        self._overlay = None  # the listening-overlay subprocess (or None)
        self._meeting = None  # active MeetingSession (or None)
        self._charmap = None      # cached char->keycode map for layout-aware typing
        self._charmap_key = None  # (layout, variant) the cached map was built for

    # -- model ----------------------------------------------------------------
    def _make_model(self, device: str, compute_type: str):
        """Load a WhisperModel on `device` and warm its kernels so the first real
        transcription is fast (not a multi-second cold JIT)."""
        from faster_whisper import WhisperModel
        cfg = self.cfg
        threads = int(cfg.get("asr_cpu_threads", 0))
        m = WhisperModel(cfg["asr_model"], device=device, compute_type=compute_type,
                         cpu_threads=threads)
        try:
            warm = np.zeros(int(cfg["sample_rate"] * 0.5), dtype=np.float32)
            list(m.transcribe(warm, language=cfg["language"] or None)[0])
        except Exception as e:  # noqa: BLE001
            # a failed warmup means this model can't actually transcribe (e.g. GPU libs not
            # found). Surface it so the auto monitor / cuda fallback don't adopt a broken model.
            log(f"warmup failed ({device}): {e!r}")
            raise
        return m

    def load_model(self) -> None:
        cfg = self.cfg
        dev = cfg["asr_device"]
        t0 = time.time()
        if dev == "auto":
            # Keep a CPU model warm as the always-available baseline; a monitor thread
            # promotes whisper to the GPU when the GPU is free and demotes it (freeing VRAM)
            # when a big model appears there. No downtime: the CPU model handles dictations
            # while the GPU model loads in the background.
            log(f"loading faster-whisper '{cfg['asr_model']}' CPU baseline (adaptive auto mode)...")
            self._cpu_model = self._make_model("cpu", cfg["asr_compute_type"])
            self.asr = self._cpu_model
            self.active_device = "cpu"
            self.asr_device = "auto(cpu)"
            log(f"ASR ready (auto mode) in {time.time() - t0:.1f}s; monitor will use GPU when free")
            threading.Thread(target=self._asr_monitor, daemon=True).start()
            return
        # fixed cpu / cuda modes
        ct = cfg["asr_gpu_compute_type"] if dev == "cuda" else cfg["asr_compute_type"]
        try:
            log(f"loading faster-whisper '{cfg['asr_model']}' on {dev} ({ct}) ...")
            self.asr = self._make_model(dev, ct)
            self.asr_device = dev
        except Exception as e:  # noqa: BLE001
            log(f"'{dev}' model load failed ({e!r}); falling back to CPU int8")
            self.asr = self._make_model("cpu", "int8")
            self.asr_device = "cpu"
        self.active_device = self.asr_device
        log(f"ASR ready on {self.asr_device} in {time.time() - t0:.1f}s")

    # -- adaptive GPU/CPU placement (auto mode) -------------------------------
    def _gpu_biggest_other_mib(self) -> int:
        """Largest VRAM chunk held by a SINGLE process other than this daemon. A big LLM
        (the harness's 14B) appears as one ~6-9 GB process; small models (the 3B cleaner,
        the agent's 2 GB, etc.) never individually cross the threshold, so whisper only
        yields to a genuine big model. Returns -1 if unqueryable (placement left unchanged)."""
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid,used_memory",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
            mine, biggest = os.getpid(), 0
            for line in out.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) != mine:
                    try:
                        biggest = max(biggest, int(parts[1]))
                    except ValueError:
                        pass
            return biggest
        except Exception as e:  # noqa: BLE001
            log(f"gpu occupancy query failed: {e!r}")
            return -1

    def mark_activity(self) -> None:
        """Record dictation activity (record-start / a transcription) and wake the monitor so it
        promotes whisper to the GPU promptly (and resets the idle-unload timer)."""
        self._last_activity = time.monotonic()
        self._wake_monitor.set()

    def _harness_loaded(self) -> bool:
        """True if the system Ollama (the harness) has a big (~14B) model resident. Uses /api/ps —
        an HTTP call, NOT nvidia-smi — so it never wakes a sleeping dGPU."""
        import requests
        big = int(self.cfg.get("asr_auto_other_vram_mib", 5000)) * 1024 * 1024
        try:
            r = requests.get(f"{self.cfg['harness_ollama_url']}/api/ps", timeout=2)
            for m in r.json().get("models", []):
                if (m.get("size_vram") or m.get("size") or 0) > big:
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _demote_to_cpu(self, why: str) -> None:
        with self.model_lock:
            self.asr = self._cpu_model
            self.active_device, self.asr_device = "cpu", "auto(cpu)"
            self._gpu_model = None
        gc.collect()   # free the GPU VRAM -> dGPU can auto-suspend (D3cold) if nothing else uses it
        log(f"auto: {why} -> whisper on CPU (GPU model released)")

    def _asr_monitor(self) -> None:
        """Activity-driven GPU/CPU placement:
          * whisper on the GPU only while there is RECENT dictation activity and no big LLM there;
          * demote to CPU when the harness's 14B appears OR after `gpu_idle_timeout_s` idle, freeing
            VRAM so the dGPU can sleep (0 W);
          * a warm CPU model is always kept, so a transcription is never blocked.
        Crucially, while whisper is OFF the GPU we detect the harness via Ollama /api/ps (HTTP) and
        never call nvidia-smi, so we don't keep waking a sleeping dGPU."""
        cfg = self.cfg
        threshold = int(cfg.get("asr_auto_other_vram_mib", 5000))
        poll = max(2, int(cfg.get("asr_auto_poll_secs", 6)))
        idle_timeout = int(cfg.get("gpu_idle_timeout_s", 300))
        while not self._shutdown_requested:
            idle = (time.monotonic() - self._last_activity) > idle_timeout
            if self.active_device == "cuda":
                # dGPU is already awake (whisper resident) -> nvidia-smi is free to poll.
                big = self._gpu_biggest_other_mib()
                if big >= threshold:
                    self._demote_to_cpu(f"big model on GPU ({big} MiB) — yielding VRAM")
                elif idle:
                    self._demote_to_cpu(f"idle >{idle_timeout}s — powering down GPU")
                wait = float(poll)
            else:
                # whisper is on CPU; the dGPU may be ASLEEP. Only promote when there's recent
                # activity AND the harness isn't on the GPU — and detect that via HTTP, not nvidia-smi.
                if not idle and not self._harness_loaded():
                    try:
                        gpu = self._make_model("cuda", cfg["asr_gpu_compute_type"])
                    except Exception as e:  # noqa: BLE001
                        log(f"auto: GPU promote failed ({e!r}); staying on CPU")
                        gpu = None
                    if gpu is not None:
                        with self.model_lock:
                            self._gpu_model, self.asr = gpu, gpu
                            self.active_device, self.asr_device = "cuda", "auto(cuda)"
                        log("auto: active + GPU free -> whisper on GPU (fast)")
                wait = 30.0 if idle else float(poll)
            self._wake_monitor.wait(timeout=wait)
            self._wake_monitor.clear()

    # -- audio capture --------------------------------------------------------
    def record(self) -> np.ndarray:
        import sounddevice as sd
        cfg = self.cfg
        sr = int(cfg["sample_rate"])
        block = max(1, int(sr * cfg["block_ms"] / 1000))
        frames: list[np.ndarray] = []
        silence_ms = 0.0
        had_speech = False
        max_frames = int(cfg["max_seconds"] * sr / block)
        log("recording...")
        try:
            with sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                                blocksize=block, device=cfg["input_device"]) as stream:
                while not self.stop_event.is_set():
                    data, overflowed = stream.read(block)
                    chunk = data[:, 0].copy()
                    frames.append(chunk)
                    rms = float(np.sqrt(np.mean(chunk ** 2)) if chunk.size else 0.0)
                    if rms >= cfg["vad_rms_threshold"]:
                        had_speech = True
                        silence_ms = 0.0
                    else:
                        silence_ms += cfg["block_ms"]
                    if cfg["auto_stop"] and had_speech and silence_ms >= cfg["silence_ms"]:
                        log("auto-stop (silence)")
                        break
                    if len(frames) >= max_frames:
                        log("max-duration reached")
                        break
        except Exception as e:  # noqa: BLE001
            log(f"ERROR capturing audio: {e!r}")
        if not frames:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(frames)
        log(f"captured {audio.size / sr:.1f}s")
        return audio

    # -- ASR ------------------------------------------------------------------
    def transcribe(self, audio: np.ndarray) -> str:
        cfg = self.cfg
        self.mark_activity()   # keep whisper on the GPU while dictation is happening
        if audio.size < int(0.2 * cfg["sample_rate"]):
            return ""
        t0 = time.time()
        # hold model_lock so the auto-mode monitor can't swap/free the model mid-transcription
        with self.model_lock:
            segments, info = self.asr.transcribe(
                audio,
                language=cfg["language"] or None,
                beam_size=cfg["beam_size"],
                vad_filter=cfg["vad_filter"],
                condition_on_previous_text=False,
                initial_prompt=cfg["initial_prompt"] or None,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        log(f"ASR [{self.active_device}] {time.time() - t0:.2f}s -> {text!r}")
        return text

    # -- LLM ------------------------------------------------------------------
    def polish(self, raw: str) -> str:
        cfg = self.cfg
        if not cfg["llm_enable"] or not raw.strip():
            return raw
        import requests
        t0 = time.time()
        try:
            r = requests.post(
                f"{cfg['ollama_url']}/api/generate",
                json={
                    "model": cfg["llm_model"],
                    "system": cfg["llm_system"],
                    "prompt": raw,
                    "stream": False,
                    "keep_alive": cfg["llm_keep_alive"],
                    # NOTE: deliberately no "num_ctx" — reuse whatever instance the harness
                    # has loaded (avoids forcing a context-size reload that fights the harness).
                    "options": {"temperature": cfg["llm_temperature"]},
                },
                timeout=cfg["llm_timeout"],
            )
            r.raise_for_status()
            out = (r.json().get("response") or "").strip()
            out = self._strip_wrapping(out)
            log(f"LLM {time.time() - t0:.2f}s -> {out!r}")
            return out or raw
        except Exception as e:  # noqa: BLE001
            log(f"LLM cleanup failed ({e!r}); using raw transcript")
            return raw

    @staticmethod
    def _strip_wrapping(text: str) -> str:
        t = text.strip()
        # strip a single layer of surrounding quotes the model sometimes adds
        for q in ('"', "'", "“", "”"):
            if len(t) >= 2 and t[0] == q and t[-1] == q:
                t = t[1:-1].strip()
                break
        return t

    def _get_charmap(self):
        """Char->keycode map for the CURRENT XKB layout, rebuilt if the layout changed."""
        import wf_layout
        key = wf_layout.get_current_layout()
        if self._charmap is None or self._charmap_key != key:
            self._charmap = wf_layout.build_charmap(*key)
            self._charmap_key = key
            log(f"typing: layout {key[0]}+{key[1] or ''} ({len(self._charmap)} chars mapped)")
        return self._charmap

    # -- injection ------------------------------------------------------------
    def inject(self, text: str) -> str:
        """Insert `text`; returns the method actually used ('type'/'paste'/'clipboard'/'')."""
        cfg = self.cfg
        if not text:
            return ""
        if cfg["trailing_space"]:
            text = text + " "
        method = cfg["inject_method"]
        # Graceful degrade: if ydotoold isn't up yet (e.g. before the first logout/in that
        # activates the 'input' group), auto-fall back to clipboard so dictation still works.
        if method in ("type", "paste") and not os.path.exists(cfg["ydotool_socket"]):
            log(f"ydotoold socket {cfg['ydotool_socket']} not present -> clipboard fallback "
                "(log out/in once to enable auto-typing)")
            method = "clipboard"
        env = os.environ.copy()
        env["YDOTOOL_SOCKET"] = cfg["ydotool_socket"]
        try:
            if method == "clipboard":
                subprocess.run(["wl-copy"], input=text.encode(), check=False)
                log("copied to clipboard (paste with Ctrl+V)")
                return "clipboard"
            if method == "paste":
                chord = PASTE_CHORDS.get(cfg.get("paste_chord", "ctrl+v"),
                                         PASTE_CHORDS["ctrl+v"])
                subprocess.run(["wl-copy"], input=text.encode(), check=False)
                time.sleep(0.05)  # let the clipboard manager register the new selection
                subprocess.run([cfg["ydotool_bin"], "key", *chord], env=env, check=False)
                return "paste"
            # default: type — LAYOUT-AWARE. We map each character to the (evdev keycode, level)
            # that produces it under the user's actual XKB layout and emit those raw codes, so
            # the text lands correctly in EVERY app (terminal or GUI) with no paste chord and no
            # clipboard use. (Plain `ydotool type` assumes US layout and mistypes on e.g. German.)
            import wf_layout
            events, skipped = wf_layout.key_events(self._get_charmap(), text)
            if skipped:
                log(f"typing: skipped unmappable char(s): {skipped[:8]}")
            delay = str(cfg.get("key_delay_ms", 4))
            for i in range(0, len(events), 400):   # chunk to keep argv sane
                subprocess.run([cfg["ydotool_bin"], "key", "--key-delay", delay]
                               + events[i:i + 400], env=env, check=False)
            return "type"
        except FileNotFoundError as e:
            log(f"injection tool missing: {e}. Copying to clipboard instead.")
            subprocess.run(["wl-copy"], input=text.encode(), check=False)
            return "clipboard"

    # -- on-screen overlay (listening pill + 1s done flash) -------------------
    def _overlay_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "wf-overlay.py")

    def _overlay_start(self, mode: str = "listening") -> None:
        if not self.cfg.get("overlay", True):
            return
        self._overlay_stop()
        try:
            self._overlay = subprocess.Popen(
                [sys.executable, self._overlay_path(), mode],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001
            log(f"overlay start failed: {e!r}")
            self._overlay = None

    def _overlay_stop(self) -> None:
        p, self._overlay = self._overlay, None
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass

    def _overlay_done(self, text: str) -> None:
        if not self.cfg.get("overlay", True):
            return
        try:
            subprocess.Popen(
                [sys.executable, self._overlay_path(), "done", (text or "Inserted")[:44]],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass

    # -- meeting mode (dual-channel Me/Client transcription) ------------------
    def _cmd_meeting(self) -> str:
        """Overlay 'Meeting' button (or `wf-toggle meeting`) -> switch this session to meeting."""
        with self.lock:
            if self.state == MEETING:
                return "already meeting"
            if self.state == PROCESSING:
                return "busy"
            if self.state == RECORDING:
                self.cancel_flag = True   # abandon the in-flight normal recording
                self.stop_event.set()
        threading.Thread(target=self._enter_meeting, daemon=True).start()
        return "meeting"

    def _enter_meeting(self) -> None:
        for _ in range(100):              # wait up to ~5s for any normal session to unwind
            with self.lock:
                if self.state == IDLE:
                    break
            time.sleep(0.05)
        self.start_meeting()

    def start_meeting(self) -> None:
        with self.lock:
            if self.state != IDLE:
                return
            self.state = MEETING
        try:
            from wf_meeting import MeetingSession
        except Exception as e:  # noqa: BLE001
            log(f"meeting: import failed: {e!r}")
            with self.lock:
                self.state = IDLE
            return
        self._overlay_start(mode="meeting")
        self._meeting = MeetingSession(self, log)
        if not self._meeting.start():
            self._meeting = None
            self._overlay_stop()
            self._overlay_done("⚠ meeting: no audio")
            with self.lock:
                self.state = IDLE
            return
        log("meeting mode ON")

    def stop_meeting(self) -> None:
        m, self._meeting = self._meeting, None
        path = m.stop() if m else None
        self._overlay_stop()
        with self.lock:
            if self.state == MEETING:
                self.state = IDLE
        if path:
            self._overlay_done(f"Saved {os.path.basename(path)}")
        log("meeting mode OFF")

    # -- session --------------------------------------------------------------
    def run_session(self) -> None:
        cfg = self.cfg
        try:
            self._overlay_start()  # animated listening pill
            audio = self.record()
            # atomically decide cancel-vs-proceed under the lock so a `cancel` arriving
            # exactly at the record->process boundary can't be silently dropped.
            with self.lock:
                cancelled = self.cancel_flag
                if not cancelled:
                    self.state = PROCESSING
            if cancelled:
                log("session cancelled")
                return
            raw = self.transcribe(audio)
            if not raw:
                log("empty transcript; nothing to inject")
                return
            polished = self.polish(raw)
            used = self.inject(polished)
            self._overlay_stop()
            self._overlay_done("Copied · Ctrl+V" if used == "clipboard" else polished)
            if cfg.get("notify"):
                notify(cfg, "✓ Inserted", polished[:80])
        except Exception as e:  # noqa: BLE001
            log(f"session error: {e!r}")
            self._overlay_done("⚠ error")
        finally:
            self._overlay_stop()
            with self.lock:
                self.state = IDLE
                self.cancel_flag = False

    # -- command handling -----------------------------------------------------
    def handle(self, cmd: str) -> str:
        cmd = cmd.strip().lower()
        if cmd == "ping":
            return f"pong ({self.asr_device or 'loading'})"
        if cmd == "status":
            return self.state
        if cmd == "shutdown":
            threading.Thread(target=self._shutdown, daemon=True).start()
            return "shutting down"
        if cmd == "cancel":
            with self.lock:
                if self.state == RECORDING:
                    self.cancel_flag = True
                    self.stop_event.set()
                    return "cancelling"
            return self.state
        if cmd == "meeting":
            return self._cmd_meeting()
        if cmd in ("toggle", "start", "stop"):
            return self._toggle(cmd)
        return f"unknown command: {cmd}"

    def _toggle(self, cmd: str) -> str:
        with self.lock:
            if self.state == MEETING:
                # the hotkey during a meeting ends it (stop_meeting blocks -> run in a thread)
                threading.Thread(target=self.stop_meeting, daemon=True).start()
                return "meeting stopping"
            if self.state == PROCESSING:
                return "busy"
            if self.state == RECORDING:
                if cmd == "start":
                    return "already recording"
                self.stop_event.set()
                return "stopping"
            # state == IDLE
            if cmd == "stop":
                return "idle"
            self.state = RECORDING
            self.stop_event.clear()
            self.cancel_flag = False
            self.mark_activity()   # promote whisper to GPU now, while you speak (auto mode)
            try:
                threading.Thread(target=self.run_session, daemon=True).start()
            except Exception:  # e.g. "can't start new thread" under resource pressure
                self.state = IDLE   # never leave the daemon wedged in RECORDING
                raise
            return "recording"

    def _shutdown(self) -> None:
        # let the reply flush, stop accepting, then drain any in-flight session (bounded)
        # so we don't kill a half-typed injection mid-keystroke.
        self._shutdown_requested = True
        time.sleep(0.1)
        for _ in range(50):  # up to ~5s
            with self.lock:
                if self.state == IDLE:
                    break
            time.sleep(0.1)
        try:
            if self._srv is not None:
                self._srv.close()
        except OSError:
            pass
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        os._exit(0)

    # -- socket server --------------------------------------------------------
    def serve(self) -> None:
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o600)
        srv.listen(8)
        self._srv = srv
        log(f"listening on {SOCKET_PATH}")
        notify(self.cfg, "wisprflow ready", f"ASR on {self.asr_device}")
        while True:
            try:
                conn, _ = srv.accept()
            except OSError as e:
                if self._shutdown_requested:
                    break
                log(f"accept error (continuing): {e!r}")  # survive transient errors
                continue
            with conn:
                try:
                    data = conn.recv(4096).decode("utf-8", "replace")
                    if data:
                        reply = self.handle(data)
                        conn.sendall(reply.encode())
                except Exception as e:  # noqa: BLE001
                    log(f"conn error: {e!r}")


def main() -> int:
    cfg = load_config()
    log("starting local-wisprflow daemon")
    d = Daemon(cfg)
    d.load_model()
    try:
        d.serve()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
