#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/visual-verify.sh [--skip-build] [--output-dir DIR] [--seed fixture.json] [--self-test] [--viewports SPEC] [--interactive] <route> [<route>...]

Starts an auth-free disposable Hermes Web UI on 127.0.0.1:0 with an isolated
HERMES_HOME, captures screenshots at 390px, 820px, and desktop widths, and
writes PNGs plus summary.json to the output directory.

Options:
  --skip-build       Serve the existing web/dist instead of running npm build.
  --output-dir DIR   Evidence directory (default: ${HERMES_REPORTS_ROOT:-$HOME/.hermes/reports}/<task_id>/<UTC-timestamp>/).
  --seed FILE        Apply a JSON seed fixture to the isolated HERMES_HOME first.
  --self-test        Equivalent to route /control when no routes are supplied.
  --viewports SPEC   Override the default viewport trio, e.g. "390x844,tablet-lg=840x1118".
  --interactive      Additionally run the click/dialog check (buzz-agent cleanup dialog on
                     the first route) and write artifacts to <output-dir>/dialog-interactive/.
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
skip_build=0
output_dir=""
seed_file=""
self_test=0
viewports_spec=""
interactive=0
routes=()
server_pid=""
tmp_home=""
server_log=""

cleanup() {
  local status=$?
  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
  fi
  if [[ -n "${tmp_home}" ]]; then
    rm -rf "${tmp_home}"
  fi
  exit "${status}"
}
trap cleanup EXIT INT TERM

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build)
      skip_build=1
      shift
      ;;
    --output-dir)
      output_dir="${2:-}"
      if [[ -z "${output_dir}" ]]; then
        echo "--output-dir requires a directory" >&2
        exit 2
      fi
      shift 2
      ;;
    --seed)
      seed_file="${2:-}"
      if [[ -z "${seed_file}" ]]; then
        echo "--seed requires a fixture path" >&2
        exit 2
      fi
      shift 2
      ;;
    --self-test)
      self_test=1
      shift
      ;;
    --viewports)
      viewports_spec="${2:-}"
      if [[ -z "${viewports_spec}" ]]; then
        echo "--viewports requires a spec" >&2
        exit 2
      fi
      shift 2
      ;;
    --interactive)
      interactive=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --*)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      routes+=("$1")
      shift
      ;;
  esac
done

if [[ "${self_test}" -eq 1 && "${#routes[@]}" -eq 0 ]]; then
  routes=("/control")
fi
if [[ "${#routes[@]}" -eq 0 ]]; then
  usage >&2
  exit 2
fi

reports_root="${HERMES_REPORTS_ROOT:-${HOME}/.hermes/reports}"
task_id="${HERMES_KANBAN_TASK:-local}"
if [[ -z "${output_dir}" ]]; then
  output_dir="${reports_root}/${task_id}/$(date -u +%Y%m%dT%H%M%SZ)"
fi

# Hinweis auf fruehere Evidenz desselben Tasks: ein Folgerun soll wissen, dass
# es sie gibt (Run 8912 hat sie gesucht, nicht gefunden und alles neu erzeugt).
# Nur eine Zeile — keine Wiederverwendung, kein Ueberspringen von Arbeit.
evidence_parent="${reports_root}/${task_id}"
if [[ -d "${evidence_parent}" ]]; then
  output_dir_abs="$(realpath -m "${output_dir}")"
  previous_run=""
  while IFS= read -r candidate; do
    if [[ "$(realpath -m "${candidate}")" != "${output_dir_abs}" ]]; then
      previous_run="${candidate}"
      break
    fi
  done < <(find "${evidence_parent}" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -rn | cut -d' ' -f2-)
  if [[ -n "${previous_run}" ]]; then
    echo "visual-verify: fruehere Evidenz fuer Task ${task_id}: ${previous_run}"
  fi
fi

mkdir -p "${output_dir}"
output_dir="$(cd "${output_dir}" && pwd)"

tmp_home="$(mktemp -d -t hermes-visual-verify-home.XXXXXX)"
server_log="${output_dir}/server.log"

apply_seed() {
  local fixture="$1"
  HERMES_HOME="${tmp_home}" python3 - "${fixture}" <<'PY'
import json
import os
import sys
from pathlib import Path

fixture = Path(sys.argv[1]).resolve()
home = Path(os.environ["HERMES_HOME"]).resolve()
data = json.loads(fixture.read_text(encoding="utf-8"))
files = data.get("files", [])
if not isinstance(files, list):
    raise SystemExit("seed fixture must contain a 'files' list")
for item in files:
    if not isinstance(item, dict) or "path" not in item:
        raise SystemExit("each seed file needs a path")
    rel = Path(str(item["path"]))
    if rel.is_absolute() or ".." in rel.parts:
        raise SystemExit(f"seed path must stay inside isolated HERMES_HOME: {rel}")
    target = (home / rel).resolve()
    if not str(target).startswith(str(home) + os.sep):
        raise SystemExit(f"seed path escapes isolated HERMES_HOME: {rel}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if "json" in item:
        target.write_text(json.dumps(item["json"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        target.write_text(str(item.get("content", "")), encoding="utf-8")
PY
}

if [[ -n "${seed_file}" ]]; then
  apply_seed "${seed_file}"
fi

if [[ "${skip_build}" -eq 0 ]]; then
  # Keep autonomous/worktree verification builds out of the tracked production
  # assets. vite.config.ts and hermes_cli.web_server share this override, so the
  # disposable server serves exactly the branch build without dirtying git.
  export HERMES_WEB_DIST="${tmp_home}/web_dist"
  (cd "${repo_root}/web" && npm run build)
fi

export HERMES_HOME="${tmp_home}"
export HERMES_SANDBOX_MODE=1
unset HERMES_KANBAN_DB HERMES_KANBAN_BOARD HERMES_PROFILE HERMES_CONFIG

(
  cd "${repo_root}"
  python3 -m hermes_cli.main dashboard --no-open --host 127.0.0.1 --port 0 --skip-build
) >"${server_log}" 2>&1 &
server_pid=$!

port=""
for _ in {1..100}; do
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    echo "Hermes disposable server exited before readiness; see ${server_log}" >&2
    exit 1
  fi
  port="$(python3 - "${server_log}" <<'PY'
import re
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace") if Path(sys.argv[1]).exists() else ""
match = re.search(r"HERMES_(?:DASHBOARD|BACKEND)_READY port=(\d+)", text)
print(match.group(1) if match else "")
PY
)"
  if [[ -n "${port}" ]]; then
    break
  fi
  sleep 0.2
done

if [[ -z "${port}" ]]; then
  echo "Hermes disposable server did not become ready; see ${server_log}" >&2
  exit 1
fi

runner_args=(
  --base-url "http://127.0.0.1:${port}"
  --output-dir "${output_dir}"
  --git-head "$(git -C "${repo_root}" rev-parse HEAD)"
)
if [[ -n "${viewports_spec}" ]]; then
  runner_args+=(--viewports "${viewports_spec}")
fi
if [[ "${interactive}" -eq 1 ]]; then
  runner_args+=(--interactive)
fi
runner_args+=("${routes[@]}")

node "${repo_root}/scripts/visual_verify_runner.mjs" "${runner_args[@]}"
