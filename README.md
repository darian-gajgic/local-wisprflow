# local-wisprflow

A fully-local, private dictation tool — speak, and cleaned/punctuated text is typed into
whatever app is focused. Zero cloud. Mirrors Wispr Flow's two-stage "wait-then-polish"
design entirely on this machine.

```
mic ─▶ record (push-to-talk toggle) ─▶ faster-whisper ASR ─▶ Ollama LLM cleanup ─▶ type into focused app
        wf-toggle hotkey                (large-v3, CPU)         (gemma3:4b)          (ydotool)
```

## Why these specific choices on THIS machine

This box shares its **12 GB GPU with a resident 14B research harness (~10 GB VRAM)** and runs
Ollama with `OLLAMA_KV_CACHE_TYPE=q4_0`. Two consequences drove the design — both verified
empirically during the build:

- **ASR is adaptive + power-aware (`asr_device: "auto"`).** A CPU model stays warm always. On
  **dictation activity**, whisper is promoted to the **GPU** (`large-v3`, ~0.3 s) — the load runs
  *while you speak*, so it's ready by the time you stop. It's demoted back to **CPU** (freeing its
  VRAM) when (a) the harness's 14B appears on the GPU, or (b) there's been **no dictation for
  `gpu_idle_timeout_s` (default 5 min)** — so the dGPU can auto-suspend to **D3cold (0 W)** and save
  battery. Switches never block dictation (the warm CPU model covers the gap). To avoid *waking* a
  sleeping dGPU, the monitor detects the harness via Ollama `/api/ps` (HTTP) while idle and only
  runs `nvidia-smi` while whisper is already on the GPU. `wf-toggle ping` shows `auto(cuda)`/`auto(cpu)`.
- **Cleanup uses `gemma3:4b` on a dedicated, isolated Ollama** (`wf-cleanup-llm.service`, port
  **11435**, own models dir, **f16 KV cache**). The system Ollama's `q4_0` cache garbles small
  models, but this second instance doesn't inherit it. gemma3:4b (temperature 0) follows the
  "clean up, don't rewrite" instruction far more faithfully than a 3B — which summarized long
  dictations, inserted paragraph breaks, and leaked "Sure, here is the corrected text:". Cleanup
  runs in **~0.8–1.3 s** using ~3.3 GB, and a deterministic sanitizer in `polish()` strips any
  stray preamble/newlines as a backstop. Dictation never touches the system Ollama or its 14B.
  See **[docs/cleanup.md](docs/cleanup.md)** for the full cleanup-pipeline design, the two
  failure modes it fixes, and the pattern-completion framing that keeps it transcribing.

Net result: end-to-end **~0.9 s** after you stop talking, whisper yields the GPU to the harness
on demand, and nothing disturbs the system Ollama service or its config.

## Components

| File | Role |
|---|---|
| `wf_daemon.py` | Resident daemon: keeps whisper warm, listens on a Unix socket, runs record→ASR→cleanup→inject. |
| `wf-run` | Launcher: sets `LD_LIBRARY_PATH` (CUDA wheels) + `YDOTOOL_SOCKET`, execs the daemon under `.venv`. |
| `wf-toggle` | Tiny stdlib client the hotkey runs: `toggle`/`start`/`stop`/`cancel`/`status`/`ping`/`shutdown`. |
| `wf-keylistener.py` | evdev listener: fires `wf-toggle` on a special key GNOME can't bind (here `KEY_PRESENTATION`). |
| `wf_meeting.py` | Meeting mode: dual-channel (mic + system-audio monitor) speaker-labeled transcription. |
| `wf_layout.py` | Layout-aware typing: maps chars → correct keycodes for the active XKB layout (libxkbcommon). |
| `systemd/*.service` | User services (autostart): `wf-daemon`, `wf-cleanup-llm` (isolated cleanup Ollama, `gemma3:4b`), `wf-keylistener`, `ydotool`. |
| `install-system.sh` | **(sudo)** apt: `libportaudio2 ydotool wl-clipboard` + `/dev/uinput` udev rule + `input` group. |
| `install-services.sh` | Start the user services (no sudo) + pull `gemma3:4b` into the isolated cleanup Ollama. |
| `set-hotkey.sh` / `grab-key-gui.py` / `grab-key-evdev.py` | Bind a GNOME shortcut, or capture a special hardware key. |
| `config.example.json` | Copy to `~/.config/wisprflow/config.json` to override defaults. |

## Setup (from scratch)

The Python env is already built (`.venv`, Python 3.12, faster-whisper + CUDA-12 wheels) and
`gemma3:4b` (cleanup) / `large-v3` (ASR) are already downloaded. Remaining steps:

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

- Press your hotkey → **recording** (an animated "Listening" pill appears) → speak.
- Press it **again** to stop → it transcribes, cleans up, and types the result into the focused app.
- `./wf-toggle status` shows `idle` / `recording` / `processing`. `./wf-toggle cancel` aborts a recording.

The **Listening pill** (bottom-center of the primary monitor) has two mode buttons: **MeetingMode**
and **NoteMode** (below). The pill is DPI-scaled and pinned to the primary monitor (it no longer
mis-sizes or straddles the seam on a multi-monitor desktop).

> **If your key press is sometimes "not registered":** on some laptops the trigger key
> (`KEY_PRESENTATION` here) is reported by *several* input devices at once, so one physical press
> emitted two key-downs → two toggles that cancelled out. `wf-keylistener` now **debounces**
> (default 300 ms, `WF_DEBOUNCE_MS`) so duplicate emissions collapse into a single toggle.

## NoteMode (one sentence per line)

For longer notes, a single paragraph is hard to read. Turn on **NoteMode** and each dictation is
written **one sentence per line** instead:

1. Press your hotkey → the Listening pill appears → click **📝 NoteMode** (it lights up blue, "•ON").
   Or run `./wf-toggle note` (toggles; prints `note on`/`note off`).
2. Speak and stop as usual — the inserted text is broken at sentence boundaries, one per line, and
   ends on a fresh line so the next note starts cleanly.
3. NoteMode is a **persistent toggle** — it stays on for every dictation until you turn it off (or
   set `"note_mode": true` in your config to default it on).

Sentence splitting is **deterministic** (done in `format_notes()`, no LLM), so it works even when the
cleanup LLM is unavailable — it relies on the punctuation Whisper already produces. Abbreviations
(`Dr.`, `e.g.`, `z.B.`), initials, decimals, and standalone list markers (`1.`) don't trigger a line
break, while a clause that merely ends in a number (`I scored 8.`) still splits.

> **NoteMode types real Enter keys** (one per sentence line, `inject_method: "type"`). That's perfect
> in a text editor / notes app, but in a **terminal or chat box** each newline submits the line — so
> use NoteMode where newlines mean "new line", not "send". The pill shows **"NoteMode •ON"** while it's
> active so you can tell at a glance.

## Meeting mode (dual-channel transcription)

Transcribes a call with **speaker separation**, for meetings you're allowed to record:

1. Press your hotkey → the listening pill appears with a **"👥 MeetingMode"** button.
2. Click **MeetingMode** → it starts capturing two streams and writes a live transcript to
   `~/wf-meetings/meeting-<timestamp>.md`:
   ```
   Client: <what the other side said>

   Me: <what you said>
   ```
3. Press your hotkey again to **stop** and finalize the file.

How it works: the **microphone** = "Me" and the **default output sink's `.monitor`** (whatever is
playing — the Zoom/Teams call) = "Client", both captured via **`ffmpeg -f pulse`**. (This matters:
`sounddevice` hangs on monitor sources here, and `pw-record --target <sink>` silently falls back
to the mic for **Bluetooth** sinks — so both channels would record your voice. `ffmpeg`'s pulse
`.monitor` input works for ALSA *and* Bluetooth.) Each stream is segmented on silence (windowed
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
- **`inject_method`** — `type` (default; **layout-aware** — see gotchas), `paste` (wl-copy + a
  paste chord; needs the right chord per app), or `clipboard` (just copies; you paste).
- **`initial_prompt`** — bias ASR toward names/jargon you dictate often.
- **`llm_enable: false`** — skip cleanup, inject the raw transcript (lowest latency).
- **`asr_model`** — `distil-large-v3` or `medium` for lower latency at some accuracy cost.

## Latency (measured here)

For a spoken utterance, expect roughly **ASR (~0.47× its length) + cleanup (~0.8–1.3 s)** after
you stop talking — e.g. a 6 s sentence ≈ 3 s ASR + ~1 s cleanup. Cleanup runs on the isolated
`gemma3:4b` (`:11435`) and is independent of the harness; it's slower only on the first call
after the model idles out of memory (~2–4 s cold load).

## Notes / gotchas

- **Injection is layout-aware typing (`inject_method: "type"`).** Plain `ydotool type` assumes a
  US layout and mistypes on non-US ones (German QWERTZ: `y`↔`z`, `?`→`_`). Instead, `wf_layout.py`
  reads the current XKB layout (here `de+nodeadkeys`) via **libxkbcommon (ctypes)** and maps each
  character to the correct **evdev keycode**, which ydotool emits — so text lands correctly in
  **every** app (terminal *and* GUI) with **no paste chord** and no clipboard use. This avoids the
  paste-chord problem (GUIs want Ctrl+V, terminals like Hermes want Ctrl+Shift+V) since focused-app
  detection is blocked on GNOME Wayland. Trade-offs: typing is slightly slower than paste, and
  characters not on the keyboard (em-dash, curly quotes) are normalized to plain equivalents.
  The `paste`/`clipboard` methods remain available for special cases.
- **Don't run whisper on the GPU while the harness is active** — it will OOM or evict the 14B.
- The daemon imports `sounddevice` only when recording, so it starts fine even before PortAudio
  is installed (recording just errors until `install-system.sh` has run).
