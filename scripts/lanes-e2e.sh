#!/usr/bin/env bash
# Isolated E2E runner for the /lanes model platform.
# Boots the WORKTREE backend (new endpoints) + the gate-built WORKTREE web_dist
# on an ephemeral loopback port with a disposable HERMES_HOME seeded with a few
# profile configs (so the matrix + reasoning enabled/disabled states render for
# real). The Playwright spec route-mocks only the probe POSTs (deterministic,
# no real model calls / no cost). Tears everything down on exit.
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="/home/piet/.hermes/hermes-agent/venv/bin/python"
tmp_home="$(mktemp -d -t hermes-lanes-e2e-home.XXXXXX)"
server_pid=""
server_log="${tmp_home}/server.log"

cleanup() {
  local status=$?
  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
  fi
  rm -rf "${tmp_home}"
  exit "${status}"
}
trap cleanup EXIT INT TERM

# Seed profile configs: gpt-5.x rows (reasoning ENABLED) + an alibaba/qwen row
# (reasoning DISABLED — honest: no Reasoning-Knopf in the transport).
seed_profile() {
  local name="$1"; shift
  mkdir -p "${tmp_home}/profiles/${name}"
  cat > "${tmp_home}/profiles/${name}/config.yaml"
}
seed_profile coder <<'YAML'
model:
  provider: openai-codex
  default: gpt-5.6-sol
worker_runtime: hermes
YAML
seed_profile reviewer <<'YAML'
model:
  provider: openai-codex
  default: gpt-5.6-sol
worker_runtime: hermes
YAML
seed_profile research <<'YAML'
model:
  provider: alibaba-token-plan
  default: qwen3.8-max-preview
worker_runtime: hermes
YAML

export HERMES_HOME="${tmp_home}"
export HERMES_SANDBOX_MODE=1
export PYTHONPATH="${repo_root}"
unset HERMES_KANBAN_DB HERMES_KANBAN_BOARD HERMES_PROFILE HERMES_CONFIG

# Worktree backend (PYTHONPATH wins over the editable install) + gate web_dist.
(
  cd "${repo_root}"
  "${VENV_PY}" -m hermes_cli.main dashboard --no-open --host 127.0.0.1 --port 0 --skip-build
) >"${server_log}" 2>&1 &
server_pid=$!

port=""
for _ in {1..200}; do
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    echo "LANES_E2E: server exited before readiness; tail:" >&2
    tail -40 "${server_log}" >&2 || true
    exit 1
  fi
  port="$("${VENV_PY}" - "${server_log}" <<'PY'
import re, sys
from pathlib import Path
t = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace") if Path(sys.argv[1]).exists() else ""
m = re.search(r"HERMES_(?:DASHBOARD|BACKEND)_READY port=(\d+)", t)
print(m.group(1) if m else "")
PY
)"
  if [[ -n "${port}" ]]; then break; fi
  sleep 0.25
done
if [[ -z "${port}" ]]; then
  echo "LANES_E2E: server did not become ready; tail:" >&2
  tail -40 "${server_log}" >&2 || true
  exit 1
fi
echo "LANES_E2E_READY port=${port}"

cd "${repo_root}/web"
PLAYWRIGHT_BASE_URL="http://127.0.0.1:${port}" \
  ../node_modules/.bin/playwright test e2e/lanes-platform.spec.ts --reporter=list
