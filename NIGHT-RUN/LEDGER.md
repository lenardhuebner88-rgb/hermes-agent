# LEDGER — one entry per candidate module

Format is fixed by `NIGHT-RUN/RUNBOOK.md` step 7. Append only; never rewrite an
earlier entry. `started=` in the STATUS line is the timestamp of entry 1.

<!-- entries below -->
REJECTED hermes_cli/autoresearch_reconcile.py — probe generated 0 mutants (nothing to measure)

## 2. hermes_cli/pa_graph.py — 2026-07-28T22:12:29Z
- probe before: 6/20 = 30%   (source sha1 b7892f11051f)
- survivors killed: [K] bool_op_swap L197, [K] bool_op_swap L272, [K] bool_op_swap L466
- new tests: tests/hermes_cli/test_pa_graph.py::test_normalize_vault_path_drops_dot_and_empty_segments, ::test_link_targets_preserves_angle_bracket_link_with_space, ::test_project_node_label_falls_back_to_slug
- red proof: probe re-measure 30%→45% (3 new kills confirmed by probe)
- probe after: 9/20 = 45%
- gate: narrow pytest 20 passed | ruff clean
- commit: 5f2e1bb02

## 3. hermes_cli/plan_compiler.py — 2026-07-28T22:19:22Z
- probe before: 10/20 = 50%   (source sha1 fdbac37baf4f)
- survivors killed: [K] bool_op_swap L81, [K] bool_op_swap L462, [K] bool_op_swap L463, [K] bool_op_swap L491
- new tests: tests/hermes_cli/test_plan_compiler.py::test_binding_subtask_rejects_whitespace_only_id, ::test_ac_token_uses_core_when_present_and_fallback_when_empty, ::test_plan_wide_ac_threads_into_subtask_without_own_criteria
- red proof: probe re-measure 50%→70% (4 new kills confirmed by probe)
- probe after: 14/20 = 70%
- gate: narrow pytest 37 passed | ruff clean
- commit: d691a8564

## 4. hermes_cli/operator_inventory.py — 2026-07-28T22:24:49Z
- probe before: 8/20 = 40%   (source sha1 40cba509bcc5)
- survivors killed: [K] bool_op_swap L365, [K] bool_op_swap L373
- new tests: tests/hermes_cli/test_operator_inventory.py::test_scrub_redacts_home_path, ::test_classify_process_handles_none_name, ::test_task_hint_returns_none_for_bare_kanban_prefix
- red proof: probe re-measure 40%→50% (2 new kills confirmed by probe; L66/L263 already killed by existing tests)
- probe after: 10/20 = 50%
- gate: narrow pytest 10 passed | ruff clean
- commit: 835a8ac48

## 5. hermes_cli/usage_facts_db.py — 2026-07-28T22:31:19Z
- probe before: 8/20 = 40%   (source sha1 8e78adbb3fe0)
- survivors killed: [K] comparison_swap L497
- new tests: tests/hermes_cli/test_usage_facts_db.py::test_call_kind_main_loop_is_sticky, ::test_record_llm_call_accepts_zero_call_index, ::test_increment_tool_call_accepts_zero_duration
- red proof: probe re-measure 40%→45% (1 new kill confirmed by probe; L396 survived — test exercises upsert but mutant path not reached)
- probe after: 9/20 = 45%
- gate: narrow pytest 19 passed | ruff clean
- commit: ae9adb8c0

## 6. hermes_cli/autoresearch_budget.py — 2026-07-28T22:35:51Z
- probe before: 17/20 = 85%   (source sha1 3152fb8d285a)
- survivors killed: [K] bool_op_swap L114, [K] bool_op_swap L119
- new tests: tests/test_autoresearch_budget.py::test_load_budget_config_respects_custom_timezone, ::test_load_budget_config_respects_custom_unknown_usage_policy
- red proof: probe re-measure 85%→95% (2 new kills confirmed by probe; L183 is equivalent — except handler masks behavioral difference)
- probe after: 19/20 = 95%
- gate: narrow pytest 38 passed | ruff clean
- commit: 1033dd47a

## 7. hermes_cli/pa_planspec.py — 2026-07-28T22:42:06Z
- probe before: 13/20 = 65%   (source sha1 870b87fa80e5)
- survivors killed: [K] bool_op_swap L109, [K] bool_op_swap L236, [K] bool_op_swap L307
- new tests: tests/hermes_cli/test_pa_planspec.py::test_draft_in_project_validator_preserves_value, ::test_compose_draft_prompt_includes_project, ::test_parse_validation_output_clean_with_zero_exit
- red proof: probe re-measure 65%→85% (3 new kills confirmed by probe)
- probe after: 17/20 = 85%
- gate: narrow pytest 19 passed | ruff clean
- commit: fed009813

## 8. hermes_cli/active_provider_facts.py — 2026-07-28T22:46:13Z
- probe before: 5/20 = 25%   (source sha1 f79dd4b2b2f5)
- survivors killed: [K] bool_op_swap L475
- new tests: tests/hermes_cli/test_active_provider_facts.py::test_state_session_reader_rejects_profile_with_slash, ::test_configured_provider_returns_single_match, ::test_metadata_provider_prefers_primary_field
- red proof: probe re-measure 25%→30% (1 new kill confirmed by probe; L89 is equivalent — _allowed_path downstream rejects slash-containing profiles)
- probe after: 6/20 = 30%
- gate: narrow pytest 13 passed | ruff clean
- commit: 839be0980

## 9. hermes_cli/voice_spar_session.py — 2026-07-28T22:52:08Z
- probe before: 10/20 = 50%   (source sha1 614ac29a6d92)
- survivors killed: [K] bool_op_swap L130, [K] bool_op_swap L134, [K] bool_op_swap L479
- new tests: tests/hermes_cli/test_voice_spar_session.py::test_resolve_claude_bin_prefers_env, ::test_resolve_codex_bin_prefers_env, ::test_persistent_claude_lane_defaults_cwd_to_home
- red proof: probe re-measure 50%→65% (3 new kills confirmed by probe)
- probe after: 13/20 = 65%
- gate: narrow pytest 43 passed, 1 skipped | ruff clean
- commit: 20a6ba959

REJECTED hermes_cli/subcommands/vision.py — 18/20 survivors are equivalent mutants: boolean_flip on getattr(args, "json", False) defaults is unobservable because argparse store_true always sets the attribute on the namespace

## 11. hermes_cli/pa_brief.py — 2026-07-28T23:07:56Z
- probe before: 11/20 = 55%
- killed survivors: [K] bool_op_swap L158, [K] bool_op_swap L159, [K] bool_op_swap L160, [K] bool_op_swap L325, [K] bool_op_swap L542
- probe after: 16/20 = 80%
- gate: narrow pytest 11 passed | ruff clean
- commit: 66f0091a9

## 12. hermes_cli/kanban_landed.py — 2026-07-28T23:14:58Z
- probe before: 12/20 = 60%
- killed survivors: [K] bool_op_swap L285, [K] bool_op_swap L293, [K] bool_op_swap L297, [K] bool_op_swap L431
- probe after: 16/20 = 80%
- gate: narrow pytest 15 passed | ruff clean
- commit: 08945e36a

## 13. hermes_cli/kanban_lane_fixer.py — 2026-07-28T23:19:10Z
- probe before: 13/20 = 65%
- killed survivors: [K] bool_op_swap L54, [K] bool_op_swap L93, [K] bool_op_swap L147, [K] bool_op_swap L149
- probe after: 15/20 = 75%
- gate: narrow pytest 11 passed | ruff clean
- commit: 314af0e7e

## 14. hermes_cli/host_usage.py — 2026-07-28T23:21:57Z
- probe before: 5/20 = 25%
- killed survivors: [K] bool_op_swap L92, [K] bool_op_swap L125, [K] bool_op_swap L130, [K] bool_op_swap L143, [K] bool_op_swap L145, [K] bool_op_swap L147
- probe after: 10/20 = 50%
- gate: narrow pytest 6 passed | ruff clean
- commit: eaa69b78b

## 15. hermes_cli/kanban_discord_report.py — 2026-07-28T23:29:15Z
- probe before: 7/20 = 35%
- killed survivors: [K] bool_op_swap L120, [K] bool_op_swap L123
- probe after: 9/20 = 45%
- gate: narrow pytest 8 passed | ruff clean
- commit: b13651e56

## 16. hermes_cli/control_plane_gate.py — 2026-07-28T23:37:49Z
- probe before: 10/20 = 50%
- killed survivors: [K] bool_op_swap L172, [K] bool_op_swap L330, [K] bool_op_swap L448
- probe after: 13/20 = 65%
- gate: narrow pytest 34 passed | ruff clean
- commit: ad28cd6b8

## 17. hermes_cli/pressure_status.py — 2026-07-28T23:44:41Z
- probe before: 1/20 = 5%
- killed survivors: [K] bool_op_swap L52, [K] bool_op_swap L149, [K] bool_op_swap L218, [K] bool_op_swap L226, [K] bool_op_swap L232, [K] bool_op_swap L236
- probe after: 6/20 = 30%
- gate: narrow pytest 17 passed | ruff clean
- commit: 2f84a8896

## 18. hermes_cli/pa_journal.py — 2026-07-28T23:49:03Z
- probe before: 7/20 = 35%
- killed survivors: [K] bool_op_swap L187, [K] bool_op_swap L192, [K] bool_op_swap L270
- probe after: 10/20 = 50%
- gate: narrow pytest 10 passed | ruff clean
- commit: 54da8211e

## 19. hermes_cli/library_corrections.py — 2026-07-28T23:54:27Z
- probe before: 14/20 = 70%
- killed survivors: [K] bool_op_swap L263, [K] bool_op_swap L310, [K] bool_op_swap L311
- probe after: 16/20 = 80%
- gate: narrow pytest 26 passed | ruff clean
- commit: ef48c5158

## 20. hermes_cli/pa_actions.py — 2026-07-29T00:00:45Z
- probe before: 4/20 = 20%
- killed survivors: [K] bool_op_swap L277, [K] bool_op_swap L293, [K] bool_op_swap L387, [K] bool_op_swap L388
- probe after: 8/20 = 40%
- gate: narrow pytest 15 passed | ruff clean
- commit: 946862675

## 21. hermes_cli/funnel.py — 2026-07-29T00:20:16Z
- probe before: 15/20 = 75%
- killed survivors: [K] bool_op_swap L254, [K] bool_op_swap L278, [K] bool_op_swap L425
- probe after: 18/20 = 90%
- gate: narrow pytest 46 passed | ruff clean
- commit: d182893dc

## 22. hermes_cli/pa_loops.py — 2026-07-29T00:25:49Z
- probe before: 11/20 = 55%
- killed survivors: [K] bool_op_swap L50, [K] bool_op_swap L63, [K] bool_op_swap L118, [K] bool_op_swap L120
- probe after: 15/20 = 75%
- gate: narrow pytest 21 passed | ruff clean
- commit: c6123acac
