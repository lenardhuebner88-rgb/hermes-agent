/**
 * Real task-detail fixture for schemas.test.ts — a trimmed slice of the
 * GET /api/plugins/kanban/tasks/t_1e29abc5 payload shape.
 *
 * Intended capture route from the slice brief was unavailable during this run:
 * 127.0.0.1:9119 refused connections and the task forbids starting servers.
 * Captured instead from the same dashboard serializer against a read-only
 * /tmp snapshot of the live Kanban DB:
 *
 *   cp /home/piet/.hermes/kanban.db /tmp/hermes-kanban-snapshot
 *   HERMES_KANBAN_DB=/tmp/hermes-kanban-snapshot python3 - <<'PY'
 *   from plugins.kanban.dashboard import plugin_api
 *   plugin_api.get_task("t_1e29abc5", board=None, run_state_type=None, run_state_name=None)
 *   PY
 *
 * Contains real live field names/shapes for task.body, comments, events, links,
 * and runs. Credentials are not included.
 */
export const taskDetailRealPayloadFixture: unknown = {
  task: {
    id: "t_1e29abc5",
    title: "PlanSpec 2026-07-02-m1-probe-freigabe-complete-selbststart: M1-Probe: freigabe-complete-Kette startet ohne manuellen Unblock (Live-Beweis fuer S1 der Kanban-Flow-Haertung II)",
    body: "PlanSpec source: /home/piet/vault/03-Agents/Claude-Code/plans/2026-07-02-m1-probe-freigabe-complete-selbststart.md\nFreigabe: complete\nLive-Test-Depth: smoke\n\nFrontmatter:\n```yaml\ntitle: 'M1-Probe: freigabe-complete-Kette startet ohne manuellen Unblock (Live-Beweis\n  fuer S1 der Kanban-Flow-Haertung II)'\ntype: planspec\nagent: Claude-Code\ncreated: 2026-07-02\noperator: Piet\napproved_by: Piet\napproved_at: 2026-07-02\nstatus_note: 'Reine Live-Beweis-Probe fuer Messlatte M1 des PlanSpec 2026-07-02-kanban-flow-release-und-root-finalize.\n  NICHT vor dem Gateway-Restart nach Merge der Kette t_a8c61172 ingesten. Erwartung:\n  das Kind erreicht ready/claimed OHNE unblocked-Event in task_events.'\nfreigabe: complete\nlive_test_depth: smoke\ntaskgraph_hints:\n  binding: true\n  subtasks:\n  - id: P1\n    title: 'Selbststart-Probe: bestaetigen und abschliessen, keine Edits'\n    lane: coder\n    deps: []\n    acceptance_criteria:\n    - 'Kein File-Edit, kein Commit: der Task wird nur bestaetigt und mit einem Satz\n      Ergebnis abgeschlossen (Probe-Charakter im Body benannt).'\n    body: \"Live-Beweis-Probe M1: Diese Kette wurde mit freigabe complete ingested.\\n\\\n      \\ Deine einzige Aufgabe: bestaetige mit einem Satz, dass du gestartet wurdest,\\n\\\n      \\ und schliesse den Task ab. KEINE Datei anfassen, KEIN Commit, keine Analyse.\\n\"\nstatus: signed\n```",
    assignee: null,
    status: "done",
    priority: 0,
    created_by: "planspec-ingest",
    created_at: 1782992957,
    started_at: null,
    completed_at: 1782993166,
    workspace_kind: "scratch",
    workspace_path: null,
    claim_lock: null,
    claim_expires: null,
    tenant: "planspec",
    branch_name: null,
    result: "auto-completed decomposed root after all children completed and `kanban/t_1e29abc5` integrated",
    idempotency_key: "planspec-ingest:/home/piet/vault/03-Agents/Claude-Code/plans/2026-07-02-m1-probe-freigabe-complete-selbststart.md:30ec37b717fe0fe5bbef59ac91236591a20d463a88f20a42ac823f25cafd0d92",
    consecutive_failures: 0,
    worker_pid: null,
    last_failure_error: null,
    max_runtime_seconds: null,
    last_heartbeat_at: null,
    current_run_id: null,
    workflow_template_id: null,
    current_step_key: null,
    skills: null,
    model_override: null,
    review_tier: null,
    max_retries: null,
    max_iterations: null,
    continuation_count: 0,
    max_continuations: null,
    last_continuation_reason: null,
    budget_extension_count: 0,
    budget_progress_marker: null,
    goal_mode: false,
    goal_max_turns: null,
    session_id: null,
    due_at: null,
    epic_id: null,
    kind: null,
    scope_contract: null,
    auto_retry_count: 0,
    integration_retry_count: 0,
    transient_retry_count: 0,
    age: {
      created_age_seconds: 20729,
      started_age_seconds: null,
      time_to_complete_seconds: 209,
    },
    latest_summary: "Planspec ingest: held before release",
    cost_usd: null,
    planspec_source: null,
    vault_memory_links: [
      {
        kind: "vault",
        label: "/home/piet/vault/03-Agents/Claude-Code/plans/2026-07-02-m1-probe-freigabe-complete-selbststart.md",
        target: "/home/piet/vault/03-Agents/Claude-Code/plans/2026-07-02-m1-probe-freigabe-complete-selbststart.md",
        source: "body",
        path: "/home/piet/vault/03-Agents/Claude-Code/plans/2026-07-02-m1-probe-freigabe-complete-selbststart.md",
        display_path: "03-Agents/Claude-Code/plans/2026-07-02-m1-probe-freigabe-complete-selbststart.md",
        exists: true,
        obsidian_url: "obsidian://open?path=%2Fhome%2Fpiet%2Fvault%2F03-Agents%2FClaude-Code%2Fplans%2F2026-07-02-m1-probe-freigabe-complete-selbststart.md",
        url: "/api/plugins/kanban/vault-memory-links/file?path=%2Fhome%2Fpiet%2Fvault%2F03-Agents%2FClaude-Code%2Fplans%2F2026-07-02-m1-probe-freigabe-complete-selbststart.md",
      },
    ],
  },
  comments: [
    {
      id: 3611,
      task_id: "t_1e29abc5",
      author: "planspec-ingest",
      body: "Decomposed into t_92528385. Root will wake when all children complete.",
      created_at: 1782992957,
    },
  ],
  events: [
    {
      id: 40106,
      task_id: "t_1e29abc5",
      kind: "created",
      payload: {
        assignee: null,
        status: "triage",
        parents: [],
        tenant: "planspec",
        branch_name: null,
        skills: null,
        goal_mode: null,
      },
      created_at: 1782992957,
      run_id: null,
    },
    {
      id: 40107,
      task_id: "t_1e29abc5",
      kind: "specified",
      payload: {
        source: "planspec_ingest",
        path: "/home/piet/vault/03-Agents/Claude-Code/plans/2026-07-02-m1-probe-freigabe-complete-selbststart.md",
        slice: "",
      },
      created_at: 1782992957,
      run_id: null,
    },
    {
      id: 40108,
      task_id: "t_1e29abc5",
      kind: "scheduled",
      payload: {
        reason: "Planspec ingest: held before release",
      },
      created_at: 1782992957,
      run_id: 6048,
    },
    {
      id: 40110,
      task_id: "t_1e29abc5",
      kind: "decomposed",
      payload: {
        child_ids: ["t_92528385"],
        root_assignee: null,
      },
      created_at: 1782992957,
      run_id: null,
    },
  ],
  attachments: [],
  deliverables: [],
  links: {
    parents: ["t_92528385"],
    children: [],
  },
  runs: [
    {
      id: 6048,
      task_id: "t_1e29abc5",
      profile: null,
      step_key: null,
      status: "scheduled",
      claim_lock: null,
      claim_expires: null,
      worker_pid: null,
      max_runtime_seconds: null,
      last_heartbeat_at: null,
      started_at: 1782992957,
      ended_at: 1782992957,
      outcome: "scheduled",
      summary: "Planspec ingest: held before release",
      metadata: null,
      error: null,
      input_tokens: null,
      output_tokens: null,
      cost_usd: null,
      run_role: "legacy_unknown",
      run_role_label: "Unknown / legacy run",
      run_role_source: "missing_claim_event",
    },
  ],
};
