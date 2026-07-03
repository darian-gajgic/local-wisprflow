#!/usr/bin/env bash
# Phase C — system layer (needs sudo). Installs the injection/audio packages, grants
# /dev/uinput access, and adds you to the 'input' group. Deliberately NARROW and vetted:
# it touches nothing in the kernel/nvidia/cuda/grub/boot stack.
#
# Prereq: open a sudo window first by running  `sudo -v`  in your terminal.
set -euo pipefail

PKGS="libportaudio2 ydotool wl-clipboard"

echo "== [1/4] safety: simulating apt install of: $PKGS =="
sim="$(apt-get install -s $PKGS 2>&1)"
if echo "$sim" | grep -qiE "linux-image|linux-headers|linux-modules|nvidia|cuda|grub|shim|^Remv"; then
  echo "!!! ABORT — simulation would touch a protected package or remove something:"
  echo "$sim" | grep -iE "linux-image|linux-headers|linux-modules|nvidia|cuda|grub|shim|^Remv"
  exit 1
fi
echo "$sim" | grep -E "^Inst" || true
echo "   -> safe (only the 3 packages above)."

echo "== checking sudo window =="
if ! sudo -n true 2>/dev/null; then
  echo "sudo is not open. Run  'sudo -v'  in your terminal, then re-run this script."
  exit 1
fi

echo "== [2/4] apt install =="
sudo -n apt-get install -y $PKGS

echo "== [3/4] udev rule so /dev/uinput is usable without root (group input, 0660) =="
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
  | sudo -n tee /etc/udev/rules.d/99-uinput.rules >/dev/null
sudo -n udevadm control --reload-rules
sudo -n udevadm trigger

echo "== [4/4] add $USER to the 'input' group =="
if id -nG "$USER" | tr ' ' '\n' | grep -qx input; then
  echo "   already in 'input' group."
else
  sudo -n usermod -aG input "$USER"
  echo "   added. NOTE: you must LOG OUT and back in (or reboot) for this to take effect."
fi

echo
echo "System layer done."
echo "Next: after re-login, run  ./install-services.sh  to start ydotoold + the daemon,"
echo "then  ./set-hotkey.sh '<Super>d'  to bind your dictation key."
