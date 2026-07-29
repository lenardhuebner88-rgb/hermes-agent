# CANDIDATES — fork-owned modules that have a same-named test file

Generated 2026-07-28. The order IS the work order: the dense band first (120-900 LoC,
fast baseline), then the small ones, the heavyweights last (>900 LoC — baseline often
>30 s, only touch them once the rest is done).

Fork-owned = present in HEAD, absent from `origin/main`. Reproduce with:

    comm -23 <(git ls-tree -r --name-only HEAD -- hermes_cli loops | sort) \
             <(git ls-tree -r --name-only origin/main -- hermes_cli loops | sort)

98 candidates. Tick a box only after its LEDGER entry exists.


## Band A — 120-900 LoC (start here)

- [x] `hermes_cli/autoresearch_reconcile.py` (879 LoC) — `tests/test_autoresearch_reconcile.py`
- [x] `hermes_cli/pa_graph.py` (874 LoC) — `tests/hermes_cli/test_pa_graph.py`
- [x] `hermes_cli/plan_compiler.py` (859 LoC) — `tests/hermes_cli/test_plan_compiler.py`
- [x] `hermes_cli/operator_inventory.py` (771 LoC) — `tests/hermes_cli/test_operator_inventory.py`
- [x] `hermes_cli/usage_facts_db.py` (747 LoC) — `tests/hermes_cli/test_usage_facts_db.py`
- [x] `hermes_cli/autoresearch_budget.py` (702 LoC) — `tests/test_autoresearch_budget.py`
- [x] `hermes_cli/pa_planspec.py` (689 LoC) — `tests/hermes_cli/test_pa_planspec.py`
- [x] `hermes_cli/active_provider_facts.py` (677 LoC) — `tests/hermes_cli/test_active_provider_facts.py`
- [x] `hermes_cli/voice_spar_session.py` (676 LoC) — `tests/hermes_cli/test_voice_spar_session.py`
- [x] `hermes_cli/subcommands/vision.py` (658 LoC) — `tests/hermes_cli/subcommands/test_vision.py`
- [x] `hermes_cli/pa_brief.py` (643 LoC) — `tests/hermes_cli/test_pa_brief.py`
- [x] `hermes_cli/kanban_landed.py` (627 LoC) — `tests/hermes_cli/test_kanban_landed.py`
- [x] `hermes_cli/kanban_lane_fixer.py` (606 LoC) — `tests/hermes_cli/test_kanban_lane_fixer.py`
- [x] `hermes_cli/host_usage.py` (590 LoC) — `tests/hermes_cli/test_host_usage.py`
- [x] `hermes_cli/kanban_discord_report.py` (583 LoC) — `tests/hermes_cli/test_kanban_discord_report.py`
- [x] `hermes_cli/control_plane_gate.py` (570 LoC) — `tests/hermes_cli/test_control_plane_gate.py`
- [x] `hermes_cli/pressure_status.py` (570 LoC) — `tests/hermes_cli/test_pressure_status.py`
- [x] `hermes_cli/pa_journal.py` (514 LoC) — `tests/hermes_cli/test_pa_journal.py`
- [x] `hermes_cli/library_corrections.py` (505 LoC) — `tests/hermes_cli/test_library_corrections.py`
- [x] `hermes_cli/pa_actions.py` (479 LoC) — `tests/hermes_cli/test_pa_actions.py`
- [x] `hermes_cli/funnel.py` (469 LoC) — `tests/hermes_cli/test_funnel.py`
- [x] `hermes_cli/pa_loops.py` (464 LoC) — `tests/hermes_cli/test_pa_loops.py`
- [x] `hermes_cli/disposition.py` (458 LoC) — `tests/hermes_cli/test_disposition.py`
- [x] `hermes_cli/auto_release.py` (442 LoC) — `tests/hermes_cli/test_auto_release.py`
- [x] `hermes_cli/symbol_test_narrowing.py` (439 LoC) — `tests/hermes_cli/test_symbol_test_narrowing.py` *(REJECTED: probe timeout)*
- [x] `hermes_cli/capability_researcher.py` (438 LoC) — `tests/test_capability_researcher.py`
- [x] `hermes_cli/memory_digest.py` (438 LoC) — `tests/hermes_cli/test_memory_digest.py`
- [x] `hermes_cli/library_models.py` (425 LoC) — `tests/hermes_cli/test_library_models.py`
- [x] `hermes_cli/terminal_candidates.py` (416 LoC) — `tests/hermes_cli/test_terminal_candidates.py`
- [x] `hermes_cli/agent_question_push.py` (389 LoC) — `tests/hermes_cli/test_agent_question_push.py`
- [x] `hermes_cli/gate_leaker.py` (389 LoC) — `tests/hermes_cli/test_gate_leaker.py`
- [x] `hermes_cli/goal_judge_rendering.py` (379 LoC) — `tests/hermes_cli/test_goal_judge_rendering.py`
- [x] `hermes_cli/kanban_close_sprint.py` (374 LoC) — `tests/hermes_cli/test_kanban_close_sprint.py`
- [x] `hermes_cli/strategist_surface.py` (366 LoC) — `tests/hermes_cli/test_strategist_surface.py`
- [x] `hermes_cli/library_results.py` (356 LoC) — `tests/hermes_cli/test_library_results.py`
- [x] `hermes_cli/pa_health.py` (350 LoC) — `tests/hermes_cli/test_pa_health.py`
- [x] `hermes_cli/pa_news.py` (342 LoC) — `tests/hermes_cli/test_pa_news.py`
- [x] `hermes_cli/design_board_kanban.py` (327 LoC) — `tests/test_design_board_kanban.py`
- [x] `hermes_cli/library_state.py` (323 LoC) — `tests/hermes_cli/test_library_state.py`
- [x] `hermes_cli/autoresearch_lane_contracts.py` (322 LoC) — `tests/hermes_cli/test_autoresearch_lane_contracts.py`
- [x] `hermes_cli/health_status.py` (322 LoC) — `tests/hermes_cli/test_health_status.py`
- [x] `hermes_cli/kanban_shadow_routing.py` (286 LoC) — `tests/hermes_cli/test_kanban_shadow_routing.py`
- [x] `hermes_cli/agent_question_suggest.py` (285 LoC) — `tests/hermes_cli/test_agent_question_suggest.py`
- [x] `hermes_cli/scoped_auto_commit.py` (281 LoC) — `tests/hermes_cli/test_scoped_auto_commit.py`
- [x] `loops/model_catalog.py` (253 LoC) — `tests/loops/test_model_catalog.py`
- [x] `hermes_cli/design_board_view.py` (243 LoC) — `tests/test_design_board_view.py`
- [x] `hermes_cli/affected_test_budget.py` (238 LoC) — `tests/hermes_cli/test_affected_test_budget.py`
- [x] `hermes_cli/voice_phone_action.py` (232 LoC) — `tests/hermes_cli/test_voice_phone_action.py`
- [x] `hermes_cli/system_stats_history.py` (224 LoC) — `tests/hermes_cli/test_system_stats_history.py`
- [x] `hermes_cli/plan_prose.py` (221 LoC) — `tests/hermes_cli/test_plan_prose.py`
- [x] `hermes_cli/autoresearch_lane_models.py` (209 LoC) — `tests/test_autoresearch_lane_models.py`
- [x] `hermes_cli/kanban_score_hygiene.py` (184 LoC) — `tests/hermes_cli/test_kanban_score_hygiene.py`
- [x] `hermes_cli/stats_config.py` (180 LoC) — `tests/test_stats_config.py`
- [x] `hermes_cli/pa_titles.py` (178 LoC) — `tests/hermes_cli/test_pa_titles.py`
- [x] `hermes_cli/design_board_store.py` (171 LoC) — `tests/test_design_board_store.py`
- [ ] `hermes_cli/cron_observability.py` (168 LoC) — `tests/hermes_cli/test_cron_observability.py`
- [ ] `hermes_cli/kanban_dispatch_policy.py` (168 LoC) — `tests/hermes_cli/test_kanban_dispatch_policy.py`
- [ ] `hermes_cli/operator_digest_view.py` (168 LoC) — `tests/hermes_cli/test_operator_digest_view.py`
- [ ] `hermes_cli/pa_live_share.py` (143 LoC) — `tests/hermes_cli/test_pa_live_share.py`
- [ ] `hermes_cli/pa_push.py` (140 LoC) — `tests/hermes_cli/test_pa_push.py`
- [ ] `hermes_cli/design_board_cli.py` (126 LoC) — `tests/test_design_board_cli.py`

## Band B — <120 LoC

- [ ] `hermes_cli/metrics_lite.py` (119 LoC) — `tests/hermes_cli/test_metrics_lite.py`
- [ ] `hermes_cli/claude_cli_model_catalog.py` (116 LoC) — `tests/hermes_cli/test_claude_cli_model_catalog.py`
- [ ] `hermes_cli/kanban_chain_repair.py` (112 LoC) — `tests/hermes_cli/test_kanban_chain_repair.py`
- [ ] `hermes_cli/autoresearch_runs.py` (110 LoC) — `tests/test_autoresearch_runs.py`
- [ ] `hermes_cli/vault_provenance_view.py` (105 LoC) — `tests/hermes_cli/test_vault_provenance_view.py`
- [ ] `hermes_cli/kanban_comment_delivery.py` (94 LoC) — `tests/hermes_cli/test_kanban_comment_delivery.py`
- [ ] `hermes_cli/pa_reminders.py` (87 LoC) — `tests/hermes_cli/test_pa_reminders.py`
- [ ] `hermes_cli/kanban_escalation_class.py` (86 LoC) — `tests/hermes_cli/test_kanban_escalation_class.py`
- [ ] `hermes_cli/oneshot_service_tier.py` (80 LoC) — `tests/hermes_cli/test_oneshot_service_tier.py`
- [ ] `hermes_cli/voice_health_track.py` (76 LoC) — `tests/hermes_cli/test_voice_health_track.py`
- [ ] `hermes_cli/autoresearch_lane_runner.py` (73 LoC) — `tests/hermes_cli/test_autoresearch_lane_runner.py`
- [ ] `hermes_cli/design_board_tailwind.py` (65 LoC) — `tests/test_design_board_tailwind.py`
- [ ] `hermes_cli/error_sanitize.py` (55 LoC) — `tests/hermes_cli/test_error_sanitize.py`

## Band C — >900 LoC (last)

- [ ] `hermes_cli/agent_terminals.py` (3491 LoC) — `tests/hermes_cli/test_agent_terminals.py`
- [ ] `hermes_cli/voice_ws.py` (3300 LoC) — `tests/hermes_cli/test_voice_ws.py`
- [ ] `hermes_cli/outcome_verification.py` (3073 LoC) — `tests/test_outcome_verification.py`
- [ ] `hermes_cli/planspecs.py` (2831 LoC) — `tests/hermes_cli/test_planspecs.py`
- [ ] `loops/runner.py` (2786 LoC) — `tests/loops/test_runner.py`
- [ ] `hermes_cli/projects_overview.py` (2472 LoC) — `tests/hermes_cli/test_projects_overview.py`
- [ ] `hermes_cli/autoresearch_proposals.py` (2457 LoC) — `tests/test_autoresearch_proposals.py`
- [ ] `hermes_cli/vision_metrics.py` (2313 LoC) — `tests/hermes_cli/test_vision_metrics.py`
- [ ] `hermes_cli/agent_questions.py` (1990 LoC) — `tests/hermes_cli/test_agent_questions.py`
- [ ] `hermes_cli/voice_live_session.py` (1903 LoC) — `tests/hermes_cli/test_voice_live_session.py`
- [ ] `hermes_cli/library_view.py` (1885 LoC) — `tests/hermes_cli/test_library_view.py`
- [ ] `hermes_cli/pa_chat.py` (1833 LoC) — `tests/hermes_cli/test_pa_chat.py`
- [ ] `hermes_cli/kanban_closeout.py` (1292 LoC) — `tests/hermes_cli/test_kanban_closeout.py`
- [ ] `hermes_cli/usage_facts_readmodel.py` (1260 LoC) — `tests/hermes_cli/test_usage_facts_readmodel.py`
- [ ] `hermes_cli/foreign_lane_harvest.py` (1220 LoC) — `tests/hermes_cli/test_foreign_lane_harvest.py`
- [ ] `hermes_cli/control_loops.py` (1085 LoC) — `tests/hermes_cli/test_control_loops.py`
- [ ] `hermes_cli/affected_test_mapping.py` (1076 LoC) — `tests/hermes_cli/test_affected_test_mapping.py`
- [ ] `hermes_cli/deep_audit.py` (1038 LoC) — `tests/test_deep_audit.py`
- [ ] `hermes_cli/langfuse_scores_export.py` (966 LoC) — `tests/hermes_cli/test_langfuse_scores_export.py`
- [ ] `hermes_cli/pa_search.py` (949 LoC) — `tests/hermes_cli/test_pa_search.py`
- [ ] `hermes_cli/library_knowledge.py` (911 LoC) — `tests/hermes_cli/test_library_knowledge.py`
- [ ] `hermes_cli/claude_code_harvester.py` (903 LoC) — `tests/hermes_cli/test_claude_code_harvester.py`
- [ ] `hermes_cli/kanban_review_policy.py` (902 LoC) — `tests/hermes_cli/test_kanban_review_policy.py`
- [ ] `hermes_cli/test_foundry.py` (901 LoC) — `tests/test_test_foundry.py`
