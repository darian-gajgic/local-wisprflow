# local-wisprflow

Push-to-talk dictation for Linux that runs entirely on your own machine. Press a hotkey, speak, press it again, and cleaned-up, punctuated text is typed into whichever app has focus. Speech recognition (faster-whisper) and text cleanup (a small LLM on Ollama) both run locally, so no audio or text leaves the computer.

```
mic -> record (hotkey) -> faster-whisper large-v3 -> Ollama gemma3:4b cleanup -> typed into the focused app (ydotool)
```

## Why

It follows the two-stage design of Wispr Flow: transcribe first, then let an LLM fix punctuation and remove filler words. Here both stages are local. It is also built to share one consumer GPU with other local models without fighting them for memory.

## Features

- **Three output modes**, cycled from the on-screen pill or with `./wf-toggle mode`:
  - *Clean* (default): the LLM turns the transcript into punctuated sentences and drops fillers.
  - *Notes*: the same cleanup, one sentence per line. It types real Enter keys, so avoid it in terminals and chat boxes.
  - *Raw*: exactly what Whisper heard, no LLM, punctuation stripped. The fastest mode.
- **English, German and Romanian**, switchable per session from the pill.
- **Layout-aware typing.** `wf_layout.py` reads the active XKB layout through libxkbcommon and sends the matching key codes. Text lands correctly on non-US layouts such as German QWERTZ, in terminals and GUI apps alike, without touching the clipboard.
- **Adaptive GPU use** (`"asr_device": "auto"`). A CPU copy of Whisper is always warm. Whisper moves to the GPU while you dictate and back to the CPU when another process loads a large model, or after 30 minutes without dictation so the GPU can power down. Switching never blocks a dictation.
- **Isolated cleanup model.** A second Ollama instance on port 11435, with its own model directory and an f16 KV cache, so it never touches a system Ollama or its settings. A deterministic sanitizer strips any preamble the model adds. Design notes: [docs/cleanup.md](docs/cleanup.md).
- **Meeting mode.** Captures the microphone ("Me") and the system audio output ("Client") as two channels and writes a live, speaker-labelled Markdown transcript to `~/wf-meetings/`. Use headphones so the microphone does not pick up the other side, and only record calls you are allowed to record.

## Requirements

- Linux with GNOME on Wayland and PipeWire. The hotkey is bound through `gsettings`; typing uses `ydotool` and `/dev/uinput`.
- `libportaudio2`, `ydotool`, `wl-clipboard` (installed by `install-system.sh` with apt), and `ffmpeg` for meeting mode.
- Python 3.12 and [uv](https://github.com/astral-sh/uv).
- [Ollama](https://ollama.com), installed at `/usr/local/bin/ollama` (the path the cleanup service uses).
- Optional: an NVIDIA GPU. The CUDA 12 libraries come from pip wheels, so no system CUDA toolkit is needed. Without a GPU, set `"asr_device": "cpu"`.

## Install

```bash
git clone https://github.com/darian-gajgic/local-wisprflow.git
cd local-wisprflow

# 1. Python environment with the pinned versions
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-lock.txt

# 2. Download the Whisper model once (the daemon runs with HF_HUB_OFFLINE=1)
.venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3', device='cpu', compute_type='int8')"

# 3. System packages and /dev/uinput access, then log out and back in
sudo -v && ./install-system.sh

# 4. Config, user services (cleanup LLM, daemon, key listener) and the one-time gemma3:4b pull
mkdir -p ~/.config/wisprflow && cp config.example.json ~/.config/wisprflow/config.json
./install-services.sh

# 5. Bind a hotkey
./set-hotkey.sh '<Ctrl><Super>space'
```

The unit files in `systemd/` and the two `.desktop` files contain the absolute path of the original install. Point their `ExecStart` and `Exec` lines at your clone before step 4. Until you log out and back in after step 3, `ydotool` cannot type; set `"inject_method": "clipboard"` to try it before that.

## Usage

- Press the hotkey: a "Listening" pill appears. Speak, then press the hotkey again. The text is typed into the focused window.
- `./wf-toggle status` shows `idle`, `recording` or `processing`. `./wf-toggle cancel` discards a recording.
- `./wf-start` brings the whole stack up and health-checks it. `./wf-stop` shuts it down.
- Logs: `journalctl --user -u wf-daemon -n 50`.

## Configuration

Settings live in `~/.config/wisprflow/config.json`. The most useful ones:

| Key | Effect |
|---|---|
| `auto_stop`, `vad_rms_threshold`, `silence_ms` | Stop on silence instead of a second key press. Calibrate the threshold to your microphone. |
| `inject_method` | `type` (default, layout-aware), `paste`, or `clipboard`. |
| `initial_prompt` | Bias recognition towards names and jargon you use often. |
| `llm_enable` | `false` skips cleanup and types the raw transcript. |
| `asr_model` | `distil-large-v3` or `medium` for lower latency at some cost in accuracy. |
| `meeting_vad_floor`, `meeting_silence_ms` | Tune how meeting mode splits speech. |

## Components

| File | Role |
|---|---|
| `wf_daemon.py` | Resident daemon: keeps Whisper warm and runs record, transcribe, clean up, type. |
| `wf-toggle` | Small client the hotkey calls (`toggle`, `status`, `cancel`, `mode`, `ping`). |
| `wf-overlay.py` | The on-screen pill with mode, language and meeting buttons. |
| `wf_layout.py` | Character to key-code mapping for the active keyboard layout. |
| `wf_meeting.py` | Two-channel meeting transcription. |
| `wf-keylistener.py` | Optional evdev listener for a hardware key GNOME cannot bind. |
| `systemd/` | User services for the daemon, the cleanup Ollama and the key listener. |

## Tested on

One laptop: NVIDIA RTX 5070 Ti Laptop GPU (12 GB), a 24-thread CPU, GNOME on Wayland, German keyboard layout, built-in microphone. During development the GPU also held a 14B model on the system Ollama, using about 10 GB of VRAM; the adaptive ASR and the separate cleanup instance were designed around that.

Measured there: with Whisper on the GPU, text appears about 0.9 s after you stop speaking. On the CPU, large-v3 int8 transcribes at about 0.47 times the length of the recording, and cleanup adds 0.8 to 1.3 s.
