#!/usr/bin/env bash
# dogfood.sh <planspec.md> — run a binding dogfood PlanSpec through the Hermes
# kanban tunnel end-to-end: ingest -> release from scheduled -> monitor until the
# reviewer-join is terminal -> print each lane's evidence + the join verdict.
#
# Launch with run_in_background:true (it polls with sleep, blocked in foreground).
set -uo pipefail
SPEC="${1:?usage: dogfood.sh <planspec.md>}"
REPO=/home/piet/.hermes/hermes-agent
H="$REPO/venv/bin/hermes"
cd "$REPO"

J=$("$H" plan ingest "$SPEC" --author dogfood --json) || { echo "ingest failed"; exit 1; }
read -r ROOT CHILDREN JOIN < <(python3 - "$J" <<'PY'
import json,sys
d=json.loads(sys.argv[1])
ids=d["child_ids"]; kids=d.get("children") or []
# join = the child with non-empty planspec_deps (the reviewer aggregate); fallback: last child
join=next((ids[i] for i,c in enumerate(kids) if c.get("planspec_deps")), ids[-1])
print(d["root_task_id"], ",".join(ids), join)
PY
)
CH=${CHILDREN//,/ }
echo "root=$ROOT  children=$CH  join=$JOIN"
"$H" kanban unblock $CH >/dev/null && echo "released to ready"

st() { "$H" kanban show "$1" --json 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin)['task']['status'])" 2>/dev/null; }
for i in $(seq 1 60); do
  js=$(st "$JOIN")
  echo "[poll $i] join=$js | leaves: $(for c in $CH; do printf '%s ' "$(st "$c")"; done)"
  case "$js" in done|blocked) echo ">> join terminal"; break;; esac
  sleep 30
done

echo "===== LANE EVIDENCE ====="
for c in $CH; do
  [ "$c" = "$JOIN" ] && continue
  "$H" kanban show "$c" --json | python3 -c "import json,sys;d=json.load(sys.stdin);t=d['task'];print('##',t['id'],t['assignee'],t['status']);print((d.get('latest_summary') or '(none)')[:900]);print()"
done
echo "===== JOIN VERDICT ($JOIN) ====="
"$H" kanban show "$JOIN" --json | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('latest_summary') or '(none)');print();cs=d.get('comments') or [];print((cs[-1].get('body') or '')[:2600] if cs else '')"
