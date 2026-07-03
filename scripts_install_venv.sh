#!/usr/bin/env bash
# Clean, torch-free environment for the local-wisprflow daemon.
# faster-whisper uses ctranslate2 (NOT torch); VAD is energy-based + faster-whisper's
# own bundled onnxruntime vad_filter, so we deliberately DO NOT install silero-vad
# (which drags in torch + conflicting CUDA-13 wheels).
set -euo pipefail
cd /home/sinep/local-wisprflow
export PATH="$HOME/.local/bin:$PATH"

echo "[$(date +%T)] removing any old venv..."
rm -rf .venv

echo "[$(date +%T)] creating clean venv (CPython 3.12 via uv)..."
uv venv --python 3.12 .venv

echo "[$(date +%T)] installing deps (faster-whisper + CUDA-12 runtime wheels)..."
uv pip install --python .venv/bin/python \
  faster-whisper sounddevice numpy requests \
  nvidia-cublas-cu12 nvidia-cudnn-cu12

echo "[$(date +%T)] verifying imports + ctranslate2 CUDA visibility..."
.venv/bin/python - <<'PY'
import faster_whisper, sounddevice, numpy, requests, ctranslate2
print("imports ok | ctranslate2", ctranslate2.__version__, "| numpy", numpy.__version__)
try:
    print("ctranslate2 CUDA device count:", ctranslate2.get_cuda_device_count())
except Exception as e:
    print("ctranslate2 CUDA probe error:", repr(e))
PY

echo "[$(date +%T)] DONE"
