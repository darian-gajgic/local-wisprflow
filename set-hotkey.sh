#!/usr/bin/env bash
# Bind a GNOME custom keyboard shortcut to `wf-toggle` (start/stop dictation).
# Usage:  ./set-hotkey.sh '<Super>d'          (default is <Super>d)
# Change later in Settings -> Keyboard -> View and Customize Shortcuts -> Custom Shortcuts,
# or re-run this with a different binding. Remove with: ./set-hotkey.sh --remove
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BASE="org.gnome.settings-daemon.plugins.media-keys"
PATHID="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/wisprflow/"
SCHEMA="${BASE}.custom-keybinding:${PATHID}"

if [[ "${1:-}" == "--remove" ]]; then
  cur="$(gsettings get "$BASE" custom-keybindings)"
  new="$(printf '%s' "$cur" | sed "s|'${PATHID}'\(, \)\?||; s|, ]|]|")"
  gsettings set "$BASE" custom-keybindings "$new"
  echo "Removed wisprflow shortcut. List is now: $(gsettings get "$BASE" custom-keybindings)"
  exit 0
fi

BINDING="${1:-<Super>d}"
NAME="wisprflow dictate"
CMD="${HERE}/wf-toggle"

cur="$(gsettings get "$BASE" custom-keybindings)"
if [[ "$cur" != *"$PATHID"* ]]; then
  if [[ "$cur" == "@as []" || "$cur" == "[]" ]]; then
    new="['${PATHID}']"
  else
    new="${cur%]}, '${PATHID}']"
  fi
  gsettings set "$BASE" custom-keybindings "$new"
fi

gsettings set "$SCHEMA" name "$NAME"
gsettings set "$SCHEMA" command "$CMD"
gsettings set "$SCHEMA" binding "$BINDING"

echo "Bound  $BINDING  ->  $CMD"
echo "  name    = $(gsettings get "$SCHEMA" name)"
echo "  command = $(gsettings get "$SCHEMA" command)"
echo "  binding = $(gsettings get "$SCHEMA" binding)"
echo
echo "Press $BINDING to start dictating; press again to stop (then it types the cleaned text)."
