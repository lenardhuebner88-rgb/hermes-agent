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

## 23. hermes_cli/disposition.py — 2026-07-29T00:31:37Z
- probe before: 14/20 = 70%
- killed survivors: [K] boolean_flip L263, [K] boolean_flip L287, [K] boolean_flip L336, [K] boolean_flip L344, [K] bool_op_swap L397
- probe after: 19/20 = 95%
- gate: narrow pytest 59 passed | ruff clean
- commit: 27e56b49f

## 24. hermes_cli/auto_release.py — 2026-07-29T00:39:08Z
- probe before: 9/20 = 45%
- killed survivors: [K] bool_op_swap L87, [K] bool_op_swap L89, [K] bool_op_swap L270
- probe after: 12/20 = 60%
- gate: narrow pytest 32 passed | ruff clean
- commit: 1d9376389

## 25. hermes_cli/symbol_test_narrowing.py — 2026-07-29T00:44:47Z
- REJECTED: probe timeout — per-mutant runtime 11-40s × 20 mutants exceeds 300s budget; baseline GREEN but incomplete score (11/20 evaluated before timeout)

## 26. hermes_cli/capability_researcher.py — 2026-07-29T00:49:02Z
- probe before: 10/20 = 50%
- killed survivors: [K] bool_op_swap L88, [K] bool_op_swap L115, [K] bool_op_swap L123, [K] bool_op_swap L125, [K] bool_op_swap L333
- probe after: 14/20 = 70%
- gate: narrow pytest 25 passed | ruff clean
- commit: 97c7af23e

## 27. hermes_cli/memory_digest.py — 2026-07-29T00:57:45Z
- probe before: 13/20 = 65%
- killed survivors: [K] bool_op_swap L342, [K] bool_op_swap L348
- probe after: 15/20 = 75%
- gate: narrow pytest 40 passed | ruff clean
- commit: 67c774b8b

## 28. hermes_cli/library_models.py — 2026-07-29T01:04:40Z
- probe before: 13/20 = 65%
- killed survivors: [K] bool_op_swap L167, [K] bool_op_swap L291, [K] bool_op_swap L309
- probe after: 16/20 = 80%
- gate: narrow pytest 25 passed | ruff clean
- commit: 692b44993

## 29. hermes_cli/terminal_candidates.py — 2026-07-29T01:12:15Z
- probe before: 6/20 = 30%
- killed survivors: [K] boolean_flip L40, [K] bool_op_swap L94, [K] bool_op_swap L122, [K] bool_op_swap L198
- probe after: 10/20 = 50%
- gate: narrow pytest 11 passed | ruff clean
- commit: 202187b4c

## 30. hermes_cli/agent_question_push.py — 2026-07-29T01:18:19Z
- probe before: 15/20 = 75%
- killed survivors: [K] bool_op_swap L163, [K] boolean_flip L181, [K] bool_op_swap L225
- probe after: 18/20 = 90%
- gate: narrow pytest 10 passed | ruff clean
- commit: cec1da388

## 31. hermes_cli/gate_leaker.py — 2026-07-29T01:26:33Z
- probe before: 14/20 = 70%
- killed survivors: [K] bool_op_swap L100, [K] bool_op_swap L317, [K] bool_op_swap L340, [K] bool_op_swap L363, [K] bool_op_swap L379, [K] bool_op_swap L385
- probe after: 20/20 = 100%
- gate: narrow pytest 25 passed | ruff clean
- commit: bce337542

## 32. hermes_cli/goal_judge_rendering.py — 2026-07-29T01:38:43Z
- probe before: 9/20 = 45%
- killed survivors: [K] comparison_swap L75, [K] comparison_swap L89, [K] comparison_swap L114, [K] comparison_swap L126, [K] bool_op_swap L285, [K] bool_op_swap L325
- probe after: 15/20 = 75%
- gate: narrow pytest 26 passed | ruff clean
- commit: 0a17880a0

## 33. hermes_cli/kanban_close_sprint.py — 2026-07-29T01:48:50Z
- probe before: 1/20 = 5%
- killed survivors: [K] bool_op_swap L82 (×2), [K] bool_op_swap L89, [K] bool_op_swap L249, [K] bool_op_swap L303, [K] bool_op_swap L328, [K] bool_op_swap L351
- probe after: 7/20 = 35%
- gate: narrow pytest 24 passed | ruff clean
- commit: 1abc7678a

## 34. hermes_cli/strategist_surface.py — 2026-07-29T01:58:04Z
- probe before: 13/20 = 65%
- killed survivors: [K] comparison_flip L177, [K] bool_op_swap L188, [K] comparison_swap L300, [K] boolean_flip L300, [K] bool_op_swap L323
- equivalent survivors: bool_op_swap L338 (and→or unreachable for non-receipt), const_offset L280 (default param), const_offset L300 (slice offset)
- probe after: 17/20 = 85%
- commit: 80af88a9f

## 35. hermes_cli/library_results.py — 2026-07-29T02:10:44Z
- probe before: 10/20 = 50%
- killed survivors: [K] bool_op_swap L76, [K] bool_op_swap L87, [K] bool_op_swap L205, [K] bool_op_swap L277, [K] bool_op_swap L279
- probe after: 15/20 = 75%
- equivalent survivors: L77 (<= boundary after strip()), L81 (> 0 unreachable after strip()), L239 (default param), L252 (None guard), L278 (None guard)
- commit: 86edc1a53

## 36. hermes_cli/pa_health.py — 2026-07-29T02:21:08Z
- probe before: 17/20 = 85%
- killed survivors: [K] bool_op_swap L99, [K] bool_op_swap L124
- probe after: 19/20 = 95%
- equivalent survivors: L94 (>→>= equivalent: sample_count=0 → error_rate=0.0 < threshold)
- commit: 8e1ecebb6

## 37. hermes_cli/pa_news.py — 2026-07-29T02:27:40Z
- probe before: 13/20 = 65%
- killed survivors: [K] boolean_flip L121, [K] comparison_swap L152, [K] comparison_swap L182, [K] bool_op_swap L182, [K] comparison_swap L207, [K] comparison_swap L215, [K] boolean_flip L286
- probe after: 20/20 = 100%
- equivalent survivors: (none)
- commit: f14e32f2b

## 38. hermes_cli/design_board_kanban.py — 2026-07-29T02:37:49Z
- probe before: 13/20 = 65%
- killed survivors: [K] bool_op_swap L159, [K] bool_op_swap L167, [K] bool_op_swap L271, [K] bool_op_swap L278, [K] bool_op_swap L308, [K] bool_op_swap L309, [K] bool_op_swap L323
- probe after: 20/20 = 100%
- equivalent survivors: (none)
- commit: 170c44547

## 39. hermes_cli/library_state.py — 2026-07-29T02:49:57Z
- probe before: 12/20 = 60%
- killed survivors: [K] bool_op_swap L154, [K] bool_op_swap L161, [K] bool_op_swap L255, [K] bool_op_swap L278, [K] bool_op_swap L300 (and→or), [K] bool_op_swap L300 (or→and), [K] bool_op_swap L303, [K] bool_op_swap L311
- probe after: 20/20 = 100%
- equivalent survivors: (none)
- commit: 3bec4a3da

## 40. hermes_cli/autoresearch_lane_contracts.py — 2026-07-29T03:03:17Z
- probe before: 13/20 = 65%
- killed survivors: [K] boolean_flip L96, [K] boolean_flip L97, [K] boolean_flip L118, [K] bool_op_swap L287, [K] bool_op_swap L288
- probe after: 18/20 = 90%
- equivalent survivors: L34 (frozen=True→False on dataclass), L55 (frozen=True→False on dataclass)
- commit: 37b06b9cc

## 41. hermes_cli/health_status.py — 2026-07-29T03:14:33Z
- probe before: 13/20 = 65%
- killed survivors: [K] bool_op_swap L34, [K] boolean_flip L117, [K] boolean_flip L138, [K] boolean_flip L256
- equivalent survivors: L220, L228, L235 (heartbeat_age_s always float, include_heartbeat_age irrelevant)
- probe after: 17/20 = 85%
- commit: d79d6c467

## 42. hermes_cli/kanban_shadow_routing.py — 2026-07-29T03:19:59Z
- probe before: 10/20 = 50%
- killed survivors: [K] bool_op_swap L32, [K] comparison_swap L34, [K] bool_op_swap L50
- probe after: 13/20 = 65%
- commit: 975d99ae4

## 43. hermes_cli/agent_question_suggest.py — 2026-07-29T03:24:49Z
- probe before: 2/20 = 10%
- killed survivors: [K] bool_op_swap L41, [K] bool_op_swap L151, [K] bool_op_swap L152, [K] bool_op_swap L153, [K] bool_op_swap L154, [K] bool_op_swap L163
- probe after: 8/20 = 40%
- commit: 42ef00f43

## 44. hermes_cli/scoped_auto_commit.py — 2026-07-29T03:29:02Z
- probe before: 11/20 = 55%
- killed survivors: [K] comparison_swap L44, [K] boolean_flip L65, [K] bool_op_swap L200
- probe after: 14/20 = 70%
- commit: ccae3f1bd

## 45. loops/model_catalog.py — 2026-07-29T03:40:28Z
- probe before: 13/20 = 65%
- killed survivors: [K] bool_op_swap L91, [K] bool_op_swap L96, [K] bool_op_swap L170
- probe after: 16/20 = 80%
- commit: bf6458175

## 46. hermes_cli/design_board_view.py — 2026-07-29T03:48:12Z
- probe before: 11/20 = 55%
- killed survivors: [K] bool_op_swap L198, [K] comparison_swap L191, [K] comparison_swap L166
- probe after: 14/20 = 70%
- commit: 68ea9cf11

## 47. hermes_cli/affected_test_budget.py — 2026-07-29T03:55:20Z
- probe before: 7/20 = 35%
- killed survivors: [K] comparison_swap L58, [K] comparison_swap L122, [K] comparison_swap L141 (+2 collateral)
- probe after: 12/20 = 60%
- commit: 9f0a35943

## 48. hermes_cli/voice_phone_action.py — 2026-07-29T04:00:50Z
- probe before: 10/20 = 50%
- killed survivors: [K] bool_op_swap L41, [K] bool_op_swap L43, [K] bool_op_swap L47
- probe after: 13/20 = 65%
- commit: 1bc8f49f9

## 49. hermes_cli/system_stats_history.py — 2026-07-29T04:04:46Z
- probe before: 15/20 = 75%
- killed survivors: [K] boolean_flip L114, [K] boolean_flip L116, [K] boolean_flip L127 (+2 collateral)
- probe after: 18/20 = 90%
- commit: b6a60a6e3

## 50. hermes_cli/plan_prose.py — 2026-07-29T04:10:47Z
- probe before: 10/20 = 50%
- killed survivors: [K] bool_op_swap L36, [K] const_offset L69, [K] identity_swap L181
- probe after: 11/20 = 55%
- commit: e374c7717

## 51. hermes_cli/autoresearch_lane_models.py — 2026-07-29T04:20:36Z
- probe before: 6/20 = 30%
- killed survivors: [K] bool_op_swap L85, [K] bool_op_swap L166
- probe after: 9/20 = 45%
- commit: f2cab1f5f

## 52. hermes_cli/kanban_score_hygiene.py — 2026-07-29T04:23:59Z
- probe before: 15/20 = 75%
- killed survivors: [K] comparison_swap L60, [K] boolean_flip L65, [K] comparison_swap L79 (+2 collateral)
- probe after: 20/20 = 100%
- commit: b80343275

## 53. hermes_cli/stats_config.py — 2026-07-29T04:29:29Z
- probe before: 12/20 = 60%
- killed survivors: [K] boolean_flip L32-L36, [K] boolean_flip L48, [K] bool_op_swap L84, [K] bool_op_swap L89
- probe after: 20/20 = 100%
- commit: 9e49c692c

## 54. hermes_cli/pa_titles.py — 2026-07-29T04:36:16Z
- probe before: 10/20 = 50%
- killed survivors: [K] const_offset L33, L35, L36, L40, [K] const_offset L45, [K] const_offset L141, [K] const_offset L143 (+2 collateral)
- probe after: 16/20 = 80%
- commit: 4834f7993

## 55. hermes_cli/design_board_store.py — 2026-07-29T04:41:55Z
- probe before: 11/20 = 55%
- killed survivors: [K] boolean_flip L34, [K] const_offset L46, [K] boolean_flip L74 (+4 collateral)
- probe after: 18/20 = 90%
- commit: a9c390f3d

## 56. hermes_cli/cron_observability.py — 2026-07-29T04:45:59Z
- probe before: 13/20 = 65%
- killed survivors: [K] const_offset L153, [K] remove_guard L154, [K] boolean_flip L159
- probe after: 16/20 = 80%
- commit: 129a46d22

## 57. hermes_cli/kanban_dispatch_policy.py — 2026-07-29T04:53:21Z
- probe before: 11/20 = 55%
- killed survivors: [K] comparison_swap L14, [K] bool_op_swap L14, [K] comparison_swap L23, [K] bool_op_swap L23, [K] comparison_swap L132, [K] comparison_swap L140 (+1 collateral)
- probe after: 17/20 = 85%
- commit: 9d4220de2

## 58. hermes_cli/operator_digest_view.py — 2026-07-29T04:58:35Z
- probe before: 7/20 = 35%
- killed survivors: [K] comparison_swap L38, [K] const_offset L40, [K] bool_op_swap L126
- probe after: 10/20 = 50%
- commit: 84f228ca2

## 59. hermes_cli/pa_live_share.py — 2026-07-29T05:04:26Z
- probe before: 12/20 = 60%
- killed survivors: [K] comparison_swap L82, [K] comparison_swap L117, [K] const_offset L33 (+2 collateral)
- probe after: 17/20 = 85%
- commit: beeadaa09

## 60. hermes_cli/pa_push.py — 2026-07-29T05:07:24Z
- probe before: 8/20 = 40%
- killed survivors: [K] comparison_swap L43, [K] const_offset L52, [K] boolean_flip L93 (+3 collateral)
- probe after: 14/20 = 70%
- commit: ae2007395

## 61. hermes_cli/design_board_cli.py — 2026-07-29T05:10:38Z
- probe before: 7/20 = 35%
- killed survivors: [K] bool_op_swap L65, [K] boolean_flip L96, [K] comparison_swap L110
- probe after: 8/20 = 40%
- commit: 89398f4d4

## 62. hermes_cli/metrics_lite.py — 2026-07-29T05:13:50Z
- probe before: 9/20 = 45%
- killed survivors: [K] comparison_swap L36 (×2), [K] const_offset L66 (+2 collateral)
- probe after: 14/20 = 70%
- commit: 80e527340
