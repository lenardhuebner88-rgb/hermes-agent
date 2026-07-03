#!/usr/bin/env bash
# merge-audit.sh <merge-commit> [--strict]
#
# Vergleicht das tatsaechliche Ergebnis eines Merge-Commits mit dem
# Clean-Automerge seiner beiden Parents (git merge-tree --write-tree).
# Jede Datei, in der das Merge-Ergebnis vom Automerge abweicht, war eine
# MANUELLE Entscheidung waehrend der Konfliktaufloesung — genau dort haben
# die Vorfaelle v0.17→v0.18 (413638a28) still Arbeit verworfen (beide
# Richtungen: Fork-Haertungen UND Upstream-Features).
#
# Kontrakt: Nach jedem Upstream-Merge laufen lassen; jede gelistete Datei
# braucht eine Begruendungszeile im Merge-Receipt, sonst kein Push.
# --strict: Exit 1, wenn Abweichungen ausserhalb der Ignorierliste existieren
# (fuer Hook-/CI-Nutzung).
set -euo pipefail

merge=${1:?usage: merge-audit.sh <merge-commit> [--strict]}
strict=${2:-}

if ! git rev-parse -q --verify "$merge^2" >/dev/null; then
  echo "FEHLER: $merge ist kein Merge-Commit (kein zweiter Parent)." >&2
  exit 2
fi
p1=$(git rev-parse "$merge^1")
p2=$(git rev-parse "$merge^2")

# --write-tree exitet 1 bei Konflikten, druckt aber trotzdem in Zeile 1 die
# Tree-OID des Automerge (Konflikt-Dateien enthalten dann Marker-Stufen).
set +e
mt_out=$(git merge-tree --write-tree --name-only "$p1" "$p2")
mt_status=$?
set -e
if [ "$mt_status" -gt 1 ]; then
  echo "FEHLER: git merge-tree schlug fehl (Exit $mt_status)." >&2
  exit 2
fi
auto_tree=$(printf '%s\n' "$mt_out" | head -1)
conflict_files=$(printf '%s\n' "$mt_out" | sed -n '2,$p' | sed '/^$/d')

# Rauschpfade, in denen Abweichungen erwartbar/unkritisch sind.
exclude=(':(exclude)web_dist' ':(exclude)*.lock' ':(exclude)package-lock.json' \
         ':(exclude)web/package-lock.json' ':(exclude).github' ':(exclude)*.min.js')

echo "== merge-audit: $merge =="
echo "   Parents: P1(fork)=$p1"
echo "            P2(other)=$p2"
echo "   Automerge-Tree: $auto_tree (merge-tree exit $mt_status)"
echo
if [ -n "$conflict_files" ]; then
  echo "-- Konfliktdateien laut Automerge (manuelle Aufloesung war noetig):"
  printf '%s\n' "$conflict_files" | sed 's/^/   /'
  echo
fi

echo "-- Abweichungen: tatsaechliches Merge-Ergebnis vs. Clean-Automerge"
echo "   (jede Zeile = manuelle Entscheidung -> im Receipt begruenden):"
deviations=$(git diff --stat "$auto_tree" "$merge" -- . "${exclude[@]}")
if [ -z "$deviations" ]; then
  echo "   KEINE — Merge entspricht dem Automerge."
  exit 0
fi
printf '%s\n' "$deviations" | sed 's/^/   /'
echo
echo "Detail pro Datei: git diff $auto_tree $merge -- <datei>"
echo "Verlust-Richtung pruefen: git diff $merge^1 $merge -- <datei>  (Fork-Seite)"
echo "                          git diff $merge^2 $merge -- <datei>  (Upstream-Seite)"

if [ "$strict" = "--strict" ]; then
  exit 1
fi
