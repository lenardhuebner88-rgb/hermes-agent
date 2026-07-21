"""Tests for per-task Kanban iteration-budget knobs.

``kanban create --max-iterations N`` persists N on the task row, and the
worker-env builder injects ``HERMES_MAX_ITERATIONS=N`` so the spawned
worker honours the per-task override instead of the profile default.

``--max-continuations`` is covered here at the create/validation layer;
run-state behaviour is covered by ``test_kanban_auto_continuation.py``.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from pathlib import Path

import pytest

from agent.iteration_budget import IterationBudget
from agent.turn_finalizer import finalize_turn
from cli import _configured_agent_max_turns
from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


def test_profile_agent_max_iterations_alias_sets_max_turns():
    assert _configured_agent_max_turns({"max_iterations": 50}) == 50
    assert _configured_agent_max_turns({"max_turns": 40, "max_iterations": 50}) == 40
    assert _configured_agent_max_turns({"max_turns": 0, "max_iterations": 50}) == 0


# ---------------------------------------------------------------------------

# (c) per-task --max-iterations
# ---------------------------------------------------------------------------


def test_budget_columns_exist_in_fresh_db(kanban_home):
    """init_db on a fresh HERMES_HOME must create iteration-budget
    and auto-continuation columns. Old DBs go through the additive
    `_migrate_add_optional_columns` branches.
    """
    with kb.connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "max_iterations" in cols
    assert "continuation_count" in cols
    assert "max_continuations" in cols
    assert "last_continuation_reason" in cols


def test_create_task_persists_max_iterations(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="audit-with-budget", max_iterations=120,
        )
        task = kb.get_task(conn, tid)
    assert task.max_iterations == 120


def test_create_task_persists_max_continuations_zero(kanban_home):
    """0 is meaningful: disable auto-continuation for this task."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="no-auto-continue", max_continuations=0,
        )
        task = kb.get_task(conn, tid)
    assert task.max_continuations == 0
    assert task.continuation_count == 0


def test_create_task_default_max_iterations_is_none(kanban_home):
    """NULL = inherit the profile default, the safe back-compat
    behaviour for any existing automation that doesn't pass the flag.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="default-budget")
        task = kb.get_task(conn, tid)
    assert task.max_iterations is None


def test_premium_code_task_gets_documented_default_budget(kanban_home):
    """Nacht M5.4: premium build work no longer inherits a 30-turn profile cap."""
    from hermes_cli.config import DEFAULT_CONFIG

    expected = DEFAULT_CONFIG["kanban"]["premium_build_max_iterations"]
    assert expected == 90
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="medium premium build",
            assignee="premium",
            kind="code",
        )
        task = kb.get_task(conn, tid)
    assert task.max_iterations == expected


def test_premium_code_explicit_budget_wins_and_non_code_stays_unset(kanban_home):
    with kb.connect() as conn:
        explicit_id = kb.create_task(
            conn,
            title="bounded premium build",
            assignee="premium",
            kind="code",
            max_iterations=45,
        )
        research_id = kb.create_task(
            conn,
            title="premium research",
            assignee="premium",
            kind="research",
        )
        explicit = kb.get_task(conn, explicit_id)
        research = kb.get_task(conn, research_id)
    assert explicit.max_iterations == 45
    assert research.max_iterations is None


def test_decompose_defaults_premium_code_child_budget(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="premium fanout", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee=None,
            children=[
                {
                    "title": "premium implementation",
                    "assignee": "premium",
                    "kind": "code",
                    "parents": [],
                },
            ],
        )
        assert child_ids is not None
        child = kb.get_task(conn, child_ids[0])
    assert child.max_iterations == 90


def test_cli_create_flag_parses():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    ns = parser.parse_args(
        ["kanban", "create", "audit",
         "--body", "audit body",
         "--max-iterations", "120",
         "--max-continuations", "2"],
    )
    assert ns.max_iterations == 120
    assert ns.max_continuations == 2


def test_cli_create_rejects_zero_max_iterations(kanban_home, capsys, monkeypatch):
    """`--max-iterations 0` is nonsensical — refuse rather than
    creating a guaranteed-to-fail task.
    """
    ns = argparse.Namespace(
        title="zero",
        body="b",
        assignee=None,
        priority=0,
        parent=None,
        tenant=None,
        created_by=None,
        workspace="scratch",
        branch=None,
        triage=False,
        max_runtime=None,
        max_retries=None,
        max_iterations=0,
        max_continuations=None,
        skills=None,
        idempotency_key=None,
        initial_status="running",
        json=False,
        scope_contract_json=None,
        allowed_tool=[],
        forbidden_system=[],
        report_contract_version=1,
        unsafe=False,
        raw_create=False,
    )
    rc = kc._cmd_create(ns)
    assert rc == 2
    err = capsys.readouterr().err
    assert "--max-iterations must be >= 1" in err


def test_cli_create_rejects_negative_max_continuations(kanban_home, capsys):
    ns = argparse.Namespace(
        title="negative-continuations",
        body="b",
        assignee=None,
        priority=0,
        parent=None,
        tenant=None,
        created_by=None,
        workspace="scratch",
        branch=None,
        triage=False,
        max_runtime=None,
        max_retries=None,
        max_iterations=None,
        max_continuations=-1,
        skills=None,
        idempotency_key=None,
        initial_status="running",
        json=False,
        scope_contract_json=None,
        allowed_tool=[],
        forbidden_system=[],
        report_contract_version=1,
        unsafe=False,
        raw_create=False,
    )
    rc = kc._cmd_create(ns)
    assert rc == 2
    err = capsys.readouterr().err
    assert "--max-continuations must be >= 0" in err


def test_cli_create_end_to_end_persists(kanban_home, capsys):
    """End-to-end: pass --max-iterations through _cmd_create and read
    back via `get_task`.
    """
    ns = argparse.Namespace(
        title="end-to-end",
        body="body",
        assignee=None,
        priority=0,
        parent=None,
        tenant=None,
        created_by=None,
        workspace="scratch",
        branch=None,
        triage=False,
        max_runtime=None,
        max_retries=None,
        max_iterations=90,
        max_continuations=2,
        skills=None,
        idempotency_key=None,
        initial_status="running",
        json=True,
        scope_contract_json=None,
        allowed_tool=[],
        forbidden_system=[],
        report_contract_version=1,
        unsafe=False,
        raw_create=False,
    )
    rc = kc._cmd_create(ns)
    assert rc == 0
    import json as _json
    payload = _json.loads(capsys.readouterr().out)
    assert payload["max_iterations"] == 90
    assert payload["max_continuations"] == 2
    assert payload["continuation_count"] == 0

    with kb.connect() as conn:
        task = kb.get_task(conn, payload["id"])
    assert task.max_iterations == 90
    assert task.max_continuations == 2


def test_worker_env_injects_hermes_max_iterations(kanban_home, monkeypatch):
    """The worker-env builder must export HERMES_MAX_ITERATIONS=N
    when the task has a non-null max_iterations. NULL = no export
    (worker inherits the profile/global default).

    Verified by capturing the env dict via a monkey-patched
    ``subprocess.Popen``.
    """
    captured: dict[str, dict] = {}

    class _FakePopen:
        def __init__(self, cmd, env=None, **kwargs):
            captured["env"] = dict(env or {})
            self.pid = 12345

        def wait(self, *a, **kw):
            return 0

    import subprocess
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda name: str(kanban_home),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.normalize_profile_name",
        lambda name: name,
    )

    # With per-task override:
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="audit", assignee="coder",
            max_iterations=150,
        )
        task = kb.get_task(conn, tid)
    kb._default_spawn(task, "/tmp/ws", board="default")
    assert captured["env"].get("HERMES_MAX_ITERATIONS") == "150"

    captured.clear()

    # Without per-task override:
    with kb.connect() as conn:
        tid2 = kb.create_task(conn, title="default-budget", assignee="coder")
        task2 = kb.get_task(conn, tid2)
    kb._default_spawn(task2, "/tmp/ws", board="default")
    assert "HERMES_MAX_ITERATIONS" not in captured["env"]


def test_worker_cmd_passes_max_turns_flag(kanban_home, monkeypatch):
    """Per-task max_iterations must reach the worker as a ``--max-turns N``
    chat flag, not just the ``HERMES_MAX_ITERATIONS`` env var.

    The env var ALONE is a no-op in production: the worker resolves
    max_turns as "CLI arg > config > env > default" (cli.py:3052) and
    ``load_cli_config`` always injects ``agent.max_turns=90``, so config
    shadows the env var. The ``--max-turns`` chat flag hits the
    top-precedence CLI-arg branch (cli.py:3053) and actually beats the
    profile default — the whole point of the per-task override for
    audit-class tasks. Regression guard for the post-rebaseline gap where
    only the (shadowed) env var was injected.
    """
    captured: dict[str, object] = {}

    class _FakePopen:
        def __init__(self, cmd, env=None, **kwargs):
            captured["cmd"] = list(cmd)
            captured["env"] = dict(env or {})
            self.pid = 12345

        def wait(self, *a, **kw):
            return 0

    import subprocess
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda name: str(kanban_home),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.normalize_profile_name",
        lambda name: name,
    )

    # With per-task override: `--max-turns 150` must appear as a chat flag.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="audit", assignee="coder", max_iterations=150,
        )
        task = kb.get_task(conn, tid)
    kb._default_spawn(task, "/tmp/ws", board="default")
    cmd = captured["cmd"]
    assert "chat" in cmd, f"worker cmd has no chat subcommand: {cmd}"
    assert "--max-turns" in cmd, f"worker cmd missing --max-turns: {cmd}"
    flag_idx = cmd.index("--max-turns")
    assert cmd[flag_idx + 1] == "150", f"--max-turns value wrong: {cmd}"
    # Must be a chat-subcommand arg (after `chat`) so argparse routes it to
    # chat_parser.max_turns -> HermesCLI(max_turns=...), the branch that
    # outranks agent.max_turns from the profile config.
    assert flag_idx > cmd.index("chat"), f"--max-turns placed before chat: {cmd}"
    # Env var still injected for consistency / non-load_cli_config consumers.
    assert captured["env"].get("HERMES_MAX_ITERATIONS") == "150"

    captured.clear()

    # Without per-task override: no --max-turns flag (inherit profile default).
    with kb.connect() as conn:
        tid2 = kb.create_task(conn, title="default-budget", assignee="coder")
        task2 = kb.get_task(conn, tid2)
    kb._default_spawn(task2, "/tmp/ws", board="default")
    assert "--max-turns" not in captured["cmd"]


def test_worker_cmd_model_override_reaches_parser(kanban_home, monkeypatch):
    """End-to-end repro for the T4 / WI-6 fix: parse the dispatcher's worker
    argv with the REAL top-level parser and assert the per-task model override
    survives. Was RED while `-m` sat before `chat` (the chat subparser's
    default=None clobbered the top-level value); green now that `-m` is placed
    after `chat`.
    """
    captured: dict[str, object] = {}

    class _FakePopen:
        def __init__(self, cmd, env=None, **kwargs):
            captured["cmd"] = list(cmd)
            self.pid = 12345

        def wait(self, *a, **kw):
            return 0

    import subprocess
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda name: str(kanban_home),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.normalize_profile_name",
        lambda name: name,
    )

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="model-override", assignee="coder",
        )
        # model_override has no create_task kwarg — it's an escalation field
        # set on the row directly (mirrors how the dispatcher reads it).
        conn.execute(
            "UPDATE tasks SET model_override = ? WHERE id = ?",
            ("gpt-5.5-codex", tid),
        )
        task = kb.get_task(conn, tid)
    assert task.model_override == "gpt-5.5-codex"
    kb._default_spawn(task, "/tmp/ws", board="default")

    # Reconstruct the argv argparse actually sees: drop the executable (cmd[0])
    # and the `-p <profile>` pair (consumed pre-argparse by the profile override
    # handler), then parse with the real top-level parser.
    argv = list(captured["cmd"][1:])
    if "-p" in argv:
        i = argv.index("-p")
        del argv[i:i + 2]

    from hermes_cli._parser import build_top_level_parser
    parser, _subparsers, _chat = build_top_level_parser()
    ns = parser.parse_args(argv)
    assert ns.command == "chat"
    assert ns.model == "gpt-5.5-codex", (
        f"model_override lost: args.model={ns.model!r}; worker argv={argv}"
    )


def test_scout_budget_exhaustion_with_complete_handoff_completes_task(kanban_home):
    handoff = """CODER_HANDOFF:
- PATCH_TARGET: hermes_cli/kanban_db.py::record_iteration_budget_exhausted
- TEST_TARGET: tests/hermes_cli/test_kanban_iteration_budget.py
- AVOID: broad repo exploration after the handoff is complete
"""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Scout handoff recovery",
            assignee="scout",
            max_continuations=0,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        task = kb.get_task(conn, tid)
        assert task is not None
        run_id = task.current_run_id
        assert run_id is not None

        assert kb.record_iteration_budget_exhausted(
            conn,
            tid,
            summary=handoff,
            metadata={"source": "test"},
            expected_run_id=run_id,
        )

        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
        assert task is not None
        assert run is not None
        events = [e.kind for e in kb.list_events(conn, tid)]

    assert task.status == "done"
    assert task.result == handoff
    assert run.outcome == "completed"
    assert run.summary == handoff
    assert run.metadata["recovered_from"] == "iteration_budget_exhausted"
    assert "scout_handoff_recovered" in events
    assert "completed" in events
    assert "blocked" not in events
    assert "iteration_budget_exhausted" not in events


@pytest.mark.parametrize(
    "summary",
    [
        "CODER_HANDOFF: incomplete; missing PATCH_TARGET, TEST_TARGET and AVOID",
        """CODER_HANDOFF:
- PATCH_TARGET:
- TEST_TARGET: tests/hermes_cli/test_kanban_iteration_budget.py
- AVOID: broad repo exploration after the handoff is complete
""",
    ],
)
def test_scout_handoff_recovery_rejects_marker_mentions_without_values(
    kanban_home, summary
):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Scout incomplete handoff",
            assignee="scout",
            max_continuations=0,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        claimed_task = kb.get_task(conn, tid)
        assert claimed_task is not None
        run_id = claimed_task.current_run_id
        assert run_id is not None

        assert kb.record_iteration_budget_exhausted(
            conn,
            tid,
            summary=summary,
            expected_run_id=run_id,
        )

        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
        events = [e.kind for e in kb.list_events(conn, tid)]

    assert task is not None
    assert run is not None
    assert task.status != "done"
    assert run.outcome != "completed"
    assert "scout_handoff_recovered" not in events


def test_scout_handoff_recovery_rejects_template_placeholder_values(kanban_home):
    summary = """CODER_HANDOFF:
- PATCH_TARGET: erste Datei/Symbole, die der Coder ändern soll.
- TEST_TARGET: engste Tests/Gates, die den Patch beweisen.
- AVOID: Pfade/Ansätze, die Tokens verbrennen oder Arbeit doppeln.
"""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Scout template-only handoff",
            assignee="scout",
            max_continuations=0,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        claimed_task = kb.get_task(conn, tid)
        assert claimed_task is not None
        run_id = claimed_task.current_run_id
        assert run_id is not None

        assert kb.record_iteration_budget_exhausted(
            conn,
            tid,
            summary=summary,
            expected_run_id=run_id,
        )

        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
        events = [e.kind for e in kb.list_events(conn, tid)]

    assert task is not None
    assert run is not None
    assert task.status != "done"
    assert run.outcome != "completed"
    assert "scout_handoff_recovered" not in events


def test_record_task_failure_does_not_complete_reclaimed_scout_run(kanban_home):
    handoff = """CODER_HANDOFF:
- PATCH_TARGET: hermes_cli/kanban_db.py::_record_task_failure expected_run_id guard
- TEST_TARGET: tests/hermes_cli/test_kanban_iteration_budget.py
- AVOID: completing a newer reclaimed run from a stale finalizer
"""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Scout stale finalizer",
            assignee="scout",
            max_continuations=0,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        first_task = kb.get_task(conn, tid)
        assert first_task is not None
        stale_run_id = first_task.current_run_id
        assert stale_run_id is not None

        conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, claim_lock, claim_expires, started_at, "
            "last_heartbeat_at, max_runtime_seconds) "
            "VALUES (?, ?, 'running', 'reclaimed', 9999999999, 123, 123, 3600)",
            (tid, "scout"),
        )
        new_run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "UPDATE tasks SET current_run_id = ?, claim_lock = 'reclaimed' "
            "WHERE id = ?",
            (new_run_id, tid),
        )

        assert not kb._record_task_failure(
            conn,
            tid,
            "Iteration budget exhausted (20/20)",
            outcome="timed_out",
            release_claim=True,
            end_run=True,
            summary=handoff,
            expected_run_id=stale_run_id,
        )

        task = kb.get_task(conn, tid)
        run = conn.execute(
            "SELECT id, status, outcome FROM task_runs WHERE id = ?",
            (new_run_id,),
        ).fetchone()
        events = [e.kind for e in kb.list_events(conn, tid)]

    assert task is not None
    assert run is not None
    assert task.status == "running"
    assert task.current_run_id == new_run_id
    assert run["id"] == new_run_id
    assert run["status"] == "running"
    assert run["outcome"] is None
    assert "scout_handoff_recovered" not in events
    assert "timed_out" not in events


def test_turn_finalizer_recovers_scout_handoff_from_budget_exhaustion(
    kanban_home, monkeypatch
):
    handoff = """CODER_HANDOFF:
- PATCH_TARGET: agent/turn_finalizer.py::_record_task_failure budget path
- TEST_TARGET: tests/hermes_cli/test_kanban_iteration_budget.py
- AVOID: blocking scout tasks after a complete handoff summary exists
"""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="finalizer scout handoff recovery",
            assignee="scout",
            max_iterations=1,
            max_continuations=0,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    class _BudgetExhaustedScoutAgent:
        max_iterations = 1
        model = "fake-model"
        provider = "fake-provider"
        base_url = "http://fake-provider.invalid"
        session_id = "scout-budget-session"
        quiet_mode = True
        session_input_tokens = 0
        session_output_tokens = 0
        session_cache_read_tokens = 0
        session_cache_write_tokens = 0
        session_reasoning_tokens = 0
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0
        session_estimated_cost_usd = 0.0
        session_cost_status = "ok"
        session_cost_source = "test"
        _tool_guardrail_halt_decision = None
        _response_was_previewed = False
        _skill_nudge_interval = 0
        _iters_since_skill = 0
        valid_tool_names = set()
        context_compressor = SimpleNamespace(last_prompt_tokens=0)

        def __init__(self):
            self.iteration_budget = IterationBudget(1)
            assert self.iteration_budget.consume()

        def _emit_status(self, _msg):
            pass

        def _safe_print(self, *_args, **_kwargs):
            pass

        def _handle_max_iterations(self, _messages, _api_call_count):
            return handoff

        def _save_trajectory(self, *_args, **_kwargs):
            pass

        def _cleanup_task_resources(self, *_args, **_kwargs):
            pass

        def _drop_trailing_empty_response_scaffolding(self, _messages):
            pass

        def _persist_session(self, *_args, **_kwargs):
            pass

        def _file_mutation_verifier_enabled(self):
            return False

        def _turn_completion_explainer_enabled(self):
            return False

        def _drain_pending_steer(self):
            return None

        def clear_interrupt(self):
            pass

        def _sync_external_memory_for_turn(self, **_kwargs):
            pass

    result = finalize_turn(
        _BudgetExhaustedScoutAgent(),
        final_response=None,
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "scout the task"}],
        conversation_history=[],
        effective_task_id=tid,
        turn_id="turn-scout-budget",
        user_message="scout the task",
        original_user_message="scout the task",
        _should_review_memory=False,
        _turn_exit_reason="budget_exhausted",
    )

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
        events = [e.kind for e in kb.list_events(conn, tid)]

    assert result["final_response"] == handoff
    assert task is not None
    assert run is not None
    assert task.status == "done"
    assert task.result == handoff
    assert run.outcome == "completed"
    assert run.summary == handoff
    assert run.metadata["recovered_from"] == "timed_out"
    assert "scout_handoff_recovered" in events
    assert "timed_out" not in events
    assert "blocked" not in events


def test_budget_finalizer_honors_terminal_kanban_complete(kanban_home, monkeypatch):
    """A kanban worker can spend its final allowed model turn on
    ``kanban_complete``. The post-loop budget finalizer must not overwrite that
    terminal DB state with a synthetic timed_out/gave_up failure just because no
    follow-up prose turn was available.
    """

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="complete-at-budget-edge",
            assignee="coder",
            max_iterations=1,
            max_continuations=0,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        assert claimed.id == tid
        claimed_task = kb.get_task(conn, tid)
        assert claimed_task is not None
        run_id = claimed_task.current_run_id
        assert run_id is not None
        assert kb.complete_task(
            conn,
            tid,
            summary="deliverable complete at budget edge",
            expected_run_id=run_id,
        )

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    class _BudgetEdgeAgent:
        max_iterations = 1
        model = "fake-model"
        provider = "fake-provider"
        base_url = "http://fake-provider.invalid"
        session_id = "budget-edge-session"
        quiet_mode = True
        session_input_tokens = 0
        session_output_tokens = 0
        session_cache_read_tokens = 0
        session_cache_write_tokens = 0
        session_reasoning_tokens = 0
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0
        session_estimated_cost_usd = 0.0
        session_cost_status = "ok"
        session_cost_source = "test"
        _tool_guardrail_halt_decision = None
        _response_was_previewed = False
        _skill_nudge_interval = 0
        _iters_since_skill = 0
        valid_tool_names = {"kanban_complete"}
        context_compressor = SimpleNamespace(last_prompt_tokens=0)

        def __init__(self):
            self.iteration_budget = IterationBudget(1)
            assert self.iteration_budget.consume()
            self.summary_requested = False

        def _emit_status(self, _msg):
            pass

        def _safe_print(self, *_args, **_kwargs):
            pass

        def _handle_max_iterations(self, _messages, _api_call_count):
            self.summary_requested = True
            return "budget summary should not be requested after completion"

        def _save_trajectory(self, *_args, **_kwargs):
            pass

        def _cleanup_task_resources(self, *_args, **_kwargs):
            pass

        def _drop_trailing_empty_response_scaffolding(self, _messages):
            pass

        def _persist_session(self, *_args, **_kwargs):
            pass

        def _file_mutation_verifier_enabled(self):
            return False

        def _turn_completion_explainer_enabled(self):
            return False

        def _drain_pending_steer(self):
            return None

        def clear_interrupt(self):
            pass

        def _sync_external_memory_for_turn(self, **_kwargs):
            pass

    agent = _BudgetEdgeAgent()
    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=[
            {"role": "user", "content": "finish the task"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_complete",
                        "function": {
                            "name": "kanban_complete",
                            "arguments": (
                                '{"summary": "deliverable complete at budget edge"}'
                            ),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "name": "kanban_complete",
                "tool_call_id": "call_complete",
                "content": '{"ok": true}',
            },
        ],
        conversation_history=[],
        effective_task_id=tid,
        turn_id="turn-budget-edge",
        user_message="finish the task",
        original_user_message="finish the task",
        _should_review_memory=False,
        _turn_exit_reason="budget_exhausted",
    )

    assert result["completed"] is True
    assert agent.summary_requested is False

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
        events = [e.kind for e in kb.list_events(conn, tid)]
    assert task.status == "done"
    assert run.outcome == "completed"
    assert "completed" in events
    assert "blocked" not in events
    assert "iteration_budget_exhausted" not in events
