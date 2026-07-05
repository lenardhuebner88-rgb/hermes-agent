"""K8 / D7 L2 — native workflow-template routing (coder -> reviewer -> critic).

Covers:
  * the fail-soft template loader (``hermes_cli.kanban_workflows``);
  * ``dispatch_once`` routing a workflow task by its CURRENT STEP rather than
    the static ``assignee`` column, and persisting the step's role;
  * ``complete_task`` advancing ``current_step_key`` to the next step (back to
    ``ready``, re-assigned) instead of going straight to ``done`` — until the
    final step, which completes normally;
  * the HARD regression guard: a task WITHOUT ``workflow_template_id`` behaves
    byte-identically to today (straight to ``done``, ``completed`` event, no
    workflow event, no workflow-sourced ``assigned`` event).
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import types
from pathlib import Path

import pytest


def _is_purgeable_hermes_module(name: str) -> bool:
    return (
        name.startswith("hermes_cli")
        or name.startswith("hermes_state")
        or name == "hermes_constants"
    )


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Fresh HERMES_HOME + a dedicated workflows dir, modules reimported."""
    test_home = tempfile.mkdtemp(prefix="kanban_wf_routing_test_")
    wf_dir = tempfile.mkdtemp(prefix="kanban_wf_templates_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    monkeypatch.setenv("HERMES_KANBAN_WORKFLOWS_DIR", wf_dir)
    # Purge so the modules re-import against the fresh HERMES_HOME, then restore
    # the original module objects on teardown (an unrestored purge causes a
    # module-identity split that contaminates later test files).
    saved = {
        name: mod for name, mod in sys.modules.items()
        if _is_purgeable_hermes_module(name)
    }
    for name in saved:
        del sys.modules[name]
    from hermes_cli import kanban_db, kanban_workflows

    # Every dispatch route resolves through profile_exists; in a bare test
    # HERMES_HOME no profiles exist, so pretend they all do (we never spawn a
    # real worker — spawn_fn is a stub).
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    kanban_workflows.clear_workflow_cache()
    try:
        yield kanban_db, kanban_workflows, Path(wf_dir)
    finally:
        kanban_workflows.clear_workflow_cache()
        for name in [n for n in sys.modules if _is_purgeable_hermes_module(n)]:
            del sys.modules[name]
        sys.modules.update(saved)
        shutil.rmtree(test_home, ignore_errors=True)
        shutil.rmtree(wf_dir, ignore_errors=True)


def _write_template(wf_dir: Path, template_id: str, body: str) -> None:
    (wf_dir / f"{template_id}.yaml").write_text(body, encoding="utf-8")


_THREE_STEP = """\
steps:
  - key: code
    assignee: coder
  - key: review
    assignee: reviewer
  - key: critique
    assignee: critic
"""


def _make_workflow_task(kb, conn, *, template_id, step_key, title="wf task"):
    """Create a ready task opted into a workflow template at *step_key*."""
    task_id = kb.create_task(conn, title=title, assignee=None)
    conn.execute(
        "UPDATE tasks SET status = 'ready', workflow_template_id = ?, "
        "current_step_key = ? WHERE id = ?",
        (template_id, step_key, task_id),
    )
    conn.commit()
    return task_id


# ---------------------------------------------------------------------------
# Loader — happy path + fail-soft
# ---------------------------------------------------------------------------

def test_loader_parses_ordered_steps(isolated_kanban_home):
    _kb, wf, wf_dir = isolated_kanban_home
    _write_template(wf_dir, "crc", _THREE_STEP)
    tmpl = wf.load_workflow_template("crc")
    assert tmpl is not None
    assert [s.key for s in tmpl.steps] == ["code", "review", "critique"]
    assert tmpl.assignee_for("code") == "coder"
    assert tmpl.assignee_for("review") == "reviewer"
    assert tmpl.first_step_key() == "code"
    assert tmpl.next_step_key("code") == "review"
    assert tmpl.next_step_key("review") == "critique"
    # Final step has no successor.
    assert tmpl.next_step_key("critique") is None
    # Unknown step → no successor (treated as "complete", never stranded).
    assert tmpl.next_step_key("nope") is None
    assert tmpl.assignee_for("nope") is None


def test_loader_missing_file_is_none(isolated_kanban_home):
    _kb, wf, _wf_dir = isolated_kanban_home
    assert wf.load_workflow_template("does-not-exist") is None
    assert wf.load_workflow_template("") is None
    assert wf.load_workflow_template(None) is None


@pytest.mark.parametrize(
    "body",
    [
        "just a string",                       # not a mapping
        "steps: []",                            # empty steps
        "steps:\n  - key: code\n",              # step missing assignee
        "steps:\n  - assignee: coder\n",        # step missing key
        "steps:\n  - key: ''\n    assignee: x", # blank key
        "steps:\n  - key: a\n    assignee: x\n  - key: a\n    assignee: y",  # dup key
        "{ : : :",                              # invalid YAML
    ],
)
def test_loader_malformed_is_none(isolated_kanban_home, body):
    _kb, wf, wf_dir = isolated_kanban_home
    _write_template(wf_dir, "bad", body)
    wf.clear_workflow_cache()
    assert wf.load_workflow_template("bad") is None


# ---------------------------------------------------------------------------
# Dispatch routing by current step
# ---------------------------------------------------------------------------

def test_dispatch_routes_by_current_step(isolated_kanban_home):
    kb, _wf, wf_dir = isolated_kanban_home
    _write_template(wf_dir, "crc", _THREE_STEP)
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = _make_workflow_task(
            conn=conn, kb=kb, template_id="crc", step_key="code"
        )

    spawned_as: list[str] = []

    def _spawn(task, ws, **kw):
        spawned_as.append(task.assignee)
        return 4321

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn, dry_run=False)

    # Routed to the CURRENT step's role (coder), not the column (which was NULL).
    assert spawned_as == ["coder"]
    assert any(t[0] == task_id and t[1] == "coder" for t in res.spawned)
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT assignee, current_step_key FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    # The resolved role was persisted to the column for board consistency.
    assert row["assignee"] == "coder"
    assert row["current_step_key"] == "code"
    # An 'assigned' event records the workflow-sourced routing.
    with kb.connect_closing() as conn:
        kinds = [
            r["payload"]
            for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'assigned'",
                (task_id,),
            )
        ]
    assert any("workflow_step" in (p or "") for p in kinds)


# ---------------------------------------------------------------------------
# Complete advances through the chain; final step completes
# ---------------------------------------------------------------------------

def test_complete_advances_through_chain(isolated_kanban_home):
    kb, _wf, wf_dir = isolated_kanban_home
    _write_template(wf_dir, "crc", _THREE_STEP)
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = _make_workflow_task(
            conn=conn, kb=kb, template_id="crc", step_key="code"
        )

    # Step 1 (code) completes -> advances to review, NOT done.
    with kb.connect_closing() as conn:
        ok = kb.complete_task(conn, task_id, summary="coded it")
        assert ok is True
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status, current_step_key, assignee FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    assert row["status"] == "ready"
    assert row["current_step_key"] == "review"
    assert row["assignee"] == "reviewer"

    # Step 2 (review) completes -> advances to critique.
    with kb.connect_closing() as conn:
        assert kb.complete_task(conn, task_id, summary="reviewed it") is True
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status, current_step_key, assignee FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    assert row["status"] == "ready"
    assert row["current_step_key"] == "critique"
    assert row["assignee"] == "critic"

    # Step 3 (critique, final) completes -> DONE.
    with kb.connect_closing() as conn:
        assert kb.complete_task(conn, task_id, summary="critiqued it") is True
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        kinds = [
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
            )
        ]
    assert row["status"] == "done"
    # Two advance events (code->review, review->critique) then a completion.
    assert kinds.count("workflow_step_advanced") == 2
    assert "completed" in kinds


def test_dispatch_then_complete_full_loop(isolated_kanban_home):
    """End-to-end: each step is dispatched to the right role, then advances."""
    kb, _wf, wf_dir = isolated_kanban_home
    _write_template(wf_dir, "crc", _THREE_STEP)
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = _make_workflow_task(
            conn=conn, kb=kb, template_id="crc", step_key="code"
        )

    spawned_as: list[str] = []

    def _spawn(task, ws, **kw):
        spawned_as.append(task.assignee)
        return 4321

    for expected_role, summary in [
        ("coder", "coded"),
        ("reviewer", "reviewed"),
        ("critic", "critiqued"),
    ]:
        with kb.connect_closing() as conn:
            kb.dispatch_once(conn, spawn_fn=_spawn, dry_run=False)
        # The worker (claimed -> running) completes its turn.
        with kb.connect_closing() as conn:
            assert kb.complete_task(conn, task_id, summary=summary) is True

    assert spawned_as == ["coder", "reviewer", "critic"]
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    assert row["status"] == "done"


# ---------------------------------------------------------------------------
# HARD regression guard: no template => byte-identical legacy behaviour
# ---------------------------------------------------------------------------

def test_no_template_completes_straight_to_done(isolated_kanban_home):
    kb, _wf, _wf_dir = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="plain", assignee="coder")
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,)
        )
        conn.commit()

    spawned_as: list[str] = []

    def _spawn(task, ws, **kw):
        spawned_as.append(task.assignee)
        return 99

    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_spawn, dry_run=False)
    # Routed by the column assignee, untouched.
    assert spawned_as == ["coder"]

    with kb.connect_closing() as conn:
        assert kb.complete_task(conn, task_id, summary="done") is True
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status, current_step_key, workflow_template_id "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        kinds = [
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
            )
        ]
        assigned_payloads = [
            r["payload"]
            for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'assigned'",
                (task_id,),
            )
        ]
    # Straight to done — no workflow machinery touched this task.
    assert row["status"] == "done"
    assert row["workflow_template_id"] is None
    assert row["current_step_key"] is None
    assert "workflow_step_advanced" not in kinds
    assert "completed" in kinds
    assert not any("workflow_step" in (p or "") for p in assigned_payloads)


def test_broken_template_falls_back_to_column_assignee(isolated_kanban_home):
    """Fail-soft: a task points at a missing template -> legacy routing."""
    kb, _wf, _wf_dir = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="wf", assignee="coder")
        conn.execute(
            "UPDATE tasks SET status = 'ready', workflow_template_id = ?, "
            "current_step_key = ? WHERE id = ?",
            ("ghost-template", "code", task_id),
        )
        conn.commit()

    spawned_as: list[str] = []

    def _spawn(task, ws, **kw):
        spawned_as.append(task.assignee)
        return 7

    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_spawn, dry_run=False)
    # Missing template -> column assignee used.
    assert spawned_as == ["coder"]

    # Completion of a task whose template can't be resolved -> done (no stall).
    with kb.connect_closing() as conn:
        assert kb.complete_task(conn, task_id, summary="x") is True
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    assert row["status"] == "done"


# ---------------------------------------------------------------------------
# CLI verb: `kanban set-workflow <task_id> <template_id>`
# ---------------------------------------------------------------------------

def test_set_workflow_seeds_template_and_first_step(isolated_kanban_home):
    kb, _wf, wf_dir = isolated_kanban_home
    _write_template(wf_dir, "crc", _THREE_STEP)
    from hermes_cli import kanban

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="plain", assignee=None)
        conn.commit()

    args = types.SimpleNamespace(task_id=task_id, template_id="crc")
    rc = kanban._cmd_set_workflow(args)
    assert rc == 0

    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT workflow_template_id, current_step_key FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    # Both columns written atomically: template id + its first step.
    assert row["workflow_template_id"] == "crc"
    assert row["current_step_key"] == "code"


def test_set_workflow_unknown_template_no_partial_seed(isolated_kanban_home):
    kb, _wf, _wf_dir = isolated_kanban_home
    from hermes_cli import kanban

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="plain", assignee=None)
        conn.commit()

    args = types.SimpleNamespace(task_id=task_id, template_id="does-not-exist")
    rc = kanban._cmd_set_workflow(args)
    # Fail-soft: non-zero exit, and NEITHER column touched (no partial seed).
    assert rc != 0
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT workflow_template_id, current_step_key FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    assert row["workflow_template_id"] is None
    assert row["current_step_key"] is None
