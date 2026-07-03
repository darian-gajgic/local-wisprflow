# Local Wispr Flow Clone — Research & Build Plan

Target machine: Acer Predator Helios Neo 16 · Ubuntu 26.04 · **RTX 5070 Ti Laptop (12 GB VRAM, Blackwell sm_120)** · GNOME on **Wayland** · Ollama 0.30.10 already installed.

Goal: a fully-local, private voice-dictation tool that does what Wispr Flow does — speak → clean, formatted text typed into whatever app is focused — with **zero cloud**.

---

## 1. How Wispr Flow actually works (reverse-engineered)

Wispr Flow is **not** a classic real-time transcriber. It is a deliberate **two-stage, "wait-then-polish" pipeline**, and *all of it runs in the cloud*:

```
your voice ──▶ [Stage 1: cloud ASR] ──▶ raw transcript ──▶ [Stage 2: fine-tuned Llama LLM] ──▶ polished text ──▶ injected into your app
```

- **Stage 1 — ASR (speech recognition):** a speech model transcribes the audio. Wispr does *not* disclose the exact model; it uses "a combination of open-source models and proprietary LLM providers."
- **Stage 2 — LLM post-processing:** a **fine-tuned Llama** model rewrites the raw transcript — fixes grammar/punctuation, formats it, maintains tone, and adapts to *your personal phrasing over time*. This is where the "magic" lives. Wispr's own engineering blog says it plainly: *"We use large language models to post-process your speech… Flow can wait, understand, and then write what you meant. Not just what you said."*
- **Latency:** their infra provider (Baseten) reports the whole pipeline at **<700 ms p99**, with the Llama stage emitting 100+ tokens in <250 ms — achieved with heavy cloud GPUs + TensorRT, not something to expect verbatim locally.
- **No offline mode:** Flow refuses to record without internet. Even "Privacy Mode" is zero-retention *cloud* processing, not on-device.

**The critical takeaway for your build:** the app you love is *two models cooperating*. Ollama can only serve the **Stage-2 LLM**. You need a **separate local ASR model** for Stage 1 — Ollama does not do speech recognition. Your local stack is therefore:

```
mic ─▶ VAD ─▶ [local ASR model] ─▶ raw text ─▶ [Ollama LLM] ─▶ polished text ─▶ [text injection]
              (faster-whisper /                (qwen2.5)              (ydotool / clipboard)
               Parakeet)
```

Everything in Wispr's base pipeline is replicable with open tooling. The *only* things that are genuinely proprietary are (a) their specific fine-tuned models and (b) the personalization data accumulated from your usage.

---

## 2. Your hardware — what fits in 12 GB

The laptop drives the panel from the Intel iGPU and reserves the RTX 5070 Ti for compute (per system setup), so ~12 GB is available for models. Both models co-reside comfortably:

| Component | Model | VRAM (approx) |
|---|---|---|
| ASR | `faster-whisper large-v3` @ int8 | ~2.5–3 GB |
| ASR (lighter/faster) | `distil-large-v3` or `medium` @ int8 | ~1.5–2 GB |
| ASR (fastest, lowest WER, English) | NVIDIA Parakeet TDT 0.6B v2 | ~1.5–2.5 GB |
| LLM cleanup (primary) | `qwen2.5:7b` (Q4_K_M via Ollama) | ~4.7 GB |
| LLM cleanup (fast fallback) | `qwen2.5:3b` / `llama3.2:3b` | ~2 GB |

Budget: ASR (~3 GB) + `qwen2.5:7b` (~4.7 GB) ≈ **~8 GB** → fits in 12 GB with headroom to spare.

### Blackwell / Python gotchas (read before installing anything)
- ⚠️ **Do NOT use system Python 3.14** for the ML stack — too new; PyTorch/ctranslate2/NeMo wheels aren't published for it yet. Use an isolated **Python 3.12** venv (below).
- **Blackwell (sm_120)** needs a **CUDA 12.8+ runtime**. Your driver (595.71.05) is new enough. Never touch the system driver/CUDA/kernel — install CUDA *runtime* libraries as pip wheels inside the venv only.
- If you go the Parakeet/NeMo route, install **PyTorch cu128**: `pip install torch --index-url https://download.pytorch.org/whl/cu128`.
- ctranslate2 (faster-whisper's engine) must be recent enough to expose sm_120. If GPU init fails, update `ctranslate2`, or temporarily fall back to `device="cpu", compute_type="int8"` to prove the pipeline, then fix the GPU path.

---

## 3. Recommended architecture: push-to-talk, "wait-then-polish" (MVP)

Mirror Wispr's philosophy: **don't stream**. Capture a whole utterance, then transcribe + polish it in one shot. This is dramatically simpler than streaming partial transcripts and is exactly how Wispr conceptually behaves.

**UX:** press a hotkey → it records while you talk → auto-stops after ~800 ms of silence (VAD) → transcribe → LLM cleanup → text is typed into the focused app. One keypress, hands-free stop.

```
[GNOME hotkey] ─▶ wf-toggle ─▶ daemon
                                  │ 1. sounddevice captures mic to buffer
                                  │ 2. Silero VAD detects end-of-speech → finalize
                                  │ 3. faster-whisper.transcribe(audio, vad_filter=True) → raw text
                                  │ 4. POST raw text to Ollama (cleanup prompt) → polished text
                                  └ 5. ydotool type  (fallback: wl-copy + paste)
```

Streaming (live partial transcripts via WhisperLive / LocalAgreement-2) is a **Phase 3** upgrade, not needed for a great MVP.

---

## 4. Component choices (decisions made for you)

| Layer | Pick | Why |
|---|---|---|
| **ASR (MVP)** | **faster-whisper** (`large-v3` int8) | Python-native, batteries-included, built-in Silero VAD (`vad_filter=True`), up to 4× faster than openai/whisper at same accuracy, ~3 GB. Easiest path to working. |
| **ASR (upgrade)** | **NVIDIA Parakeet TDT 0.6B v2** | 6.05% avg WER, extremely fast (English-only), officially supports Blackwell. Heavier deps (NeMo/PyTorch cu128) — adopt once MVP works. |
| **VAD** | **Silero VAD** | <1 ms per 30 ms chunk on CPU, native 16 kHz. Already built into faster-whisper; frees the GPU entirely for ASR+LLM. |
| **LLM cleanup** | **`qwen2.5:7b`** via Ollama (fallback `qwen2.5:3b`) | Strong instruction-following for grammar/format/tone; the closest known open clone ("Murmur") uses exactly this. Try newer `qwen3:4b`/`qwen3:8b` too. |
| **Text injection** | **ydotool** (primary) + **wl-clipboard** paste (fallback) | Only reliable Wayland option — emulates a device via kernel `uinput`, works below the compositor. |
| **Global hotkey** | **GNOME custom shortcut** → toggle script (MVP); `evdev` hold-to-talk (advanced) | Wayland has no global-hotkey API for apps; GNOME's own keybinding is the clean way in. |

### Why injection is "the hard part"
Wayland deliberately has **no global input-injection or global-hotkey API** (security by design). `xdotool` only works on X11 and can't reach native Wayland apps. `ydotool` sidesteps this by creating a virtual input device through the kernel's `uinput` framework, so the compositor sees events as if from a real keyboard. The cost: it needs a persistent **`ydotoold` daemon** with access to `/dev/uinput` (root, or an `input`-group udev rule), and it assumes a **US keyboard layout** (garbles non-US layouts — hence the clipboard-paste fallback). `nerd-dictation` is the best open-source reference for this layer (supports xdotool/ydotool/dotool/wtype).

---

## 5. Step-by-step build plan

### Phase 0 — Environment (isolated, no system risk)
```bash
# 1. uv (fast Python manager) — installs to ~/.local, no system change
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Project venv on Python 3.12 (NOT system 3.14)
cd ~/local-wisprflow
uv venv --python 3.12 .venv
source .venv/bin/activate

# 3. Core Python deps
uv pip install faster-whisper sounddevice numpy silero-vad requests
# GPU runtime libs (CUDA 12 / cuDNN 9) as wheels — never touches system CUDA:
uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

### Phase 1 — LLM layer (Ollama is already installed)
```bash
ollama pull qwen2.5:7b        # ~4.7 GB — primary cleanup model
ollama pull qwen2.5:3b        # ~2 GB — fast fallback
# optional newer: ollama pull qwen3:4b
```
Cleanup prompt (system): keep it strict so it *edits*, never *answers*:
> "You are a dictation post-processor. Rewrite the user's raw speech transcript into clean, well-punctuated text. Fix grammar, capitalization, and filler words ('um', 'uh', 'you know'). Preserve meaning and the speaker's wording. Output ONLY the corrected text — no preamble, no quotes, no commentary."

Call Ollama via `http://localhost:11434/api/generate` (set `"stream": false`, low temperature ~0.2).

### Phase 2 — Text injection (system packages + one udev rule)
> These need sudo. Ask the user to run `sudo -v` first, then:
```bash
sudo apt install -y ydotool wl-clipboard
# Let your user access /dev/uinput without root:
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo usermod -aG input "$USER"          # log out/in afterwards for group to apply
sudo udevadm control --reload-rules && sudo udevadm trigger
# Run the daemon (add to autostart later):
ydotoold &
ydotool type "hello from ydotool"        # verify it types into the focused window
```
Fallback if a target app mangles ydotool typing: `wl-copy "text"` then send the app's paste chord.

### Phase 3 — The daemon (skeleton)
Single-file Python daemon; hotkey toggles recording via a Unix signal or socket. Skeleton:
```python
# wf-daemon.py  (illustrative skeleton — see nerd-dictation & "Murmur" for full patterns)
import sounddevice as sd, numpy as np, requests, subprocess, queue
from faster_whisper import WhisperModel

SR = 16000
asr = WhisperModel("large-v3", device="cuda", compute_type="int8")  # falls back: device="cpu"

def transcribe(audio: np.ndarray) -> str:
    segs, _ = asr.transcribe(audio, language="en", vad_filter=True)
    return " ".join(s.text for s in segs).strip()

def polish(raw: str) -> str:
    r = requests.post("http://localhost:11434/api/generate", json={
        "model": "qwen2.5:7b",
        "system": "You are a dictation post-processor. Rewrite the raw transcript into clean, "
                  "well-punctuated text. Fix grammar and remove fillers. Output ONLY the corrected text.",
        "prompt": raw, "stream": False, "options": {"temperature": 0.2},
    }, timeout=60)
    return r.json()["response"].strip()

def inject(text: str):
    subprocess.run(["ydotool", "type", "--", text])
    # fallback: subprocess.run(["wl-copy", text]); send paste chord

def record_until_silence() -> np.ndarray:
    # capture with sounddevice; stop after ~800ms of silence via Silero VAD (or faster-whisper vad_filter)
    ...

# main loop: on hotkey → audio = record_until_silence(); inject(polish(transcribe(audio)))
```

### Phase 4 — Global hotkey (GNOME Wayland)
MVP: bind a **GNOME custom shortcut** (Settings → Keyboard → Custom Shortcuts) to a `wf-toggle` script (e.g. `Super+Space` or a spare key). The script signals the running daemon to start/stop capture.
Advanced (true hold-to-talk key-down/up): a small `evdev` reader on `/dev/input/...` (needs `input` group, already granted in Phase 2).

### Phase 5 — Polish & upgrades (optional)
- Swap ASR to **Parakeet TDT 0.6B v2** for lower WER/latency (needs `torch` cu128 + NeMo).
- **Live/streaming** transcription with **WhisperLive** (`--backend faster_whisper` or `tensorrt`) or a LocalAgreement-2 scheme.
- **Command mode:** a second hotkey that sends *selected text* + a spoken instruction to the LLM ("make this formal", "bullet points") — Wispr's AI-editing feature.
- **Custom vocabulary:** bias ASR with `initial_prompt` (names/jargon) and/or a post-cleanup find/replace dictionary.
- Autostart `ydotoold` + daemon as user systemd services.

---

## 6. Existing projects — fork vs. borrow

| Project | Stack | Use it for |
|---|---|---|
| **Murmur** (everydayaiwithbrian.com/blog/replace-wispr-flow.html) | faster-whisper + Ollama (`qwen2.5:7b`) | The closest conceptual twin — copy its exact pipeline & cleanup-prompt approach. |
| **nerd-dictation** (github.com/ideasman42/nerd-dictation) | Python, hackable, all 4 injection backends | The reference for the **injection + hotkey** layer on Linux/Wayland. |
| **Handy** (github.com/cjpais/Handy) | Tauri (Rust + React), Whisper + Parakeet, offline | Best **fork target if you want a polished GUI app** instead of a script. |
| whisper-writer / OpenWhispr / WhisperTyping | Python clones | Extra reference implementations for capture→inject loops. |

**Recommendation:** build the ~200-line Python daemon (full control + your Ollama pipeline), borrowing the **injection layer from nerd-dictation** and the **cleanup-prompt design from Murmur**. Consider forking **Handy** only if you decide you want a GUI/tray app.

---

## 7. Realistic expectations & open unknowns

- **Latency (estimate, unverified on this exact GPU):** for a spoken sentence, expect roughly **~1–2.5 s** total after you stop talking (ASR ~0.3–1 s + LLM cleanup ~0.5–1.5 s). Wispr's cloud is faster (~<1 s) because it runs datacenter GPUs + TensorRT — but yours is fully local and private. Use `qwen2.5:3b` or skip the LLM on short utterances to cut latency.
- **No trustworthy public streaming-latency number exists** — the research explicitly *refuted* two commonly-cited streaming-latency figures. **Measure on your own box.**
- **VRAM co-residence** (ASR + 7B LLM in 12 GB) should be fine but confirm empirically with `nvidia-smi` under load; drop ASR to `medium`/`distil` or LLM to 3B if you see eviction.
- **ydotool + non-US layout / paste chords** can be finicky — keep the clipboard-paste fallback ready.

### Open questions to resolve while building
1. Which local LLM best matches Wispr's cleanup quality/latency for *your* speech — `qwen2.5:7b` vs `qwen2.5:3b` vs `qwen3:4b`? (Benchmark on your own dictation.)
2. faster-whisper vs Parakeet real streaming latency **on the 5070 Ti specifically** (Blackwell/cu128).
3. ydotool direct-type vs clipboard-paste reliability across your actual apps (browser, terminal, editor).

---

*Sources: Wispr Flow engineering blog & data-controls page (primary); Baseten case study (infra provider); NVIDIA Parakeet model card; SYSTRAN/faster-whisper; Collabora/WhisperLive; snakers4/silero-vad; Whisper-Streaming (arXiv 2307.14743) & Whispy (arXiv 2405.03484); ReimuNotMoe/ydotool; ideasman42/nerd-dictation. 23 claims adversarially verified, 2 refuted (streaming-latency figures).*
