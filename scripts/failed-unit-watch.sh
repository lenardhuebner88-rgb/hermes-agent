#!/usr/bin/env bash
# failed-unit-watch.sh — meldet still gescheiterte systemd-User-Units nach Discord.
#
# WARUM: Am 2026-07-28 standen acht User-Units gleichzeitig auf `failed` —
# darunter zwei Backups (pa-backup, health-track-supabase-backup), die seit
# Tagen nichts mehr sicherten. Niemand hatte es bemerkt: ein Timer, der nicht
# feuert, macht kein Geräusch. Die Wächter dieses Systems bewachen jeweils ihr
# Subsystem; über sie selbst wachte niemand.
#
# DESIGN-ENTSCHEID (aus genau diesem Befund gelernt): dieses Skript endet bei
# einem Fund mit **exit 0**. `vault-health.service` macht es andersherum — es
# endet bei einem Fund mit rc=1 und steht dadurch selbst dauerhaft auf `failed`.
# Das ist als Alarm-Kanal brauchbar, macht die Unit aber ununterscheidbar von
# einer echten Störung und würde diesen Wächter zum ersten Eintrag seiner
# eigenen Liste machen. Nonzero endet hier nur, wenn die ZUSTELLUNG scheitert.
#
# Ruhig im Normalfall: gemeldet wird nur, wenn sich die Menge der gescheiterten
# Units ändert (neu / behoben) — oder wenn eine unveränderte, nicht leere Menge
# seit REMIND_DAYS nicht mehr gemeldet wurde. Ohne dieses Nachfassen wäre genau
# der beobachtete Fall wieder still: acht Units, tagelang, unverändert.

set -uo pipefail

CHANNEL="${FAILED_UNIT_WATCH_CHANNEL:-1486480074491559966}"
NOTIFY="${FAILED_UNIT_WATCH_NOTIFY:-/home/piet/.hermes/scripts/discord-notify.py}"
STATE="${FAILED_UNIT_WATCH_STATE:-/home/piet/.hermes/state/failed-unit-watch.json}"
REMIND_DAYS="${FAILED_UNIT_WATCH_REMIND_DAYS:-3}"
DRY_RUN="${FAILED_UNIT_WATCH_DRY_RUN:-0}"

mkdir -p "$(dirname "$STATE")"

# --- Ist-Zustand ------------------------------------------------------------
# `list-units --state=failed` statt `is-failed` je Unit: eine Abfrage, und es
# erfasst auch Units, deren Namen wir nicht vorher kennen.
current="$(systemctl --user list-units --state=failed --plain --no-legend 2>/dev/null \
           | awk '{print $1}' | sort)"

# --- Vorzustand -------------------------------------------------------------
prev=""
prev_reported_at=0
if [ -f "$STATE" ]; then
  prev="$(python3 -c '
import json,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
print("\n".join(sorted(d.get("failed", []))))
' "$STATE" 2>/dev/null)"
  prev_reported_at="$(python3 -c '
import json,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    print(0); sys.exit(0)
print(int(d.get("reported_at", 0) or 0))
' "$STATE" 2>/dev/null || echo 0)"
fi

now="$(date +%s)"
changed=0
[ "$current" != "$prev" ] && changed=1

remind=0
if [ -n "$current" ] && [ "$changed" -eq 0 ]; then
  age=$(( now - prev_reported_at ))
  [ "$age" -ge $(( REMIND_DAYS * 86400 )) ] && remind=1
fi

# Zustand IMMER fortschreiben (auch ohne Meldung), sonst gilt jede stabile
# Menge dauerhaft als "neu" und der Wächter spammt.
write_state() {
  local reported_at="$1"
  python3 -c '
import json,sys
failed=[l for l in sys.stdin.read().split("\n") if l.strip()]
json.dump({"failed": failed, "reported_at": int(sys.argv[1]), "checked_at": int(sys.argv[2])},
          open(sys.argv[3], "w"), indent=2, sort_keys=True)
' "$reported_at" "$now" "$STATE" <<<"$current"
}

if [ "$changed" -eq 0 ] && [ "$remind" -eq 0 ]; then
  write_state "$prev_reported_at"
  echo "failed-unit-watch: unverändert ($(echo -n "$current" | grep -c . ) failed) — keine Meldung"
  exit 0
fi

# --- Meldung bauen ----------------------------------------------------------
n_now=$(echo -n "$current" | grep -c . || true)
n_prev=$(echo -n "$prev" | grep -c . || true)

if [ "$n_now" -eq 0 ]; then
  msg="✅ **systemd-User-Units: wieder alle grün** (vorher $n_prev gescheitert)"
else
  if [ "$remind" -eq 1 ]; then
    head="⏳ **$n_now systemd-User-Unit(s) weiterhin gescheitert** (unverändert seit ≥${REMIND_DAYS}d)"
  else
    head="🔴 **$n_now systemd-User-Unit(s) gescheitert**"
  fi
  msg="$head"

  # Neu hinzugekommene zuerst benennen — das ist die handelbare Information.
  newly="$(comm -13 <(echo "$prev") <(echo "$current") 2>/dev/null)"
  fixed="$(comm -23 <(echo "$prev") <(echo "$current") 2>/dev/null)"
  [ -n "$newly" ] && msg="$msg"$'\n'"**neu:** $(echo "$newly" | tr '\n' ' ')"
  [ -n "$fixed" ] && msg="$msg"$'\n'"**behoben:** $(echo "$fixed" | tr '\n' ' ')"

  msg="$msg"$'\n'
  while IFS= read -r unit; do
    [ -n "$unit" ] || continue
    # Letzte inhaltliche Zeile des Dienstes selbst — systemd-Rahmenzeilen
    # ("Failed to start …") sagen nur, DASS es scheiterte, nie warum.
    why="$(journalctl --user -u "$unit" -n 40 --no-pager 2>/dev/null \
           | grep -vE "systemd\[[0-9]+\]:" | tail -1 | cut -c1-160)"
    [ -n "$why" ] || why="(keine eigene Ausgabe im Journal — Dienst scheitert stumm)"
    msg="$msg"$'\n'"• \`$unit\`"$'\n'"  $why"
  done <<<"$current"

  msg="$msg"$'\n\n'"Details: \`journalctl --user -u <unit> -n 50\` · Zurücksetzen: \`systemctl --user reset-failed <unit>\`"
fi

# --- Zustellen --------------------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  printf '%s\n' "$msg"
  echo "--- DRY-RUN: nicht zugestellt, Zustand nicht fortgeschrieben ---"
  exit 0
fi

if [ ! -f "$NOTIFY" ]; then
  echo "failed-unit-watch: Notifier fehlt: $NOTIFY" >&2
  exit 1
fi

if ! printf '%s' "$msg" | python3 "$NOTIFY" --channel "$CHANNEL" --stdin; then
  # Zustellfehler ist der EINZIGE Grund für nonzero: dann ist der Alarm-Kanal
  # selbst kaputt, und dafür ist systemds eigenes Fehlerbild das richtige Mittel.
  echo "failed-unit-watch: Discord-Zustellung fehlgeschlagen" >&2
  exit 1
fi

write_state "$now"
echo "failed-unit-watch: gemeldet ($n_now failed, vorher $n_prev)"
exit 0
