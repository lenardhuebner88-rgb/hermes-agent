#!/usr/bin/env bash
# Dashboard rollback (C1, auto-release counterpart of deploy_dashboard.sh):
# return the served dashboard to a known-good anchor tag, rebuild, restart,
# re-verify health. Exits non-zero when the rollback target cannot be reached
# safely or health stays red afterwards.
#
# Usage: scripts/rollback_dashboard.sh <anchor-tag-or-commit>
#   e.g. scripts/rollback_dashboard.sh release/pre-deploy/20260705T140000
#
# Safety shape:
#   * refuses to run with a dirty tracked working tree (parallel sessions —
#     never clobber foreign work; untracked files are left alone),
#   * moves via `git checkout --detach <target>` (reversible; NO reset --hard),
#   * after a successful rollback the checkout stays DETACHED on the anchor —
#     the operator/orchestrator returns to main deliberately
#     (`git checkout main`) once the bad deploy is dealt with.
set -euo pipefail

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "[rollback] usage: rollback_dashboard.sh <anchor-tag-or-commit>" >&2
  # Offer the operator the most recent anchors as targets.
  git tag -l 'release/pre-deploy/*' | tail -5 >&2 || true
  exit 2
fi
cd "$(dirname "$0")/.."

if ! git rev-parse --verify --quiet "${TARGET}^{commit}" >/dev/null; then
  echo "[rollback] FAILED — unknown target: $TARGET" >&2
  exit 2
fi

# Dirty tracked files → abort (foreign parallel-session work must not be
# touched; a rollback on top of uncommitted edits is undefined anyway).
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "[rollback] FAILED — tracked working tree is dirty; commit/stash first:" >&2
  git status --porcelain --untracked-files=no | head -20 >&2
  exit 2
fi

FROM_SHA=$(git rev-parse --short HEAD)
TO_SHA=$(git rev-parse --short "${TARGET}^{commit}")
echo "[rollback] $FROM_SHA -> $TO_SHA ($TARGET)"
git checkout --detach "${TARGET}^{commit}"
git tag "release/rollback/$(date +%Y%m%dT%H%M%S)" HEAD 2>/dev/null || true

echo "[rollback] rebuilding frontend bundle (web/) ..."
( cd web && npm run build )

echo "[rollback] restarting hermes-dashboard.service ..."
systemctl --user restart hermes-dashboard.service

# Health-poll + payload validation — same truth as deploy_dashboard.sh
# (API payload, not screenshot).
for i in $(seq 1 15); do
  if curl -s -o /dev/null --max-time 2 http://127.0.0.1:9119/api/status; then break; fi
  sleep 1
done
LB=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 --max-redirs 0 http://127.0.0.1:9119/control || echo 000)
PAYLOAD_OK=$(curl -fsS --max-time 8 http://127.0.0.1:9119/api/status | python3 -c 'import json,sys; d=json.load(sys.stdin); assert isinstance(d, dict) and d.get("version"); print("version=%s gateway_running=%s" % (d.get("version"), d.get("gateway_running")))' 2>/tmp/hermes-rollback-payload.err || true)
echo "[rollback] health: loopback=$LB payload=${PAYLOAD_OK:-INVALID}"
if [ -z "$PAYLOAD_OK" ] || { [ "$LB" != "200" ] && [ "$LB" != "302" ]; }; then
  echo "[rollback] FAILED — dashboard still unhealthy on $TO_SHA (loopback=$LB)" >&2
  cat /tmp/hermes-rollback-payload.err 2>/dev/null >&2 || true
  exit 1
fi
echo "[rollback] OK — serving $TO_SHA (checkout is DETACHED; 'git checkout main' to return)"
