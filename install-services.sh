#!/usr/bin/env bash
# User-level setup (NO sudo). Starts the dictation daemon as a systemd *user* service.
# Text injection uses the ydotool PACKAGE's own user service (ydotool.service), which runs
# ydotoold on the default socket ($XDG_RUNTIME_DIR/.ydotool_socket) that wf-run points at.
#
# Run AFTER install-system.sh. ydotoold can only open /dev/uinput once the 'input' group is
# active in your login session, so text-typing works after you've logged out/in once.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$HOME/.config/systemd/user" "$HOME/.ollama-wf/models"
cp "$HERE/systemd/wf-cleanup-llm.service" "$HOME/.config/systemd/user/"
cp "$HERE/systemd/wf-daemon.service" "$HOME/.config/systemd/user/"
cp "$HERE/systemd/wf-keylistener.service" "$HOME/.config/systemd/user/"

systemctl --user daemon-reload
# ydotool.service is shipped + auto-enabled by the apt package; make sure it's enabled.
systemctl --user enable ydotool.service >/dev/null 2>&1 || true
# Isolated cleanup LLM (own port 11435, f16 KV cache) + its small model — started BEFORE
# the daemon that uses it. This is why a small model doesn't garble here: the system Ollama's
# q4_0 cache is NOT inherited by this instance.
systemctl --user enable --now wf-cleanup-llm.service
if ! OLLAMA_HOST=127.0.0.1:11435 ollama list 2>/dev/null | grep -q "gemma3:4b"; then
  echo "pulling gemma3:4b into the isolated cleanup Ollama (~3.3GB, one-time)..."
  OLLAMA_HOST=127.0.0.1:11435 ollama pull gemma3:4b || echo "  (pull failed; run it manually later)"
fi
systemctl --user enable --now wf-daemon.service
# key listener: triggers dictation from KEY_PRESENTATION (a special key GNOME can't bind).
# Harmless if you use the GNOME shortcut instead; edit WF_KEYCODE in the unit to change the key.
systemctl --user enable --now wf-keylistener.service

echo
echo "wf-daemon service:"
systemctl --user --no-pager status wf-daemon.service | head -6 || true

if ! id -nG | tr ' ' '\n' | grep -qx input; then
  echo
  echo "NOTE: the 'input' group is not active in this session yet, so ydotoold (text typing)"
  echo "      won't work until you LOG OUT and back in once. Recording + transcription work now;"
  echo "      to try before re-login, set \"inject_method\": \"clipboard\" in ~/.config/wisprflow/config.json."
fi
echo
echo "Test:  ./wf-toggle ping      (expect: pong (cpu))"
echo "Logs:  journalctl --user -u wf-daemon -f"
