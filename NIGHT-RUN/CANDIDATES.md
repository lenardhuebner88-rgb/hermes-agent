# CANDIDATES — fork-owned operational modules with a same-named test file

Generated 2026-07-29. The order IS the work order: the dense band first
(150-900 LoC, fast baseline), then the small ones, the heavyweights last.

Scope is deliberately disjoint from the sibling run in `qwen-mutation-nightly`,
which owns `hermes_cli/` and `loops/`. Fork-owned = in HEAD, absent from
`origin/main`. Reproduce with:

    comm -23 <(git ls-tree -r --name-only HEAD -- scripts tools gateway agent plugins | sort) \
             <(git ls-tree -r --name-only origin/main -- scripts tools gateway agent plugins | sort)

44 candidates. Tick a box only after its LEDGER entry exists.


## Band A — 150-900 LoC (start here)

- [ ] `tools/voice_live_tools.py` (876 LoC) — `tests/hermes_cli/test_voice_live_tools.py`
- [ ] `scripts/run_autoresearch_request.py` (846 LoC) — `tests/test_run_autoresearch_request.py`
- [ ] `scripts/autoresearch_v2_nightly.py` (801 LoC) — `tests/test_autoresearch_v2_nightly.py`
- [ ] `scripts/dogfood_repo_cap_evidence.py` (731 LoC) — `tests/scripts/test_dogfood_repo_cap_evidence.py`
- [ ] `gateway/kanban_alerts.py` (669 LoC) — `tests/gateway/test_kanban_alerts.py`
- [ ] `hermes_cli/subcommands/vision.py` (658 LoC) — `tests/hermes_cli/subcommands/test_vision.py`
- [ ] `scripts/refactor/split_module.py` (598 LoC) — `tests/refactor/test_split_module.py`
- [ ] `scripts/langfuse_dashboards.py` (587 LoC) — `tests/scripts/test_langfuse_dashboards.py`
- [ ] `scripts/autoresearch_writer.py` (553 LoC) — `tests/test_autoresearch_writer.py`
- [ ] `scripts/daily_research_post.py` (519 LoC) — `tests/scripts/test_daily_research_post.py`
- [ ] `scripts/refactor/fork_loss_check.py` (504 LoC) — `tests/refactor/test_fork_loss_check.py`
- [ ] `tools/verification_gate_tool.py` (475 LoC) — `tests/tools/test_verification_gate_tool.py`
- [ ] `scripts/render_autoresearch_dashboard.py` (467 LoC) — `tests/test_render_autoresearch_dashboard.py`
- [ ] `scripts/check_skill_hygiene.py` (434 LoC) — `tests/scripts/test_check_skill_hygiene.py`
- [ ] `plugins/observability/board_facts/auxiliary_wrapper.py` (427 LoC) — `tests/plugins/observability/board_facts/test_auxiliary_wrapper.py`
- [ ] `scripts/scan_kanban_block_notifications.py` (402 LoC) — `tests/test_scan_kanban_block_notifications.py`
- [ ] `scripts/gate_load_stamp.py` (356 LoC) — `tests/scripts/test_gate_load_stamp.py`
- [ ] `gateway/profile_policy.py` (349 LoC) — `tests/gateway/test_profile_policy.py`
- [ ] `tools/kanban_workspace_runner.py` (321 LoC) — `tests/tools/test_kanban_workspace_runner.py`
- [ ] `scripts/browser_reap.py` (319 LoC) — `tests/scripts/test_browser_reap.py`
- [ ] `scripts/kanban_dispatcher_watchdog.py` (316 LoC) — `tests/scripts/test_kanban_dispatcher_watchdog.py`
- [ ] `scripts/check_kanban_lifecycle_anchors.py` (311 LoC) — `tests/scripts/test_check_kanban_lifecycle_anchors.py`
- [ ] `agent/tool_result_eliding.py` (295 LoC) — `tests/agent/test_tool_result_eliding.py`
- [ ] `scripts/backfill_session_labels.py` (262 LoC) — `tests/test_backfill_session_labels.py`
- [ ] `scripts/autoresearch_request.py` (258 LoC) — `tests/test_autoresearch_request.py`
- [ ] `scripts/smoke_health_status_auth.py` (244 LoC) — `tests/scripts/test_smoke_health_status_auth.py`
- [ ] `scripts/sync_model_prices.py` (225 LoC) — `tests/scripts/test_sync_model_prices.py`
- [ ] `scripts/watermark_check.py` (218 LoC) — `tests/scripts/test_watermark_check.py`
- [ ] `scripts/cleanup_retry_heavy_scores.py` (217 LoC) — `tests/scripts/test_cleanup_retry_heavy_scores.py`
- [ ] `scripts/storage_guard.py` (217 LoC) — `tests/scripts/test_storage_guard.py`
- [ ] `scripts/reap_stale_sessions.py` (205 LoC) — `tests/test_reap_stale_sessions.py`
- [ ] `scripts/control_shot.py` (191 LoC) — `tests/scripts/test_control_shot.py`
- [ ] `scripts/refactor/layering.py` (175 LoC) — `tests/refactor/test_layering.py`
- [ ] `scripts/voice_spar_smoke.py` (154 LoC) — `tests/scripts/test_voice_spar_smoke.py`
- [ ] `scripts/affected_tests.py` (153 LoC) — `tests/scripts/test_affected_tests.py`
- [ ] `plugins/kanban/dashboard/scorecard_routes.py` (151 LoC) — `tests/plugins/test_scorecard_routes.py`

## Band B — <150 LoC

- [ ] `agent/pricing_feed.py` (145 LoC) — `tests/agent/test_pricing_feed.py`
- [ ] `scripts/refactor/api_snapshot.py` (116 LoC) — `tests/refactor/test_api_snapshot.py`
- [ ] `scripts/voice_reminder_fire.py` (77 LoC) — `tests/hermes_cli/test_voice_reminder_fire.py`
- [ ] `plugins/kanban/lifecycle.py` (39 LoC) — `tests/agent/lsp/test_lifecycle.py`
- [ ] `plugins/kanban/dashboard/digest_routes.py` (20 LoC) — `tests/plugins/test_digest_routes.py`

## Band C — >900 LoC (last)

- [ ] `gateway/pa_watcher.py` (1630 LoC) — `tests/gateway/test_pa_watcher.py`
- [ ] `tools/family_organizer_tool.py` (1452 LoC) — `tests/tools/test_family_organizer_tool.py`
- [ ] `scripts/retention_reap.py` (1082 LoC) — `tests/scripts/test_retention_reap.py`
