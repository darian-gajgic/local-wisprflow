#!/usr/bin/env python3
"""Meeting-mode dual-channel transcription for local-wisprflow.

Captures TWO streams and labels them by source (no ML diarization needed):
  * the microphone            -> "Me"
  * the default sink's MONITOR -> "Client"   (= whatever is playing, e.g. the Zoom/Teams call)

Both are captured via `pw-record --target <node.name>` — on this box sounddevice HANGS on
monitor sources, but pw-record handles the mic and the monitor reliably, even both at once
(verified). Each stream is segmented on silence with a lightweight energy VAD, transcribed by
the daemon's shared WhisperModel behind `daemon.model_lock` (CTranslate2 is NOT safe for
concurrent transcribe() calls), and written to a live, speaker-labeled transcript file:

    Client: ...

    Me: ...

Headphones give clean separation (the client's audio goes to the earbuds, never into the mic),
so no echo-cancellation is needed. Transcript is faithful (no LLM rewrite).
"""
from __future__ import annotations

import datetime
import difflib
import json
import os
import subprocess
import threading
import time
from queue import Empty, Queue

import numpy as np

SR = 16000
BLOCK = 512  # 32 ms frames


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def resolve_default_nodes():
    """(sink_name, source_name) for the current PipeWire defaults; the sink's monitor = Client.
    Resolved by node.name at runtime because PipeWire node IDs move across reboots/BT connect."""
    try:
        data = json.loads(subprocess.check_output(["pw-dump"], text=True))
    except Exception:  # noqa: BLE001
        return None, None
    sink = source = None
    for o in data:
        if (o.get("type") == "PipeWire:Interface:Metadata"
                and o.get("props", {}).get("metadata.name") == "default"):
            for m in o.get("metadata", []):
                if m.get("key") == "default.audio.sink":
                    sink = (m.get("value") or {}).get("name")
                elif m.get("key") == "default.audio.source":
                    source = (m.get("value") or {}).get("name")
    return sink, source


class MeetingSession:
    """One meeting: two capture+VAD threads feed a single transcribe/writer worker."""

    def __init__(self, daemon, log):
        self.d = daemon
        self.cfg = daemon.cfg
        self.log = log
        self.stop_event = threading.Event()
        self.segq: "Queue" = Queue()
        self.procs = []            # pw-record subprocesses
        self.threads = []
        self.turns = []            # [(speaker, text)] with consecutive same-speaker merged
        self.path = None
        self.header = ""

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> bool:
        sink, source = resolve_default_nodes()
        if not sink or not source:
            self.log("meeting: could not resolve audio nodes via pw-dump")
            return False
        d = os.path.expanduser(self.cfg.get("meeting_dir", "~/wf-meetings"))
        os.makedirs(d, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.path = os.path.join(d, f"meeting-{ts}.md")
        self.header = f"# Meeting transcript — {datetime.datetime.now():%Y-%m-%d %H:%M}\n\n"
        self.turns = []
        self._flush()
        client = sink + ".monitor"   # pulse-layer monitor name — works for ALSA AND Bluetooth
        self.log(f"meeting: -> {self.path}")
        self.log(f"meeting: Me={source}")
        self.log(f"meeting: Client={client}")
        self.threads = [
            threading.Thread(target=self._worker, daemon=True),
            threading.Thread(target=self._channel, args=(source, "Me"), daemon=True),
            threading.Thread(target=self._channel, args=(client, "Client"), daemon=True),
        ]
        for t in self.threads:
            t.start()
        return True

    def stop(self) -> str:
        self.stop_event.set()
        for p in self.procs:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
        # let the worker drain any queued segments (bounded)
        for _ in range(60):
            if self.segq.empty():
                break
            time.sleep(0.05)
        self.log(f"meeting: saved {self.path} ({len(self.turns)} turns)")
        return self.path

    # -- capture + energy VAD (per channel) -----------------------------------
    def _channel(self, target, label):
        cfg = self.cfg
        floor = float(cfg.get("meeting_vad_floor", 0.02))
        sil_need = float(cfg.get("meeting_silence_ms", 700)) / 1000.0
        min_speech = float(cfg.get("meeting_min_speech_ms", 300)) / 1000.0
        maxseg = float(cfg.get("meeting_max_seg_s", 24))
        # Capture via ffmpeg's PulseAudio input (pipewire-pulse). This uses the `.monitor`
        # source name, which works for BOTH ALSA and Bluetooth sinks — unlike `pw-record
        # --target <sink>`, which silently falls back to the default mic for Bluetooth
        # monitors (that bug made both channels record the mic).
        try:
            proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-f", "pulse", "-i", target, "-ar", str(SR), "-ac", "1", "-f", "s16le", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001
            self.log(f"meeting: ffmpeg capture failed for {label}: {e!r}")
            return
        self.procs.append(proc)
        nbytes = BLOCK * 2
        from collections import deque
        win = deque(maxlen=5)      # ~160 ms rolling window -> smooths transient noise spikes
        preroll = deque(maxlen=6)  # ~190 ms pre-roll so speech onsets aren't clipped
        buf, speaking, silence, seg_start, speech_dur = [], False, 0.0, 0.0, 0.0
        while not self.stop_event.is_set():
            data = proc.stdout.read(nbytes)
            if not data or len(data) < nbytes:
                break
            frame = np.frombuffer(data, np.int16).astype(np.float32) / 32768.0
            win.append(float(np.sqrt(np.mean(frame ** 2))))
            loud = (sum(win) / len(win)) >= floor      # windowed level, not a single frame
            if not speaking:
                preroll.append(frame)
                if loud:
                    speaking, seg_start = True, time.time()
                    buf, speech_dur, silence = list(preroll), 0.0, 0.0
            else:
                buf.append(frame)                       # keep trailing silence for a clean cut
                if loud:
                    silence = 0.0
                    speech_dur += BLOCK / SR
                else:
                    silence += BLOCK / SR
                if (silence >= sil_need and speech_dur >= min_speech) \
                        or (time.time() - seg_start) >= maxseg:
                    seg, spoke = np.concatenate(buf), speech_dur
                    buf, speaking, silence = [], False, 0.0
                    preroll.clear()
                    if spoke >= min_speech:
                        self.segq.put((seg_start, label, seg))
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass

    # -- transcribe (serialized) + write --------------------------------------
    def _worker(self):
        cfg = self.cfg
        while not (self.stop_event.is_set() and self.segq.empty()):
            try:
                seg_start, label, audio = self.segq.get(timeout=0.3)
            except Empty:
                continue
            if audio.size < int(0.2 * SR):
                continue
            self.d.mark_activity()   # keep whisper on the GPU during the meeting (auto mode)
            t0 = time.time()
            try:
                with self.d.model_lock:
                    segments, _ = self.d.asr.transcribe(
                        audio, language=cfg["language"] or None,
                        beam_size=int(cfg.get("meeting_beam_size", 3)),
                        vad_filter=False, condition_on_previous_text=False)
                    text = " ".join(s.text.strip() for s in segments).strip()
            except Exception as e:  # noqa: BLE001
                self.log(f"meeting: transcribe error: {e!r}")
                continue
            self.log(f"meeting: [{label}] {audio.size / SR:.1f}s -> {time.time() - t0:.2f}s "
                     f"-> {text[:60]!r}")
            if text:
                self._append(label, text)

    def _append(self, label, text):
        speaker = "Me" if label == "Me" else "Client"
        # Speaker-bleed dedup (matters only WITHOUT headphones): the mic re-captures the client's
        # speaker audio, so the same utterance appears back-to-back from BOTH channels. Keep the
        # "Client" (clean monitor) copy and drop the mic duplicate — works in either arrival order.
        if len(text) >= 5 and self.turns and self.turns[-1][0] != speaker \
                and _similar(text, self.turns[-1][1]) >= 0.82:
            if speaker == "Client":
                self.turns[-1] = ("Client", text)      # replace the bleed "Me" with clean Client
                if len(self.turns) >= 2 and self.turns[-2][0] == "Client":  # re-merge if it split
                    self.turns[-2] = ("Client", self.turns[-2][1] + " " + text)
                    self.turns.pop()
                self.log(f"meeting: bleed pair -> kept Client -> {text[:40]!r}")
            else:
                self.log(f"meeting: dropped mic-bleed duplicate -> {text[:40]!r}")
            self._flush()
            return
        if self.turns and self.turns[-1][0] == speaker:
            self.turns[-1] = (speaker, self.turns[-1][1] + " " + text)
        else:
            self.turns.append((speaker, text))
        self._flush()

    def _flush(self):
        body = self.header + "\n\n".join(f"{s}: {t}" for s, t in self.turns)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(body.rstrip() + "\n")
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            self.log(f"meeting: write failed: {e!r}")
