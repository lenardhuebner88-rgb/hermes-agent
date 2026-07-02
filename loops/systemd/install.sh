#!/usr/bin/env bash
# Installiert die Loop-Runner-Template-Units ins user-systemd (idempotent).
# Aktiviert NICHTS automatisch — Timer schaltet der Operator (oder das
# Dashboard) pro Pack bewusst ein:
#   systemctl --user enable --now hermes-loop@<pack>.timer   # nächtlich
#   systemctl --user start hermes-loop@<pack>.service        # einmal jetzt
set -euo pipefail

SRC="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
DST="$HOME/.config/systemd/user"
mkdir -p "$DST"
install -m 0644 "$SRC/hermes-loop@.service" "$SRC/hermes-loop@.timer" "$DST/"
systemctl --user daemon-reload
echo "Installiert: $DST/hermes-loop@.service + .timer"
systemctl --user list-timers 'hermes-loop@*' --no-pager || true
