#!/usr/bin/env bash
# Validate the isolated, loopback-only Langfuse smoke prerequisites without
# starting containers or sending any data.  A live smoke remains operator-owned.
set -euo pipefail

readonly LANGFUSE_REF="v3.224.0"
readonly LANGFUSE_COMMIT="d044f366816282235898a0673d5700e05ccbee8c"
readonly EXPECTED_BIND="127.0.0.1:13000:3000"
readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly OVERLAY="$REPO_ROOT/plugins/observability/langfuse/self-host-smoke.compose.yaml"
readonly UPSTREAM_URL="https://raw.githubusercontent.com/langfuse/langfuse/${LANGFUSE_COMMIT}/docker-compose.yml"

cleanup() { rm -rf "${work:-}"; }
trap cleanup EXIT

command -v curl >/dev/null || { echo "curl is required" >&2; exit 127; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 127; }
[[ -f "$OVERLAY" ]] || { echo "missing smoke overlay: $OVERLAY" >&2; exit 1; }

grep -Fq "$EXPECTED_BIND" "$OVERLAY" || {
  echo "smoke overlay must bind Langfuse web to $EXPECTED_BIND" >&2
  exit 1
}
grep -Fq "langfuse:${LANGFUSE_REF#v}" "$OVERLAY" || {
  echo "smoke overlay does not pin Langfuse image $LANGFUSE_REF" >&2
  exit 1
}

work="$(mktemp -d)"
curl --fail --silent --show-error --location "$UPSTREAM_URL" -o "$work/langfuse-compose.yaml"
docker compose -f "$work/langfuse-compose.yaml" -f "$OVERLAY" config >/dev/null

# These exact tests exercise the synthetic LLM trace and tool observation,
# fail-closed raw-content contract, allowlisted correlation and the
# connection/timeout/invalid-export result+outcome invariant.  No credentials,
# plugin enablement, service, or outbound Langfuse call is involved.
"$REPO_ROOT/scripts/run_tests.sh" tests/plugins/test_langfuse_plugin.py \
  -k 'unfinalized_turn_does_not_capture_next_turn or post_tool_call_backfills_matching_turn_tool_call_output or raw_prompt_output_and_tool_arguments_are_never_serialized or correlation_allowlist_keeps_only_hook_values or connection_timeout_and_invalid_export_leave_result_and_outcome_unchanged' \
  --quiet

echo "COMPOSE_VALIDATED ref=$LANGFUSE_REF commit=$LANGFUSE_COMMIT bind=$EXPECTED_BIND"
echo "SMOKE_DRY_RUN_VALIDATED synthetic_llm_tool_redaction_and_fail_soft"
echo "OPERATOR_LIVE_SMOKE_PENDING loopback_only disposable_project synthetic_data_only"
