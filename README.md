# local-wisprflow

A fully-local, private dictation tool — speak, and cleaned/punctuated text is typed into
whatever app is focused. Zero cloud. Mirrors Wispr Flow's two-stage "wait-then-polish"
design entirely on this machine.

```
mic ─▶ record (push-to-talk toggle) ─▶ faster-whisper ASR ─▶ Ollama LLM cleanup ─▶ type into focused app
        wf-toggle hotkey                (large-v3, CPU)        (qwen2.5:14b)         (ydotool)
```

## Why these specific choices on THIS machine

This box shares its **12 GB GPU with a resident 14B research harness (~10 GB VRAM)** and runs
Ollama with `OLLAMA_KV_CACHE_TYPE=q4_0`. Two consequences drove the design — both verified
empirically during the build:

- **ASR is adaptive (`asr_device: "auto"`).** A CPU model stays warm always; a monitor thread
  (polls `nvidia-smi`) runs `large-v3` on the **GPU** (~0.3 s) whenever no big LLM is loaded
  there, and demotes it to **CPU** (releasing its VRAM) the moment a big model (the harness's
  14B, one ~9 GB process) appears. So: harness idle → fast GPU ASR; harness active → whisper
  yields the GPU. Switches never block dictation (the CPU model covers the gap). `wf-toggle
  ping` shows `auto(cuda)` / `auto(cpu)`.
- **Cleanup uses `qwen2.5:3b` on a dedicated, isolated Ollama** (`wf-cleanup-llm.service`, port
  **11435**, own models dir, **f16 KV cache**). The system Ollama's `q4_0` cache garbles small
  models, but this second instance doesn't inherit it — so a fast 3B cleans up correctly in
  **~0.3–0.5 s** (vs 1.3–9.6 s sharing the 14B) using ~2 GB. Dictation no longer touches the
  14B at all; the system Ollama and the harness are left completely alone.

Net result: end-to-end **~0.9 s** after you stop talking, whisper yields the GPU to the harness
on demand, and nothing disturbs the system Ollama service or its config.

## Components

| File | Role |
|---|---|
| `wf_daemon.py` | Resident daemon: keeps whisper warm, listens on a Unix socket, runs record→ASR→cleanup→inject. |
| `wf-run` | Launcher: sets `LD_LIBRARY_PATH` (CUDA wheels) + `YDOTOOL_SOCKET`, execs the daemon under `.venv`. |
| `wf-toggle` | Tiny stdlib client the hotkey runs: `toggle`/`start`/`stop`/`cancel`/`status`/`ping`/`shutdown`. |
| `wf-keylistener.py` | evdev listener: fires `wf-toggle` on a special key GNOME can't bind (here `KEY_PRESENTATION`). |
| `systemd/*.service` | User services (autostart): `wf-daemon`, `wf-cleanup-llm` (isolated 3B Ollama), `wf-keylistener`, `ydotool`. |
| `install-system.sh` | **(sudo)** apt: `libportaudio2 ydotool wl-clipboard` + `/dev/uinput` udev rule + `input` group. |
| `install-services.sh` | Start the user services (no sudo) + pull `qwen2.5:3b` into the isolated cleanup Ollama. |
| `set-hotkey.sh` / `grab-key-gui.py` / `grab-key-evdev.py` | Bind a GNOME shortcut, or capture a special hardware key. |
| `config.example.json` | Copy to `~/.config/wisprflow/config.json` to override defaults. |

## Setup (from scratch)

The Python env is already built (`.venv`, Python 3.12, faster-whisper + CUDA-12 wheels) and
`qwen2.5:14b` / `large-v3` are already downloaded. Remaining steps:

```bash
# 1. system packages + uinput access  (needs: run `sudo -v` in your terminal first)
./install-system.sh
#    -> then LOG OUT and back in (for the 'input' group to apply)

# 2. start the services (after re-login)
./install-services.sh

# 3. bind a hotkey (Super+D is taken by "show desktop" here, so use something free)
./set-hotkey.sh '<Ctrl><Super>space'
```

**Audio:** the daemon captures from the PipeWire default source, which is pinned to the
**laptop built-in mic** (the G522 wireless headset is deliberately not used). Before your
first logout/in, `ydotoold` can't type yet, so results are **copied to the clipboard**
(paste with Ctrl+V); after re-login, they're typed automatically.

## Usage

- Press your hotkey → **recording** (a "🎙 Recording…" notification appears) → speak.
- Press it **again** to stop → it transcribes, cleans up, and types the result into the focused app.
- `./wf-toggle status` shows `idle` / `recording` / `processing`. `./wf-toggle cancel` aborts a recording.

## Meeting mode (dual-channel transcription)

Transcribes a call with **speaker separation**, for meetings you're allowed to record:

1. Press your hotkey → the listening pill appears with a **"👥 Meeting"** button.
2. Click **Meeting** → it starts capturing two streams and writes a live transcript to
   `~/wf-meetings/meeting-<timestamp>.md`:
   ```
   Client: <what the other side said>

   Me: <what you said>
   ```
3. Press your hotkey again to **stop** and finalize the file.

How it works: the **microphone** = "Me" and the **default output sink's monitor** (whatever is
playing — the Zoom/Teams call) = "Client", both captured via `pw-record --target <node>`
(sounddevice hangs on monitor sources here). Each stream is segmented on silence (windowed
energy VAD), transcribed **faithfully** (no LLM rewrite) by the shared WhisperModel behind
`model_lock`, and appended live. Speaker labels come from the source channel — no diarization ML.

> **Use headphones.** With the client's audio in your earbuds (not the speaker), the mic never
> hears them, so the two streams are cleanly separated. On **speakers** the mic re-captures the
> client (bleed); a dedup guard keeps the clean "Client" copy, but headphones are the happy path.
> Set your earbuds as the default output before starting — meeting mode follows the default sink.

Tunables: `meeting_dir`, `meeting_vad_floor` (speech threshold — raise if your speech gets split,
lower if quiet speech is missed), `meeting_silence_ms`, `meeting_beam_size`.

## Tuning (`~/.config/wisprflow/config.json`)

Copy `config.example.json` there and edit. Common knobs:

- **`auto_stop: true`** — stop automatically after `silence_ms` of silence (energy VAD) instead of
  a second keypress. Calibrate `vad_rms_threshold` to your mic (headset ≈ 0.01; noisier ≈ higher).
- **`inject_method`** — `type` (default), `paste` (wl-copy + Ctrl+V; better for non-US layouts or
  long text), or `clipboard` (just copies; you paste).
- **`initial_prompt`** — bias ASR toward names/jargon you dictate often.
- **`llm_enable: false`** — skip cleanup, inject the raw transcript (lowest latency).
- **`asr_model`** — `distil-large-v3` or `medium` for lower latency at some accuracy cost.

## Latency (measured here)

For a spoken utterance, expect roughly **ASR (~0.47× its length) + cleanup (~1–7 s)** after you
stop talking — e.g. a 6 s sentence ≈ 3 s ASR + a couple seconds cleanup. Cleanup is faster when
the 14B is already warm from the harness, slower if it must load (~5–10 s cold) or is mid-inference
for the harness (Ollama serializes requests per model).

## Notes / gotchas

- **Wayland injection + keyboard layout:** `ydotool type` emits US-layout keycodes, so it
  mistypes on non-US layouts (e.g. German QWERTZ: `y`↔`z`, `?`→`_`). This machine is on
  `de+nodeadkeys`, so `inject_method` is set to **`paste`** (wl-copy + a paste chord), which is
  layout-independent and inserts exact Unicode. If you dictate into a **terminal**, Ctrl+V may not
  paste there — set `"paste_chord": "ctrl+shift+v"` (or `"shift+insert"`) in the config.
  Note: paste mode replaces the clipboard with the dictated text.
- **Don't run whisper on the GPU while the harness is active** — it will OOM or evict the 14B.
- The daemon imports `sounddevice` only when recording, so it starts fine even before PortAudio
  is installed (recording just errors until `install-system.sh` has run).
