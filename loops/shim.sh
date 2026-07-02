#!/usr/bin/env bash
# loops/shim.sh <pack> — systemd-ExecStart-Wrapper für den Loop-Runner.
# Läuft im Checkout, zu dem dieses Script gehört (Symlink-fest), mit dem
# Repo-venv. Alles Weitere (Locks, Worktree, Stop-Kriterien, Ledger) macht
# der Runner selbst. Aufruf durch hermes-loop@<pack>.service, manuell:
#   ~/.hermes/hermes-agent/loops/shim.sh builder-reviewer
set -uo pipefail

PACK="${1:?usage: shim.sh <pack-name>}"
REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
PY="$REPO/venv/bin/python"
[ -x "$PY" ] || { echo "shim: venv fehlt: $PY" >&2; exit 2; }

# loops/ ist bewusst NICHT paketiert (pyproject packages) — cwd=REPO + PYTHONPATH
# machen `-m loops.runner` unabhängig davon auffindbar.
cd "$REPO"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" -m loops.runner --pack "$PACK" --cmd night
