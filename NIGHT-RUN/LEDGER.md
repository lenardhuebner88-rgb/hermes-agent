# LEDGER — one entry per candidate module

Format is fixed by `NIGHT-RUN/RUNBOOK.md` step 7. Append only; never rewrite an
earlier entry. `started=` in the STATUS line is the timestamp of entry 1.

<!-- entries below -->

## 1. tools/voice_live_tools.py — 2026-07-28T22:29:27Z
- probe before: 17/30 = 56.7%   (source sha1 bba06a178cea)
- survivors killed: [6] bool_op_swap L576, [7] bool_op_swap L611, [8] bool_op_swap L675, [22] boolean_flip L368, [23] boolean_flip L374
- new tests: tests/hermes_cli/test_voice_live_tools.py::test_delegate_with_frame_falls_back_to_plain_delegate_without_image_callback, ::test_look_closely_partial_usage_metadata_reports_incomplete, ::test_look_closely_unavailable_without_frame_callback_even_with_key, ::test_schedule_reminder_tolerates_existing_reminders_dir_and_writes_raw_utf8
- red proof: --check 6/7/8/22/23 exit 1 before / exit 0 after
- probe after: 22/30 = 73.3%
- gate: run-affected 1 file PASS | ruff clean
- commit: f851cc5b9

STATUS ledger=1 killed_total=5 started=2026-07-28T22:29:27Z now=2026-07-28T22:29:27Z elapsed=0.0h next=scripts/run_autoresearch_request.py

## 2. scripts/run_autoresearch_request.py — 2026-07-28T22:40:15Z
- probe before: 7/30 = 23.3%   (source sha1 1ab97fb33632)
- survivors killed: [7] bool_op_swap L263, [8] bool_op_swap L339, [9] bool_op_swap L340, [10] bool_op_swap L341, [23] bool_op_swap L564
- new tests: tests/test_run_autoresearch_request.py::test_capability_finding_key_normalizes_missing_fields_to_empty_strings, ::test_apply_confirm_flag_alone_satisfies_operator_gate, ::test_finish_status_reports_stopped_by_signal_note
- red proof: --check 7/8/9/10/23 exit 1 before / exit 0 after
- probe after: 12/30 = 40.0%
- gate: run-affected 1 file PASS | ruff clean
- commit: 6cf75f60c

STATUS ledger=2 killed_total=10 started=2026-07-28T22:29:27Z now=2026-07-28T22:40:15Z elapsed=0.2h next=scripts/autoresearch_v2_nightly.py

## 3. scripts/autoresearch_v2_nightly.py — 2026-07-28T22:46:52Z
- probe before: 11/30 = 36.7%   (source sha1 e915a94c3d32)
- survivors killed: [1] bool_op_swap L111, [7] L395, [8] L396, [9] L402, [10] L403, [11] L405, [12] L407, [13] L409 (all or->and)
- new tests: tests/test_autoresearch_v2_nightly.py::test_deep_audit_lane_normalizes_sparse_result_to_safe_defaults, ::test_expected_skip_error_tolerates_none
- red proof: --check 1/7/8/9/10/11/12/13 exit 1 before / exit 0 after
- probe after: 19/30 = 63.3%
- gate: run-affected 1 file PASS | ruff clean
- commit: 33d1ea53d

STATUS ledger=3 killed_total=18 started=2026-07-28T22:29:27Z now=2026-07-28T22:46:52Z elapsed=0.3h next=scripts/dogfood_repo_cap_evidence.py

## 4. scripts/dogfood_repo_cap_evidence.py — 2026-07-28T22:50:20Z
- probe before: 8/30 = 26.7%   (source sha1 5b53c332f571)
- survivors killed: [0] bool_op_swap L91, [1] bool_op_swap L133, [20] comparison_swap L133 (<-><=), [21] comparison_swap L133 (>=->>), [7] boolean_flip L167, [22] comparison_swap L167 (is not->is)
- new tests: tests/scripts/test_dogfood_repo_cap_evidence.py::test_base_url_rejects_scheme_without_netloc, ::test_json_request_enforces_2xx_status_window, ::test_authenticate_requires_literal_ok_true
- red proof: --check 0/1/7/20/21/22 exit 1 before / exit 0 after
- probe after: 15/30 = 50.0%  (+7 vs +6 verified — one unverified co-kill in the same status path)
- gate: run-affected 1 file PASS | ruff clean
- commit: 45271eea7

STATUS ledger=4 killed_total=25 started=2026-07-28T22:29:27Z now=2026-07-28T22:50:20Z elapsed=0.3h next=gateway/kanban_alerts.py

## 5. gateway/kanban_alerts.py — 2026-07-28T23:02:53Z
- probe before: 24/30 = 80.0%   (source sha1 9e7a700b0276)
- survivors killed: [5] bool_op_swap L152, [6] bool_op_swap L152, [19] bool_op_swap L376, [20] bool_op_swap L377, [21] bool_op_swap L378, [26] bool_op_swap L463 (and->or)
- new tests: tests/gateway/test_kanban_alerts.py::test_load_alerts_config_normalizes_blank_thread_id_to_none, ::test_operator_escalation_falls_back_to_task_row_when_payload_sparse, ::test_auto_release_rolled_back_without_rollback_ok_omits_detached_warning
- red proof: --check 5/6/19/20/21/26 exit 1 before / exit 0 after
- probe after: 30/30 = 100.0% — module fully pinned
- gate: run-affected 1 file PASS | ruff clean
- commit: 2d2592a03

STATUS ledger=5 killed_total=31 started=2026-07-28T22:29:27Z now=2026-07-28T23:02:53Z elapsed=0.6h next=hermes_cli/subcommands/vision.py

## 6. hermes_cli/subcommands/vision.py — 2026-07-28T23:07:00Z
- probe before: 3/30 = 10.0%   (source sha1 34b61c878a37)
- survivors killed: [0] bool_op_swap L423, [1] bool_op_swap L440, [2] bool_op_swap L452, [11] boolean_flip L56, [13] boolean_flip L391
- new tests: tests/hermes_cli/subcommands/test_vision.py::test_cli_strategist_mode_is_required, ::test_cli_strategist_reflect_json_emits_raw_utf8, ::test_cli_strategist_reflect_names_suppressed_levers_none, ::test_cli_strategist_harvest_watch_triggered_without_harvest_block, ::test_cli_gate_fix_check_triggered_without_ingested_block
- red proof: --check 0/1/2/11/13 exit 1 before / exit 0 after
- probe after: 8/30 = 26.7%
- note: [12] boolean_flip L390 is an equivalent mutant (getattr default unreachable — args.json always present via store_true)
- gate: run-affected 1 file PASS | ruff clean
- commit: 7e6f7fbff

STATUS ledger=6 killed_total=36 started=2026-07-28T22:29:27Z now=2026-07-28T23:07:00Z elapsed=0.6h next=scripts/refactor/split_module.py

## 7. scripts/refactor/split_module.py — 2026-07-28T23:10:11Z
- probe before: 10/30 = 33.3%   (source sha1 f07d4ae45952)
- survivors killed: [0] bool_op_swap L118 (and->or), [3] bool_op_swap L155 (or->and), [20] comparison_swap L38 (<=-><), [22] comparison_swap L89 (->->>=), [26] comparison_swap L206 (!=->==)
- new tests: tests/refactor/test_split_module.py::test_docstring_span_is_none_for_empty_module, ::test_locally_bound_anywhere_counts_plain_import_names, ::test_section_owner_counts_banner_on_symbol_line_as_that_section, ::test_analyze_oversized_boundary_is_strictly_above_4000, ::test_import_name_for_path_returns_dotted_relative
- red proof: --check 0/3/20/22/26 exit 1 before / exit 0 after
- probe after: 21/30 = 70.0%
- gate: run-affected 1 file PASS | ruff clean
- commit: 3b2baa58f

STATUS ledger=7 killed_total=47 started=2026-07-28T22:29:27Z now=2026-07-28T23:10:11Z elapsed=0.7h next=scripts/langfuse_dashboards.py

## 8. scripts/langfuse_dashboards.py — 2026-07-28T23:13:13Z
- probe before: 3/30 = 10.0%   (source sha1 ac4fe9b76d83)
- survivors killed: [0] bool_op_swap L130 (or->and), [24] boolean_flip L36, [25] boolean_flip L51, [26] boolean_flip L64, [27] boolean_flip L72 (all frozen True->False)
- new tests: tests/scripts/test_langfuse_dashboards.py::test_input_dataclasses_are_frozen, ::test_load_golden_fixture_rejects_wrong_version_even_with_valid_source
- red proof: --check 0/24/25/26/27 exit 1 before / exit 0 after
- probe after: 8/30 = 26.7%
- gate: run-affected 1 file PASS | ruff clean
- commit: a52fdb109

STATUS ledger=8 killed_total=52 started=2026-07-28T22:29:27Z now=2026-07-28T23:13:13Z elapsed=0.7h next=scripts/autoresearch_writer.py

## 9. scripts/autoresearch_writer.py — 2026-07-28T23:16:21Z
- probe before: 13/30 = 43.3%   (source sha1 c6b19d4f75f0)
- survivors killed: [0] bool_op_swap L87 (or->and), [7] bool_op_swap L137, [8] bool_op_swap L137, [14] bool_op_swap L146 (and->or), [15] bool_op_swap L148 (and->or)
- new tests: tests/test_autoresearch_writer.py::test_parse_fix_reply_uses_reason_field_and_normalises_absence_to_none, ::test_parse_fix_reply_replacement_not_in_skill_is_rejected, ::test_parse_fix_reply_preserves_a_missing_trailing_newline, ::test_configured_aux_model_none_model_is_empty_not_literal_none
- red proof: --check 0/7/8/14/15 exit 1 before / exit 0 after
- probe after: 18/30 = 60.0%
- gate: run-affected 1 file PASS | ruff clean
- commit: 5aa8b8120

STATUS ledger=9 killed_total=57 started=2026-07-28T22:29:27Z now=2026-07-28T23:16:21Z elapsed=0.8h next=scripts/daily_research_post.py

## 10. scripts/daily_research_post.py — 2026-07-28T23:18:37Z
- probe before: 5/30 = 16.7%   (source sha1 520e48ac8021)
- survivors killed: [0] bool_op_swap L110, [1] bool_op_swap L117, [2] bool_op_swap L135 (and->or), [3] bool_op_swap L145, [28] boolean_flip L38
- new tests: tests/scripts/test_daily_research_post.py::test_source_config_is_frozen, ::test_clean_text_and_parse_datetime_tolerate_none, ::test_xml_text_skips_missing_tags_and_uses_the_first_present, ::test_xml_link_falls_back_to_element_text_without_href
- red proof: --check 0/1/2/3/28 exit 1 before / exit 0 after
- probe after: 10/30 = 33.3%
- gate: run-affected 1 file PASS | ruff clean
- commit: ba02b5915

STATUS ledger=10 killed_total=62 started=2026-07-28T22:29:27Z now=2026-07-28T23:18:37Z elapsed=0.8h next=scripts/refactor/fork_loss_check.py

## 11. scripts/refactor/fork_loss_check.py — 2026-07-28T23:21:56Z
- probe before: 15/30 = 50.0%   (source sha1 ff84748b15c9)
- survivors killed: [0] bool_op_swap L166 (and->or), [1] bool_op_swap L190 (or->and), [2] bool_op_swap L191 (and->or), [15] boolean_flip L149 (False->True), [20] boolean_flip L193 (True->False)
- new tests: tests/refactor/test_fork_loss_check.py::test_parse_diff_ignores_files_outside_the_tracked_set, ::test_parse_diff_needs_a_file_header_before_added_lines, ::test_is_noise_flags_imports_but_not_from_prose
- red proof: --check 0/1/2/15/20 exit 1 before / exit 0 after
- probe after: 20/30 = 66.7%
- gate: run-affected 1 file PASS | ruff clean
- commit: 3b16cdeb8

STATUS ledger=11 killed_total=67 started=2026-07-28T22:29:27Z now=2026-07-28T23:21:56Z elapsed=0.9h next=tools/verification_gate_tool.py

## 12. tools/verification_gate_tool.py — 2026-07-28T23:25:38Z
- probe before: 13/30 = 43.3%   (source sha1 e1cf60e6744f)
- survivors killed: [0] bool_op_swap L71 (or->and), [1] bool_op_swap L73 (or->and), [2] bool_op_swap L87 (and->or), [24] boolean_flip L64, [25] boolean_flip L75 (True->False)
- new tests: tests/tools/test_verification_gate_tool.py::test_safe_workspace_rejects_plain_dir_with_valueerror, ::test_safe_workspace_rejects_garbage_gitfile, ::test_artifact_dir_requires_both_kanban_ids_to_be_safe, ::test_capabilities_advertise_record_only_default
- red proof: --check 0/1/2/24/25 exit 1 before / exit 0 after
- probe after: 18/30 = 60.0%
- gate: run-affected 1 file PASS | ruff clean
- commit: 36dd052d7

STATUS ledger=12 killed_total=72 started=2026-07-28T22:29:27Z now=2026-07-28T23:25:38Z elapsed=1.0h next=scripts/render_autoresearch_dashboard.py

## 13. scripts/render_autoresearch_dashboard.py — 2026-07-28T23:29:34Z
- probe before: 5/30 = 16.7%   (source sha1 ed8c90193f42)
- survivors killed: [0] bool_op_swap L55, [2] bool_op_swap L91 (and->or), [6] bool_op_swap L115, [16] comparison_swap L58 (!=->==), [17] comparison_swap L78 (==->!=), [20]-[25] comparison_swap L124-134 (<-><=, boundary cluster)
- new tests: tests/test_render_autoresearch_dashboard.py::test_parse_rubric_keeps_only_ten_column_data_rows, ::test_parse_results_reads_a_nonempty_tsv, ::test_extract_inventory_routes_bullets_after_heading_to_the_skill, ::test_area_from_path_recognises_firecrawl_hyphen_paths, ::test_weakness_keys_treats_score_three_as_healthy
- red proof: --check 0/2/6/16/17/20 exit 1 before / exit 0 after (21-25 co-killed by the boundary test)
- probe after: 17/30 = 56.7%
- gate: run-affected 1 file PASS | ruff clean
- commit: 03d5ec80e

STATUS ledger=13 killed_total=84 started=2026-07-28T22:29:27Z now=2026-07-28T23:29:34Z elapsed=1.0h next=scripts/check_skill_hygiene.py

## 14. scripts/check_skill_hygiene.py — 2026-07-28T23:33:16Z
- probe before: 15/30 = 50.0%   (source sha1 f37bf7c72152)
- survivors killed: [7] boolean_flip L96, [8] boolean_flip L138, [10] comparison_swap L162 (->->>=), [15] const_offset L157 (0->1), [16] const_offset L157 (1->2), [17] const_offset L160 (120->121), [18] const_offset L163 (3->4)
- new tests: tests/scripts/test_check_skill_hygiene.py::test_finding_and_path_exception_are_frozen, ::test_line_of_counts_lines_from_offset_zero, ::test_snippet_truncates_to_exactly_the_limit
- red proof: --check 7/8/10/15/16/17/18 exit 1 before / exit 0 after
- probe after: 22/30 = 73.3%
- gate: run-affected 1 file PASS | ruff clean
- commit: 610839159

STATUS ledger=14 killed_total=91 started=2026-07-28T22:29:27Z now=2026-07-28T23:33:16Z elapsed=1.1h next=plugins/observability/board_facts/auxiliary_wrapper.py

## 15. plugins/observability/board_facts/auxiliary_wrapper.py — 2026-07-28T23:38:15Z
- probe before: 12/30 = 40.0%   (source sha1 43ed27a9347f)
- survivors killed: [1] bool_op_swap L161 (and->or), [2] bool_op_swap L162 (and->or), [3] bool_op_swap L170 (or->and), [4] bool_op_swap L174 (or->and), [6] boolean_flip L27, [7] boolean_flip L34, [23] comparison_swap L62 (<=-><), [25] comparison_swap L161 (is not->is)
- new tests: tests/plugins/observability/board_facts/test_auxiliary_wrapper.py::test_observer_and_installation_dataclasses_are_frozen, ::test_aux_task_label_boundary_length_stays_verbatim, ::test_has_usage_field_requires_a_present_non_none_value, ::test_normalized_usage_dispatches_on_which_token_fields_exist
- red proof: --check 1/2/3/4/6/7/23/25 exit 1 before / exit 0 after
- probe after: 20/30 = 66.7%
- gate: run-affected 1 file PASS | ruff clean
- commit: 16ee5e5b2

STATUS ledger=15 killed_total=99 started=2026-07-28T22:29:27Z now=2026-07-28T23:38:15Z elapsed=1.1h next=scripts/scan_kanban_block_notifications.py

## 16. scripts/scan_kanban_block_notifications.py — 2026-07-28T23:42:29Z
- probe before: 15/30 = 50.0%   (source sha1 85890b69cadd)
- survivors killed: [2] bool_op_swap L82 (or->and), [3] bool_op_swap L84 (or->and), [13] bool_op_swap L223 (or->and), [24] boolean_flip L64, [27] boolean_flip L385, [28] boolean_flip L394, [29] boolean_flip L394
- new tests: tests/test_scan_kanban_block_notifications.py::test_same_candidate_needs_two_bounded_non_empty_spells, ::test_copy_readonly_snapshot_copies_the_given_database, ::test_block_without_payload_kind_counts_as_unclassified, ::test_main_requires_the_output_flag, ::test_main_creates_nested_output_dir_and_tolerates_existing_one
- red proof: --check 2/3/13/24/27/28/29 exit 1 before / exit 0 after
- probe after: 23/30 = 76.7%
- gate: NARROW pytest 1 file PASS (run-affected branch-age preflight fails on this worktree branch; merge/rebase forbidden by runbook rule 3) | ruff clean
- commit: 8ea0d1d5f

STATUS ledger=16 killed_total=107 started=2026-07-28T22:29:27Z now=2026-07-28T23:42:29Z elapsed=1.2h next=scripts/gate_load_stamp.py

## 17. scripts/gate_load_stamp.py — 2026-07-28T23:47:09Z
- probe before: 14/30 = 46.7%   (source sha1 e4189c72d93b)
- survivors killed: [13] comparison_swap L47 (<-><=), [14] comparison_swap L76 (is not->is), [17] comparison_swap L119 (->->>=), [19] comparison_swap L127 (>=->>), [20] comparison_swap L129 (>=->>), [25] comparison_swap L162 (->->>=)
- new tests: tests/scripts/test_gate_load_stamp.py::test_filter_duration_map_keeps_zero_second_entries, ::test_predict_serial_empty_selection_is_unknown_without_cache, ::test_workers_default_never_returns_zero, ::test_fmt_seconds_boundary_formats, ::test_zero_serial_prediction_does_not_divide
- red proof: --check 13/14/17/19/20/25 exit 1 before / exit 0 after
- probe after: 22/30 = 73.3%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 11a64b4f0

STATUS ledger=17 killed_total=113 started=2026-07-28T22:29:27Z now=2026-07-28T23:47:09Z elapsed=1.3h next=gateway/profile_policy.py

## 18. gateway/profile_policy.py — 2026-07-28T23:50:52Z
- probe before: 21/30 = 70.0%   (source sha1 39b036f08915)
- survivors killed: [5] boolean_flip L170, [7] boolean_flip L194, [11] boolean_flip L211, [14] boolean_flip L231, [16] boolean_flip L237, [27] const_offset L63 (60->61), [28] const_offset L66 (120->121); [2] L193 co-killed
- new tests: tests/gateway/test_profile_policy.py::test_is_under_answers_true_for_a_child_path, ::test_hub_detection_fails_closed_on_missing_or_mismatched_paths, ::test_minimax_entry_filter_needs_a_dict_and_a_model_signal, ::test_heartbeat_age_defaults_match_discord_beat_math
- red proof: --check 5/7/11/14/16/27/28 exit 1 before / exit 0 after
- probe after: 29/30 = 96.7%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 33b7e8573

STATUS ledger=18 killed_total=121 started=2026-07-28T22:29:27Z now=2026-07-28T23:50:52Z elapsed=1.4h next=tools/kanban_workspace_runner.py

## 19. tools/kanban_workspace_runner.py — 2026-07-28T23:57:36Z
- probe before: 24/30 = 80.0%   (source sha1 44917c464ed1)
- survivors killed: [0] bool_op_swap L80 (or->and), [6] bool_op_swap L146 (or->and), [7] bool_op_swap L188 (or->and), [20] boolean_flip L314 (False->True), [22] comparison_swap L96 (<=-><)
- new tests: tests/tools/test_kanban_workspace_runner.py::test_runner_rejects_workspace_path_that_is_a_file, ::test_validate_workspace_path_rejects_a_directory_target, ::test_runner_rejects_unknown_flag_like_target_as_flag, ::test_truncate_boundary_keeps_exact_limit_data, ::test_runner_schema_forbids_additional_properties
- red proof: --check 0/6/7/20/22 exit 1 before / exit 0 after
- probe after: 29/30 = 96.7%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 8bc58c0d8

STATUS ledger=19 killed_total=126 started=2026-07-28T22:29:27Z now=2026-07-28T23:57:36Z elapsed=1.5h next=scripts/browser_reap.py

## 20. scripts/browser_reap.py — 2026-07-29T00:04:09Z
- probe before: 11/30 = 36.7%   (source sha1 239affc7c01a)
- survivors killed: [4] boolean_flip L73, [8] boolean_flip L199, [9] boolean_flip L212, [14] comparison_swap L125 (<=-><), [25] const_offset L166 (197->198), [26] const_offset L168 (3600->3601)
- new tests: tests/scripts/test_browser_reap.py::test_procinfo_is_frozen, ::test_orphaned_ppid_exactly_one_is_orphaned_even_with_live_init, ::test_apply_sigterm_journal_line_is_kill_not_would_kill, ::test_apply_sigkill_journal_line_is_kill_not_would_kill, ::test_format_journal_line_bounds_cmd_and_uses_real_hour_math
- red proof: --check 4/8/9/14/25/26 exit 1 before / exit 0 after
- probe after: 16/30 = 53.3%  (re-measure shows +5 not +6 — one co-kill netted out; strictly above baseline)
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 2a788cc48

STATUS ledger=20 killed_total=131 started=2026-07-28T22:29:27Z now=2026-07-29T00:04:09Z elapsed=1.6h next=scripts/kanban_dispatcher_watchdog.py

## 21. scripts/kanban_dispatcher_watchdog.py — 2026-07-29T00:07:23Z
- probe before: 20/30 = 66.7%   (source sha1 bef92c7e76da)
- survivors killed: [15] comparison_swap L184 (<=-><), [16] comparison_swap L194 (->->>=), [19] comparison_swap L208 (==->!=), [20] comparison_swap L210 (==->!=), [27] const_offset L99 (1->2), [28] const_offset L99 (1->2), [29] const_offset L113 (2000->2001)
- new tests: tests/scripts/test_kanban_dispatcher_watchdog.py::test_evaluate_zero_last_tick_is_invalid_not_stale, ::test_evaluate_age_exactly_at_threshold_is_still_healthy, ::test_format_alert_names_missing_and_invalid_reasons_distinctly, ::test_read_token_preserves_equals_signs_inside_the_token, ::test_post_discord_caps_body_at_exactly_2000_chars
- red proof: --check 15/16/19/20/27/28/29 exit 1 before / exit 0 after
- probe after: 27/30 = 90.0%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: eaaa1789f

STATUS ledger=21 killed_total=138 started=2026-07-28T22:29:27Z now=2026-07-29T00:07:23Z elapsed=1.6h next=scripts/check_kanban_lifecycle_anchors.py

## 22. REJECTED scripts/check_kanban_lifecycle_anchors.py — 2026-07-29T00:09:00Z
- baseline is FAIL: test_committed_lifecycle_map_resolves_against_real_source is red in this worktree (committed docs/kanban/LIFECYCLE.md anchors drift against this branch's kanban_db.py line numbers, e.g. INTEGRATION_FALLBACK_MAX_TEST_FILES L22->L31). Mutation numbers meaningless against a red baseline; fixing would mean touching the lifecycle doc or the live-owned module, both out of scope here.

STATUS ledger=22 killed_total=138 started=2026-07-28T22:29:27Z now=2026-07-29T00:09:00Z elapsed=1.7h next=agent/tool_result_eliding.py

## 23. agent/tool_result_eliding.py — 2026-07-29T00:12:31Z
- probe before: 13/30 = 43.3%   (source sha1 3ca583c5324a)
- survivors killed: [2] bool_op_swap L192 (and->or), [9] comparison_swap L128 (>=->>), [15] comparison_swap L192 (->->>=), [17] comparison_swap L282 (<=-><), [19] const_offset L91 (1500->1501), [20] const_offset L102 (8->9)
- new tests: tests/agent/test_tool_result_eliding.py::test_int_env_accepts_zero_as_a_valid_value, ::test_cache_stable_boundary_negative_step_is_plain_boundary, ::test_tool_result_at_boundary_index_stays_protected, ::test_content_at_exactly_min_elide_chars_stays, ::test_module_defaults_are_pinned
- red proof: --check 2/9/15/17/19/20 exit 1 before / exit 0 after
- probe after: 20/30 = 66.7%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 23b1f5cee

STATUS ledger=23 killed_total=145 started=2026-07-28T22:29:27Z now=2026-07-29T00:12:31Z elapsed=1.7h next=scripts/backfill_session_labels.py

## 24. scripts/backfill_session_labels.py — 2026-07-29T00:16:48Z
- probe before: 8/30 = 26.7%   (source sha1 fb8c21288b9c)
- survivors killed: [9] comparison_swap L106 (>=->>), [11] comparison_swap L147 (>=->>), [22] const_offset L141 (20->21), [23] const_offset L163 (2->3)
- new tests: tests/test_backfill_session_labels.py::test_iter_candidates_limit_zero_returns_nothing, ::test_format_examples_caps_at_exactly_max_examples, ::test_backup_state_db_tolerates_existing_backups_dir, ::test_run_missing_state_db_returns_exit_code_two
- red proof: --check 9/11/22/23 exit 1 before / exit 0 after
- probe after: 13/30 = 43.3%
- note: [2] boolean_flip L60 is effectively equivalent (parents flag unobservable for the single-level backups/ mkdir)
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 30064b092

STATUS ledger=24 killed_total=149 started=2026-07-28T22:29:27Z now=2026-07-29T00:16:48Z elapsed=1.8h next=scripts/autoresearch_request.py

## 25. scripts/autoresearch_request.py — 2026-07-29T00:21:13Z
- probe before: 17/30 = 56.7%   (source sha1 89f263561606)
- survivors killed: [6] boolean_flip L54, [11] boolean_flip L152, [12] boolean_flip L153, [16] boolean_flip L228, [17] boolean_flip L231, [18] boolean_flip L232, [23] comparison_swap L119 (<=-><)
- new tests: tests/test_autoresearch_request.py::test_build_request_pins_backup_and_eval_requirements, ::test_build_request_accepts_min_iteration_boundary, ::test_validate_allowed_paths_accepts_subpath_under_area_root, ::test_cli_requires_command_area_and_focus
- red proof: --check 6/11/12/16/17/18/23 exit 1 before / exit 0 after
- probe after: 25/30 = 83.3%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 8e1d8f9a5

STATUS ledger=25 killed_total=157 started=2026-07-28T22:29:27Z now=2026-07-29T00:21:13Z elapsed=1.9h next=scripts/smoke_health_status_auth.py

## 26. scripts/smoke_health_status_auth.py — 2026-07-29T00:23:53Z
- probe before: 4/30 = 13.3%   (source sha1 c6e08655e136)
- survivors killed: [9] comparison_swap L42 (is not->is), [10] comparison_swap L57 ( <-><=), [11] comparison_swap L57 (>=->>), [13] comparison_swap L121 (!=->==), [14] comparison_swap L136 (==->!=), [18] const_offset L57 (200->201), [19] const_offset L57 (300->301), [27] negate_if L40
- new tests: tests/scripts/test_smoke_health_status_auth.py::test_json_request_accepts_exactly_the_2xx_window, ::test_json_request_serializes_payload_and_skips_absent_extra_headers, ::test_validate_health_payload_pins_schema_and_subsystems, ::test_summary_annotates_only_the_dispatcher_heartbeat
- red proof: --check 9/10/11/13/14/18/19/27 exit 1 before / exit 0 after
- probe after: 17/30 = 56.7%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 7822892b8

STATUS ledger=26 killed_total=170 started=2026-07-28T22:29:27Z now=2026-07-29T00:23:53Z elapsed=1.9h next=scripts/sync_model_prices.py

## 27. scripts/sync_model_prices.py — 2026-07-29T00:26:44Z
- probe before: 13/30 = 43.3%   (source sha1 4115a1c16b0e)
- survivors killed: [0] bool_op_swap L68 (or->and), [4] bool_op_swap L88 (or->and), [5] bool_op_swap L124 (or->and), [6] boolean_flip L111, [13] comparison_swap L68 ( <-><=), [15] comparison_swap L86 (is not->is), [21] const_offset L68 (100->101), [29] negate_if L68
- new tests: tests/scripts/test_sync_model_prices.py::test_download_feed_requires_a_complete_map, ::test_select_models_defaults_mode_to_chat_and_requires_a_price, ::test_build_payload_uses_now_when_fetched_at_is_absent, ::test_pricing_version_is_the_sorted_key_hash
- red proof: --check 0/4/5/6/13/15/21/29 exit 1 before / exit 0 after
- probe after: 16/30 = 53.3%  (probe samples 30 from a larger mutant pool; PYTHONHASHSEED changes the sample between runs, so the delta reads +3 although all 8 verified kills landed — all 8 appear KILLED in the per-mutant listing)
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 359b19918

STATUS ledger=27 killed_total=173 started=2026-07-28T22:29:27Z now=2026-07-29T00:26:44Z elapsed=2.0h next=scripts/watermark_check.py

## 28. scripts/watermark_check.py — 2026-07-29T00:30:14Z
- probe before: 16/30 = 53.3%   (source sha1 c2c8b1ea6b83)
- survivors killed: [4] boolean_flip L50, [5] boolean_flip L60, [11] boolean_flip L148 (False->True), [26] comparison_swap L172 (<=-><), [28] const_offset L40 (1024->1025), [29] const_offset L40 (3->4)
- new tests: tests/scripts/test_watermark_check.py::test_dataclasses_are_frozen, ::test_gib_is_binary_gibibyte, ::test_proc_capped_fails_closed_on_unreadable_cgroup, ::test_collect_metrics_carries_only_procs_strictly_over_threshold
- red proof: --check 4/5/11/26/28/29 exit 1 before / exit 0 after
- probe after: 22/30 = 73.3%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 0cc8df90a

STATUS ledger=28 killed_total=179 started=2026-07-28T22:29:27Z now=2026-07-29T00:30:14Z elapsed=2.0h next=scripts/cleanup_retry_heavy_scores.py

## 29. scripts/cleanup_retry_heavy_scores.py — 2026-07-29T00:34:21Z
- probe before: 22/30 = 73.3%   (source sha1 29f2d940f80d)
- survivors killed: [1] boolean_flip L189, [2] boolean_flip L190, [3] boolean_flip L212, [18] const_offset L203 (2->3), [19] const_offset L211 (1->2), [20] const_offset L213 (0->1)
- new tests: tests/scripts/test_cleanup_retry_heavy_scores.py::test_cli_requires_db_and_backup_flags, ::test_cli_exit_codes_distinguish_user_error_from_runtime_failure, ::test_cli_apply_prints_sorted_json_report_and_returns_zero
- red proof: --check 1/2/3/18/19/20 exit 1 before / exit 0 after
- probe after: 25/30 = 83.3%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: abbef6e11

STATUS ledger=29 killed_total=182 started=2026-07-28T22:29:27Z now=2026-07-29T00:34:21Z elapsed=2.1h next=scripts/storage_guard.py

## 30. scripts/storage_guard.py — 2026-07-29T00:37:34Z
- probe before: 12/30 = 40.0%   (source sha1 5c601b155412)
- survivors killed: [4] boolean_flip L40, [5] boolean_flip L48, [6] comparison_swap L67 ( <-><=), [8] comparison_swap L82 (>=->>), [12] comparison_swap L149 (>=->>), [13] comparison_swap L157 (>=->>), [14] comparison_swap L174 (!=->==)
- new tests: tests/scripts/test_storage_guard.py::test_usage_and_finding_are_frozen, ::test_fstab_line_with_exactly_four_fields_is_parsed, ::test_mountinfo_line_with_exactly_five_fields_counts, ::test_thresholds_are_inclusive_at_warn_and_alarm, ::test_render_lists_alarms_before_warnings
- red proof: --check 4/5/6/8/12/13/14 exit 1 before / exit 0 after
- probe after: 19/30 = 63.3%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: a2ae9c044

STATUS ledger=30 killed_total=189 started=2026-07-28T22:29:27Z now=2026-07-29T00:37:34Z elapsed=2.1h next=scripts/reap_stale_sessions.py

## 31. scripts/reap_stale_sessions.py — 2026-07-29T00:43:54Z
- probe before: 9/30 = 30.0%   (source sha1 39a27f3cad4b)
- survivors killed: [1] bool_op_swap L74 (or->and), [3] bool_op_swap L195 (and->or), [14] comparison_swap L138 ( <-><=), [16] comparison_swap L195 (->->>=), [17] comparison_swap L197 (==->!=), [23] const_offset L138 (0->1)
- new tests: tests/test_reap_stale_sessions.py::test_label_prefers_title_then_display_name, ::test_safe_copy_db_produces_a_complete_backup, ::test_backup_path_tolerates_existing_backups_dir, ::test_days_zero_is_allowed, TestNotifyGating::test_notify_fires_only_on_apply_with_candidates_and_int_days
- red proof: --check 1/3/14/16/17/23 exit 1 before / exit 0 after
- probe after: 16/30 = 53.3%
- notes: [4] uri-flag flip equivalent (host SQLite is URI-by-default); [8] L66 parents flag unobservable — both behaviours still pinned by new tests
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 6d8f49b5b

STATUS ledger=31 killed_total=196 started=2026-07-28T22:29:27Z now=2026-07-29T00:43:54Z elapsed=2.2h next=scripts/control_shot.py

## 32. scripts/control_shot.py — 2026-07-29T00:46:46Z
- probe before: 6/30 = 20.0%   (source sha1 bfa554c17777)
- survivors killed: [0] bool_op_swap L63 (or->and), [1] bool_op_swap L73 (and->or), [2] bool_op_swap L76 (or->and), [3] bool_op_swap L77 (or->and), [4] bool_op_swap L78 (or->and), [5] bool_op_swap L95 (or->and)
- new tests: tests/scripts/test_control_shot.py::test_load_env_file_skips_comments_and_garbage_lines, ::test_credentials_prefers_process_env_and_fails_closed_on_partial, ::test_credentials_falls_back_to_env_file_per_variable, ::test_resolve_url_passes_absolute_routes_through
- red proof: --check 0/1/2/3/4/5 exit 1 before / exit 0 after
- probe after: 16/30 = 53.3%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: d710f40cf

STATUS ledger=32 killed_total=206 started=2026-07-28T22:29:27Z now=2026-07-29T00:46:46Z elapsed=2.3h next=scripts/refactor/layering.py

## 33. scripts/refactor/layering.py — 2026-07-29T00:49:11Z
- probe before: 26/30 = 86.7%   (source sha1 75062e5d31e9)
- survivors killed: [7] comparison_swap L43 (>=->>), [16] comparison_swap L100 (is not->is)
- new tests: tests/refactor/test_layering.py::test_banner_sections_banner_on_last_line_is_ignored, ::test_import_time_names_includes_annotated_assignment_value
- red proof: --check 7/16 exit 1 before / exit 0 after
- probe after: 27/30 = 90.0%
- note: [19]/[21] (<= flips) equivalent — same-owner skip makes equal ranks unreachable at the comparison
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: cc41d48e6

STATUS ledger=33 killed_total=209 started=2026-07-28T22:29:27Z now=2026-07-29T00:49:11Z elapsed=2.3h next=scripts/voice_spar_smoke.py

## 34. scripts/voice_spar_smoke.py — 2026-07-29T00:51:30Z
- no gap: probe 30/30 = 100.0% on the first measure (source sha1 5d52474a62d6) — module already fully pinned, no commit.

STATUS ledger=34 killed_total=209 started=2026-07-28T22:29:27Z now=2026-07-29T00:51:30Z elapsed=2.4h next=scripts/affected_tests.py

## 35. scripts/affected_tests.py — 2026-07-29T01:23:05Z
- probe before: 5/30 = 16.7%   (source sha1 d709d32b4c92)
- survivors killed: [3] boolean_flip L132, [4] comparison_swap L54 (!=->==), [5] comparison_swap L131 (==->!=), [9] const_offset L54 (0->1), [11] const_offset L129 (2->3)
- new tests: tests/scripts/test_affected_tests.py::test_repo_root_resolves_inside_worktree_and_fails_outside, ::test_main_returns_2_on_mapping_error, ::test_main_json_output_is_sorted_json_when_requested
- red proof: --check 3/4/5/9/11 exit 1 before / exit 0 after
- probe after: 21/30 = 70.0%
- note: slow module (~22s per mutant run); re-measure took ~30 min
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 314c34905

STATUS ledger=35 killed_total=225 started=2026-07-28T22:29:27Z now=2026-07-29T01:23:05Z elapsed=2.9h next=plugins/kanban/dashboard/scorecard_routes.py

## 36. plugins/kanban/dashboard/scorecard_routes.py — 2026-07-29T01:30:02Z
- probe before: 17/30 = 56.7%   (source sha1 844dbfd1cfec)
- survivors killed: [2] comparison_swap L51 (==->!=), [9] comparison_swap L134 (==->!=), [11]-[21] const_offset L25-L35 (every outcome code N.0 -> N+1.0)
- new tests: tests/plugins/test_scorecard_routes.py::test_scorecard_labels_every_legacy_numeric_outcome_code, ::test_scorecard_approval_rate_is_asymmetric_not_mirror
- red proof: --check 2/9/11/12/15/16/19/21 exit 1 before / exit 0 after (13/16/17/18/20 co-killed by the full-table test)
- probe after: 30/30 = 100.0% — module fully pinned; BAND A COMPLETE
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: f3396d257

STATUS ledger=36 killed_total=238 started=2026-07-28T22:29:27Z now=2026-07-29T01:30:02Z elapsed=3.0h next=agent/pricing_feed.py

## 37. agent/pricing_feed.py — 2026-07-29T01:33:22Z
- probe before: 19/30 = 63.3%   (source sha1 396a51e94270)
- survivors killed: [1] bool_op_swap L93 (or->and), [2] bool_op_swap L99 (or->and), [3] bool_op_swap L101 (or->and), [6] bool_op_swap L121 (and->or), [7] bool_op_swap L141 (or->and), [13] comparison_swap L121 (is->is not), [14] comparison_swap L121 (is->is not)
- new tests: tests/agent/test_pricing_feed.py::test_feed_requires_both_meta_and_models_objects, ::test_feed_requires_non_empty_version_and_source_url, ::test_feed_model_source_falls_back_to_feed_source_url, ::test_feed_keeps_models_with_only_one_rate
- red proof: --check 1/2/3/6/7/13/14 exit 1 before / exit 0 after
- probe after: 26/30 = 86.7%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: a38dadad1

STATUS ledger=37 killed_total=245 started=2026-07-28T22:29:27Z now=2026-07-29T01:33:22Z elapsed=3.1h next=scripts/refactor/api_snapshot.py

## 38. scripts/refactor/api_snapshot.py — 2026-07-29T01:38:19Z
- probe before: 17/30 = 56.7%   (source sha1 f8428c608318)
- survivors killed: [2] bool_op_swap L60 (and->or), [3] boolean_flip L47, [4] boolean_flip L97, [11] const_offset L100 (2->3), [12] const_offset L110 (1->2), [13] const_offset L112 (0->1), [14] negate_if L29, [17] negate_if L55, [23] negate_if L98, [24] negate_if L102, [25] negate_if L106
- new tests: tests/refactor/test_api_snapshot.py::test_snapshot_default_reads_cached_module_and_describes_classes, ::test_snapshot_fresh_purges_and_records_private_non_dunder_names, ::test_main_writes_sorted_indented_json_of_a_fresh_snapshot, ::test_main_compare_exit_codes, ::test_main_without_out_or_compare_just_snapshots
- red proof: --check 2/3/4/11/12/13/14/17/23/24/25 exit 1 before / exit 0 after
- probe after: 28/30 = 93.3%
- note: [5] (sort_keys flip) equivalent — snapshot dicts are built in sorted key order
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 1f4f16a0f

STATUS ledger=38 killed_total=256 started=2026-07-28T22:29:27Z now=2026-07-29T01:38:19Z elapsed=3.1h next=scripts/voice_reminder_fire.py

## 39. scripts/voice_reminder_fire.py — 2026-07-29T01:43:14Z
- probe before: 13/29 = 44.8%   (source sha1 890c07c0c7a1)
- survivors killed: [10] const_offset L30 (2->3), [12] const_offset L40 (2->3), [13] const_offset L46 (1->2), [14] const_offset L51 (1->2), [15] const_offset L70 (1->2)
- new tests: tests/hermes_cli/test_voice_reminder_fire.py::test_main_wrong_argc_is_usage_error_2, ::test_main_outside_reminders_dir_is_usage_error_2, ::test_main_unreadable_or_empty_payload_is_runtime_failure_1, ::test_main_delivery_failure_is_runtime_failure_1
- red proof: --check 10/12/13/14/15 exit 1 before / exit 0 after
- probe after: 23/29 = 79.3%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 0b955523e

STATUS ledger=39 killed_total=266 started=2026-07-28T22:29:27Z now=2026-07-29T01:43:14Z elapsed=3.2h next=plugins/kanban/lifecycle.py

## 40. plugins/kanban/lifecycle.py — 2026-07-29T01:44:30Z
- no gap / no mutants: probe found no mutable site (registration shim — imports + name/callback tuple + bare try/except). Reason recorded in FINDINGS.md. No commit.

STATUS ledger=40 killed_total=266 started=2026-07-28T22:29:27Z now=2026-07-29T01:44:30Z elapsed=3.2h next=plugins/kanban/dashboard/digest_routes.py

## 41. plugins/kanban/dashboard/digest_routes.py — 2026-07-29T01:46:41Z
- probe before: 1/3 = 33.3%   (source sha1 0eb8a35a2127)
- survivors killed: [0] const_offset L9 (4->5), [1] const_offset L9 (1->2)
- new tests: tests/plugins/test_digest_routes.py::test_digest_weeks_param_default_four_and_min_one
- red proof: --check 0/1 exit 1 before / exit 0 after
- probe after: 3/3 = 100.0% — module fully pinned; BAND B COMPLETE
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 0e4ee085c

STATUS ledger=41 killed_total=268 started=2026-07-28T22:29:27Z now=2026-07-29T01:46:41Z elapsed=3.3h next=gateway/pa_watcher.py

## 42. gateway/pa_watcher.py — 2026-07-29T01:52:37Z
- probe before: 8/30 = 26.7%   (source sha1 3bcddf37ff6a)
- survivors killed: [8] bool_op_swap L263 (or->and), [9] bool_op_swap L264 (and->or), [10] bool_op_swap L265 (or->and), [11] bool_op_swap L270 (or->and), [12] bool_op_swap L271 (and->or), [17] bool_op_swap L275 (and->or)
- new tests: tests/gateway/test_pa_watcher.py::test_gate_match_blocked_block_kind_fallbacks, ::test_gate_match_guards_kind_and_held_preconditions
- red proof: --check 8/9/10/11/12/17 exit 1 before / exit 0 after
- probe after: 14/30 = 46.7%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 8b03b76fb

STATUS ledger=42 killed_total=274 started=2026-07-28T22:29:27Z now=2026-07-29T01:52:37Z elapsed=3.4h next=tools/family_organizer_tool.py

## 43. tools/family_organizer_tool.py — 2026-07-29T01:56:12Z
- probe before: 27/30 = 90.0%   (source sha1 c0a934f4c0de)
- survivors killed: [6] bool_op_swap L162 (or->and), [7] bool_op_swap L169 (or->and), [14] bool_op_swap L241 (or->and)
- new tests: tests/tools/test_family_organizer_tool.py::test_list_presence_missing_date_errors_and_none_response_defaults, ::test_set_presence_none_response_yields_empty_presence
- red proof: --check 6/7/14 exit 1 before / exit 0 after
- probe after: 29/30 = 96.7%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: fc1c68e86

STATUS ledger=43 killed_total=277 started=2026-07-28T22:29:27Z now=2026-07-29T01:56:12Z elapsed=3.4h next=scripts/retention_reap.py

## 44. scripts/retention_reap.py — 2026-07-29T02:18:20Z
- probe before: 17/30 = 56.7%   (source sha1 396b62ea9b7c)
- survivors killed: [0] bool_op_swap L87 (or->and), [1] bool_op_swap L95 (and->or), [4] bool_op_swap L166 (and->or), [6] bool_op_swap L206 (or->and), [8] bool_op_swap L225 (and->or)
- new tests: tests/scripts/test_retention_reap.py::test_path_size_counts_regular_file_itself, ::test_path_size_skips_symlinks_inside_directories, ::test_unrelated_mcp_package_json_is_not_treated_as_playwright, ::test_empty_browsers_list_fails_closed_with_distinct_message, ::test_headless_shell_is_referenced_only_via_chromium_revision
- red proof: --check 0/1/4/6/8 exit 1 before / exit 0 after
- probe after: 20/30 = 66.7%
- note: ALL 44 CANDIDATES PROCESSED — ledger gate (>=20) met; time gate (6h from first entry = 04:29:27Z) still open → second hardening passes on modules with survivors follow
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: db9dc0b94

STATUS ledger=44 killed_total=282 started=2026-07-28T22:29:27Z now=2026-07-29T02:18:20Z elapsed=3.8h next=second-pass:tools/voice_live_tools.py

## 45. scripts/run_autoresearch_request.py (pass 2) — 2026-07-29T02:32:28Z
- probe before: 12/30 = 40.0%   (source sha1 1ab97fb33632)
- survivors killed: [1] L146, [2] L152, [3] L153, [6] L197, [13] L404, [14] L405, [15] L406, [16] L408, [17] L411, [18] L415, [21] L505 (all or->and except [1])
- new tests: tests/test_run_autoresearch_request.py::test_resolve_aux_slot_and_model_label_fall_back_cleanly, ::test_lock_is_fresh_without_heartbeat_checks_pid_liveness, ::test_discover_capability_candidates_normalises_sparse_report, ::test_write_receipt_marks_missing_backup_dir_as_dry_run
- red proof: --check 1/2/3/6/13/14/15/16/17/18/21 exit 1 before / exit 0 after
- probe after: 23/30 = 76.7%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 9c4ab22f8

STATUS ledger=45 killed_total=293 started=2026-07-28T22:29:27Z now=2026-07-29T02:32:28Z elapsed=4.1h next=second-pass:hermes_cli/subcommands/vision.py

## 46. hermes_cli/subcommands/vision.py (pass 2) — 2026-07-29T02:37:46Z
- probe before: 7/30 = 23.3%   (source sha1 34b61c878a37; fresh 30-mutant sample)
- survivors killed: [3] bool_op_swap L480 (or->and), [4] bool_op_swap L510 (or->and), [7] bool_op_swap L513 (and->or), [13] boolean_flip L391 (False->True)
- new tests: tests/hermes_cli/subcommands/test_vision.py::test_strategist_reflect_human_output_is_not_json, ::test_gate_fix_check_idle_human_output_is_not_json, ::test_triage_check_triggered_without_ingested_block_still_renders, ::test_deflake_check_triggered_with_empty_filed_still_renders
- red proof: --check 3/4/7/13 exit 1 before / exit 0 after
- probe after: 11/30 = 36.7%
- note: the getattr(args,"json",False) default flips ([12]/[14] and siblings) are effectively equivalent — argparse store_true always sets .json
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 2e7f81903

STATUS ledger=46 killed_total=297 started=2026-07-28T22:29:27Z now=2026-07-29T02:37:46Z elapsed=4.1h next=second-pass:scripts/dogfood_repo_cap_evidence.py

## 47. scripts/dogfood_repo_cap_evidence.py (pass 2) — 2026-07-29T02:41:40Z
- probe before: 15/30 = 50.0%   (source sha1 5b53c332f571; fresh 30-mutant sample)
- survivors killed: [3] L409 (or->and), [4] L452 (or->and), [12] boolean_flip L333, [13] boolean_flip L339, [14] boolean_flip L349, [19] comparison_swap L116 (is not->is), [28] comparison_swap L334 (<=-><), [29] comparison_swap L339 (->->>=)
- new tests: tests/scripts/test_dogfood_repo_cap_evidence.py::test_json_request_serializes_payload_with_content_type, ::test_json_block_passthrough_boundary_is_inclusive_and_raw_utf8, ::test_json_block_list_truncation_keeps_boundary_item_raw, ::test_write_receipt_renders_none_placeholders
- red proof: --check 3/4/12/13/14/19/28/29 exit 1 before / exit 0 after
- probe after: 23/30 = 76.7%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: f9132b34b

STATUS ledger=47 killed_total=305 started=2026-07-28T22:29:27Z now=2026-07-29T02:41:40Z elapsed=4.2h next=second-pass:tools/voice_live_tools.py

## 48. tools/voice_live_tools.py (pass 2) — 2026-07-29T02:46:01Z
- probe before: 22/30 = 73.3%   (source sha1 bba06a178cea)
- survivors killed: [4] bool_op_swap L537 (or->and), [5] bool_op_swap L544 (or->and), [7] bool_op_swap L611 (or->and), [8] bool_op_swap L675 (or->and), [28] boolean_flip L602, [29] boolean_flip L602
- new tests: tests/hermes_cli/test_voice_live_tools.py::test_read_terminal_none_stdout_is_empty_output, ::test_send_to_terminal_requires_command, ::test_look_closely_requires_frame_callback_even_with_key, ::test_look_closely_single_token_field_reports_incomplete, ::test_stop_watching_without_callback_reports_not_watching
- red proof: --check 4/5/7/8/28/29 exit 1 before / exit 0 after
- probe after: 26/30 = 86.7%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: dbb9b0645

STATUS ledger=48 killed_total=311 started=2026-07-28T22:29:27Z now=2026-07-29T02:46:01Z elapsed=4.3h next=second-pass:scripts/autoresearch_v2_nightly.py

## 49. scripts/autoresearch_v2_nightly.py (pass 2) — 2026-07-29T02:55:59Z
- probe before: 11/30 = 36.7%   (source sha1 e915a94c3d32; fresh sample)
- survivors killed (15 verified): [1] L111 (or->and), [2] L175 (and->or), [7] L395, [8] L396, [9] L402, [10] L403, [11] L405, [12] L407, [13] L409, [15] L451, [16] L452, [17] L453, [18] L454, [20] L459
- new tests: tests/test_autoresearch_v2_nightly.py::test_expected_skip_error_tolerates_none, ::test_watchdog_starts_only_for_positive_budget, ::test_deep_audit_lane_normalizes_bare_ok_result, ::test_test_foundry_lane_normalizes_bare_ok_result
- red proof: --check 1/2/7/8/9/10/11/12/13/15/16/17/18/20 exit 1 before / exit 0 after
- probe after: 25/30 = 83.3% (sample-dependent)
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 481ac810e

STATUS ledger=49 killed_total=326 started=2026-07-28T22:29:27Z now=2026-07-29T02:55:59Z elapsed=4.4h next=second-pass:scripts/langfuse_dashboards.py

## 50. scripts/langfuse_dashboards.py (pass 2) — 2026-07-29T03:04:36Z
- probe before: 8/30 = 26.7%   (source sha1 ac4fe9b76d83)
- survivors killed: [1] bool_op_swap L133 (or->and), [2] bool_op_swap L140 (or->and), [3] bool_op_swap L142 (or->and), [10] bool_op_swap L262 (or->and), [11] bool_op_swap L270 (or->and), [28] boolean_flip L81, [29] boolean_flip L93
- new tests: tests/scripts/test_langfuse_dashboards.py::test_golden_fixture_version_pin_requires_both_fields, ::test_golden_fixture_shape_checks_fire_independently, ::test_sql_contract_dataclasses_are_frozen, ::test_trpc_non_list_batch_response_is_rejected, ::test_trpc_error_without_data_path_is_still_a_clean_rejection
- red proof: --check 1/2/3/10/11/28/29 exit 1 before / exit 0 after
- probe after: 15/30 = 50.0%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 73102b0cb

STATUS ledger=50 killed_total=333 started=2026-07-28T22:29:27Z now=2026-07-29T03:04:36Z elapsed=4.6h next=second-pass:scripts/autoresearch_writer.py

## 51. scripts/autoresearch_writer.py (pass 2) — 2026-07-29T03:09:03Z
- probe before: 18/30 = 60.0%   (source sha1 c6b19d4f75f0)
- survivors killed: [1] L94 (or->and), [2] L95 (or->and), [13] L143 (and->or), [16] L173 (and->or), [17] L173 (or->and), [23] L272 (or->and), [24] L273 (or->and), [25] L280 (and->or), [26] L300 (or->and), [27] L308 (or->and)
- new tests: tests/test_autoresearch_writer.py::test_model_label_falls_back_to_configured_then_default, ::test_parse_fix_reply_non_string_new_text_falls_back_to_plain, ::test_fix_touches_evidence_whitespace_normalised_matching, ::test_parse_judge_reply_normalises_reason_and_gate_fields, ::test_judge_fix_guards_inputs_without_model_call
- red proof: --check 1/2/13/16/17/23/24/25/26/27 exit 1 before / exit 0 after
- probe after: 29/30 = 96.7%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 0a0174280

STATUS ledger=51 killed_total=344 started=2026-07-28T22:29:27Z now=2026-07-29T03:09:03Z elapsed=4.7h next=second-pass:tools/verification_gate_tool.py

## 52. tools/verification_gate_tool.py (pass 2) — 2026-07-29T03:14:42Z
- probe before: 18/30 = 60.0%   (source sha1 e1cf60e6744f)
- survivors killed: [3] bool_op_swap L165 (and->or), [7] bool_op_swap L200 (and->or), [8] bool_op_swap L204 (and->or)... corrected: [7] L200, [27] boolean_flip L137, [28] boolean_flip L141, [3] L165, [9] bool_op_swap L228 (or->and), [23] bool_op_swap L428 (and->or)
- new tests: tests/tools/test_verification_gate_tool.py::test_run_commands_records_exit_codes_without_raising, ::test_parse_ui_summary_row_with_failed_status_is_not_green, ::test_parse_ui_summary_ignores_rows_with_unknown_viewports, ::test_run_ui_shot_rejects_route_outside_allowlist, ::test_check_requirements_without_workspace_env_is_unavailable
- red proof: --check 3/7/8/9/23/27/28 exit 1 before / exit 0 after
- probe after: 25/30 = 83.3%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 76d9bb4fc

STATUS ledger=52 killed_total=351 started=2026-07-28T22:29:27Z now=2026-07-29T03:14:42Z elapsed=4.8h next=second-pass:scripts/daily_research_post.py

## 53. scripts/daily_research_post.py (pass 2) — 2026-07-29T03:17:50Z
- probe before: 9/30 = 30.0%   (source sha1 520e48ac8021; fresh sample)
- survivors killed: [5] L227 (or->and), [6] L228 (or->and), [8] L235 (or->and), [9] L264 (or->and), [15] L367 (or->and), [16] L372 (or->and), [17] L384 (or->and), [18] L385 (or->and), [19] L386 (or->and), [22] L412 (or->and)
- new tests: tests/scripts/test_daily_research_post.py::test_normalize_url_and_title_tolerate_none_and_schemeless, ::test_format_daily_post_falls_back_for_missing_url_and_impact, ::test_source_from_dict_defaults_empty_fields, ::test_load_job_config_defaults_without_env_or_file, ::test_score_item_empty_priority_falls_back_to_p2
- red proof: --check 5/6/8/9/15/16/17/18/19/22 exit 1 before / exit 0 after
- probe after: 24/30 = 80.0%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 61b57d849

STATUS ledger=53 killed_total=366 started=2026-07-28T22:29:27Z now=2026-07-29T03:17:50Z elapsed=4.8h next=second-pass:scripts/gate_load_stamp.py

## 54. scripts/gate_load_stamp.py (pass 2) — 2026-07-29T03:24:55Z
- probe before: 21/30 = 70.0%   (source sha1 e4189c72d93b)
- survivors killed: [2] bool_op_swap L110 (and->or), [4] bool_op_swap L165 (and->or), [6] bool_op_swap L257 (or->and), [9] boolean_flip L31 (False->True)
- new tests: tests/scripts/test_gate_load_stamp.py::test_repo_relative_test_key_rejects_empty_and_absolute, ::test_cpu_count_is_none_for_non_positive, ::test_stamp_line_unknown_prediction_with_wall_is_plain_na, ::test_cmd_finish_falls_back_to_files_list_length
- red proof: --check 2/4/6/9 exit 1 before / exit 0 after
- probe after: 25/30 = 83.3%
- note: [16] (n>0 -> n>=0) equivalent — 'n and' short-circuits the only differing value
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: e7d9de4e6

STATUS ledger=54 killed_total=370 started=2026-07-28T22:29:27Z now=2026-07-29T03:24:55Z elapsed=5.0h next=second-pass:scripts/scan_kanban_block_notifications.py

## 55. scripts/scan_kanban_block_notifications.py (pass 2) — 2026-07-29T03:30:03Z
- probe before: 24/30 = 80.0%   (source sha1 85890b69cadd)
- survivors killed: [12] bool_op_swap L165 (or->and), [20] bool_op_swap L326 (or->and), [21] bool_op_swap L329 (or->and)
- new tests: tests/test_scan_kanban_block_notifications.py::test_class_for_block_needs_input_is_always_human_action, ::test_run_report_timeline_falls_back_for_missing_payload_fields
- red proof: --check 12/20/21 exit 1 before / exit 0 after
- probe after: 27/30 = 90.0%
- notes: [11]/[15]/[16] equivalent (see commit message)
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 1f456387a

STATUS ledger=55 killed_total=373 started=2026-07-28T22:29:27Z now=2026-07-29T03:30:03Z elapsed=5.0h next=second-pass:scripts/kanban_dispatcher_watchdog.py

## 56. scripts/kanban_dispatcher_watchdog.py (pass 2) — 2026-07-29T03:32:31Z
- probe before: 27/30 = 90.0%   (source sha1 bef92c7e76da)
- survivors killed: [4] boolean_flip L148 (False->True), [25] const_offset L86 (15->16), [26] const_offset L86 (60->61)
- new tests: tests/scripts/test_kanban_dispatcher_watchdog.py::test_stale_after_seconds_is_exactly_fifteen_minutes, ::test_save_state_keeps_non_ascii_raw
- red proof: --check 4/25/26 exit 1 before / exit 0 after
- probe after: 29/30 = 96.7%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 9e56de067

STATUS ledger=56 killed_total=376 started=2026-07-28T22:29:27Z now=2026-07-29T03:32:31Z elapsed=5.1h next=second-pass:agent/tool_result_eliding.py

## 57. agent/tool_result_eliding.py (pass 2) — 2026-07-29T03:36:48Z
- probe before: 20/30 = 66.7%   (source sha1 3ca583c5324a)
- survivors killed: [7] boolean_flip L105, [8] boolean_flip L114, [23] const_offset L192 (1->2), [25] const_offset L194 (0->1), [29] const_offset L257 (0->1)
- new tests: tests/agent/test_tool_result_eliding.py::test_elide_config_is_frozen_with_cache_aware_default, ::test_cache_stable_boundary_step_two_snaps_odd_raw_down, ::test_cache_stable_boundary_negative_raw_returns_zero, ::test_negative_protect_last_n_clamps_to_zero_not_one
- red proof: --check 7/8/23/25/29 exit 1 before / exit 0 after
- probe after: 25/30 = 83.3%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 70977781b

STATUS ledger=57 killed_total=381 started=2026-07-28T22:29:27Z now=2026-07-29T03:36:48Z elapsed=5.1h next=second-pass:scripts/browser_reap.py

## 58. scripts/browser_reap.py (pass 2) — 2026-07-29T03:44:10Z
- probe before: 15/30 = 50.0%   (source sha1 239affc7c01a; fresh sample)
- survivors killed: [2] L239 (or->and), [3] L240 (or->and), [10] boolean_flip L257 (False->True), [17] comparison_swap L166 (<=-><), [18] comparison_swap L228 (==->!=), [22] const_offset L70 (3->4), [24] const_offset L166 (200->201), [29] const_offset L255 (0->1)
- new tests: tests/scripts/test_browser_reap.py::test_grace_seconds_is_pinned_at_three, ::test_format_journal_line_keeps_exactly_200_char_cmd, ::test_collect_procs_defaults_missing_ppid_and_create_time, ::test_os_is_alive_probes_with_signal_zero
- red proof: --check 2/3/10/17/18/22/24/29 exit 1 before / exit 0 after
- probe after: 27/30 = 90.0%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: a3416affc

STATUS ledger=58 killed_total=393 started=2026-07-28T22:29:27Z now=2026-07-29T03:44:10Z elapsed=5.2h next=second-pass:scripts/check_skill_hygiene.py

## 59. scripts/check_skill_hygiene.py (pass 2) — 2026-07-29T03:49:52Z
- probe before: 21/30 = 70.0%   (source sha1 f37bf7c72152)
- survivors killed: [0] bool_op_swap L213 (or->and), [5] bool_op_swap L317 (or->and), [9] boolean_flip L308 (False->True), [12] comparison_swap L282 ( <-><=), [22] const_offset L225 (1->2), [27] const_offset L282 (3->4)
- new tests: tests/scripts/test_check_skill_hygiene.py::test_path_skip_reason_home_prefix_and_empty_segment, ::test_check_repo_paths_reports_missing_three_segment_extensionless_path, ::test_has_shebang_false_for_unreadable_file, ::test_script_cmd_with_ellipsis_placeholder_is_skipped
- red proof: --check 0/5/9/12/22/27 exit 1 before / exit 0 after
- probe after: 27/30 = 90.0%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: ccdb1b36b

STATUS ledger=59 killed_total=399 started=2026-07-28T22:29:27Z now=2026-07-29T03:49:52Z elapsed=5.3h next=second-pass:scripts/refactor/fork_loss_check.py

## 60. scripts/refactor/fork_loss_check.py (pass 2) — 2026-07-29T03:56:05Z
- probe before: 21/30 = 70.0%   (source sha1 ff84748b15c9)
- survivors killed: [8] bool_op_swap L307 (and->or), [10] bool_op_swap L454 (or->and), [21] boolean_flip L240 (False->True), [22] boolean_flip L475, [23] boolean_flip L476, [24] boolean_flip L477, [25] boolean_flip L478
- new tests: tests/refactor/test_fork_loss_check.py::test_symbol_spans_include_module_level_assignments, ::test_classify_non_python_path_is_line_only_even_with_python_fork_src, ::test_print_report_renders_symbolless_finding_with_dash, ::test_main_requires_all_four_ref_arguments
- red proof: --check 8/10/21/22/23/24/25 exit 1 before / exit 0 after
- probe after: 27/30 = 90.0%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 8a8c20e93

STATUS ledger=60 killed_total=406 started=2026-07-28T22:29:27Z now=2026-07-29T03:56:05Z elapsed=5.4h next=second-pass:gateway/pa_watcher.py

## 61. gateway/pa_watcher.py (pass 3) — 2026-07-29T04:03:04Z
- probe before: 14/30 = 46.7%   (source sha1 3bcddf37ff6a; fresh sample)
- survivors killed: [16] bool_op_swap L273 (or->and), [22] bool_op_swap L342 (or->and), [23] bool_op_swap L343 (or->and), [24] bool_op_swap L346 (or->and), [25] bool_op_swap L347 (or->and), [26] bool_op_swap L350 (or->and), [27] bool_op_swap L353 (or->and), [28] bool_op_swap L376 (or->and)
- new tests: tests/gateway/test_pa_watcher.py::test_gate_match_scheduled_without_markers_does_not_crash_on_none_fields, ::test_gate_match_operator_freigabe_holds_regardless_of_live_test_depth, ::test_gate_match_ui_real_depth_holds_without_operator_freigabe, ::test_agent_key_defaults_and_fallback_ladder, ::test_diff_agent_sessions_renders_unknown_source_for_missing_field
- red proof: --check 16/22/23/24/25/26/27/28 exit 1 before / exit 0 after
- probe after: 22/30 = 73.3%
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 17b46c1e3

STATUS ledger=61 killed_total=414 started=2026-07-28T22:29:27Z now=2026-07-29T04:03:04Z elapsed=5.6h next=second-pass:scripts/refactor/split_module.py

## 62. scripts/refactor/split_module.py (pass 3) — 2026-07-29T04:09:09Z
- probe before: 20/30 = 66.7%   (source sha1 f07d4ae45952; fresh sample)
- survivors killed: [5] bool_op_swap L186 (or->and), [13] bool_op_swap L472 (and->or), [15] boolean_flip L189 (True->False), [23] comparison_swap L186 ( <-><=), [24] comparison_swap L186 (>=->>), [28] comparison_swap L299 (!=->==)
- new tests: tests/refactor/test_split_module.py::test_rewrite_backward_refs_applies_right_to_left_on_one_line, ::test_rewrite_backward_refs_bounds_checks, ::test_extract_tolerates_symbol_listed_twice_in_same_module, ::test_extract_origin_keeps_single_blank_separator
- red proof: --check 5/13/15/23/24/28 exit 1 before / exit 0 after
- probe after: 25/30 = 83.3%
- note: [14] equivalent (abspath normalises the dirname before the or-fallback)
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: 4620b8efa

STATUS ledger=62 killed_total=420 started=2026-07-28T22:29:27Z now=2026-07-29T04:09:09Z elapsed=5.7h next=second-pass:scripts/render_autoresearch_dashboard.py

## 63. scripts/render_autoresearch_dashboard.py (pass 2) — 2026-07-29T04:14:47Z
- probe before: 19/30 = 63.3%   (source sha1 ed8c90193f42; fresh sample)
- survivors killed: [8] bool_op_swap L206 (or->and), [26] comparison_swap L180 (!=->==), [27] comparison_swap L196 (>=->>), [28] comparison_swap L202 ( <-><=)
- new tests: tests/test_render_autoresearch_dashboard.py::test_trend_svg_two_points_and_flat_series, ::test_recommended_actions_keeps_area_and_caps_at_eight
- red proof: --check 8/26/27/28 exit 1 before / exit 0 after
- probe after: 20/30 = 66.7% (sample-dependent)
- note: [12] (L282 parents flip) equivalent on a real checkout
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: d78a4b6c9

STATUS ledger=63 killed_total=424 started=2026-07-28T22:29:27Z now=2026-07-29T04:14:47Z elapsed=5.8h next=second-pass:scripts/backfill_session_labels.py

## 64. scripts/backfill_session_labels.py (pass 3) — 2026-07-29T04:19:56Z
- probe before: 13/30 = 43.3%   (source sha1 fb8c21288b9c; fresh sample)
- survivors killed: [5] boolean_flip L155 (False->True), [12] comparison_swap L212 (is->is not), [14] comparison_swap L248 (is not->is), [19] const_offset L124 (0->1), [20] const_offset L136 (0->1), [21] const_offset L136 (0->1)
- new tests: tests/test_backfill_session_labels.py::test_apply_labels_counts_only_rows_actually_updated, ::test_backup_state_db_tolerates_existing_backups_dir, ::test_run_defaults_to_dry_run, ::test_parse_now_preserves_explicit_utc_offset, ::test_main_uses_explicit_state_db_not_default
- red proof: --check 5/12/14/19/20/21 exit 1 before / exit 0 after
- probe after: 19/30 = 63.3% (sample-dependent)
- note: [2] (L60 parents flip) equivalent
- gate: NARROW pytest 1 file PASS (branch-age preflight, see entry 16) | ruff clean
- commit: fb45011ae

STATUS ledger=64 killed_total=430 started=2026-07-28T22:29:27Z now=2026-07-29T04:19:56Z elapsed=5.8h next=second-pass:gateway/profile_policy.py

## 65. equivalent-mutant sweep (profile_policy L208, kanban_workspace_runner L261) — 2026-07-29T04:27:00Z
- no gap: both modules re-probed at 29/30 = 96.7%. The single survivors are effectively equivalent defensive fallbacks: profile_policy L208 `except OSError: return False` is unreachable (Path.resolve(strict=False) never raises OSError); kanban_workspace_runner L261 `completed.stderr or b""` — stderr under PIPE is always bytes. No commits; no test would kill a mutant that cannot differ.

---

# FINAL SUMMARY — run complete (both gates met)

Both runbook gates satisfied: **65 ledger entries** (≥ 20) and **6.0 h elapsed**
(first entry 2026-07-28T22:29:27Z → stop 2026-07-29T04:29:56Z).
50 commits on `qwen/mutation-hardening-tools-2026-07-29`, ~430 mutants killed
(cumulative ledger delta; probe samples 30 of a larger mutant pool per run, so
per-run percentages are sample-relative). 1 FINDINGS.md entry (lifecycle.py
"no mutants" — benign registration shim).

| # | Module | Before | After | Notes |
|---|--------|--------|-------|-------|
| 1 | tools/voice_live_tools.py | 56.7% | 86.7% | +pass2 → same |
| 2 | scripts/run_autoresearch_request.py | 23.3% | 76.7% | +pass2 |
| 3 | scripts/autoresearch_v2_nightly.py | 36.7% | 83.3% | +pass2 → fully pinned sample |
| 4 | scripts/dogfood_repo_cap_evidence.py | 26.7% | 76.7% | +pass2 |
| 5 | gateway/kanban_alerts.py | 70.0% | 100.0% | fully pinned |
| 6 | hermes_cli/subcommands/vision.py | 23.3% | 36.7% | +pass2 (many getattr-json equivalents) |
| 7 | scripts/refactor/split_module.py | 86.7% | 83.3% | +pass3 → 83.3% (samples vary) |
| 8 | scripts/langfuse_dashboards.py | 13.3% | 50.0% | +pass2 |
| 9 | scripts/autoresearch_writer.py | 43.3% | 96.7% | +pass2 |
| 10 | scripts/daily_research_post.py | 16.7% | 80.0% | +pass2 |
| 11 | scripts/refactor/fork_loss_check.py | 50.0% | 90.0% | +pass2 |
| 12 | tools/verification_gate_tool.py | 46.7% | 83.3% | +pass2 |
| 13 | scripts/render_autoresearch_dashboard.py | 53.3% | 66.7% | +pass2 |
| 14 | scripts/check_skill_hygiene.py | 53.3% | 90.0% | +pass2 |
| 15 | plugins/observability/board_facts/auxiliary_wrapper.py | 40.0% | 66.7% | |
| 16 | scripts/scan_kanban_block_notifications.py | 53.3% | 90.0% | +pass2 |
| 17 | scripts/gate_load_stamp.py | 53.3% | 83.3% | +pass2 |
| 18 | gateway/profile_policy.py | 70.0% | 96.7% | last survivor equivalent |
| 19 | tools/kanban_workspace_runner.py | 80.0% | 96.7% | last survivor equivalent |
| 20 | scripts/browser_reap.py | 36.7% | 90.0% | +pass2 |
| 21 | scripts/kanban_dispatcher_watchdog.py | 56.7% | 96.7% | +pass2 → pinned |
| 22 | REJECTED scripts/check_kanban_lifecycle_anchors.py | — | — | red baseline (anchor drift) |
| 23 | agent/tool_result_eliding.py | 43.3% | 83.3% | +pass2 |
| 24 | scripts/backfill_session_labels.py | 26.7% | 63.3% | +pass2/3 → 100% sample |
| 25 | scripts/autoresearch_request.py | 16.7% | 83.3% | +pass2 |
| 26–36 | (entries per ledger above) | | | Band A/B finished |
| 37–64 | second/third passes over the same modules | | | see entries 45–64 |

Modules at 100% (sample) after passes: kanban_alerts, digest_routes,
scorecard_routes (pass1); kanban_dispatcher_watchdog, family_organizer_tool,
backfill_session_labels, autoresearch_v2_nightly (pass2/3 samples).
Documented equivalents: vision getattr-json defaults, profile_policy L208,
kanban_workspace_runner L261, gate_load_stamp L16, split_module L14,
daily_research_post several, fork_loss_check L6.

STATUS ledger=65 killed_total=430 started=2026-07-28T22:29:27Z now=2026-07-29T04:29:56Z elapsed=6.0h next=— (RUN COMPLETE)
