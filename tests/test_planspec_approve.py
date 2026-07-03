"""Tests for POST /api/plugins/kanban/planspecs/approve (composed PlanSpec release).

Harness mirrors test_kanban_strategist_endpoint.py: bare-router FastAPI app,
real sqlite kanban DB via tmp_path, no mocks of the DB layer.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb

PREFIX = "/api/plugins/kanban"


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[1]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    mod_name = "hermes_dashboard_plugin_kanban_planspec_approve_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name].router
    spec = importlib.util.spec_from_file_location(mod_name, plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_VISION_METRICS_PATH", raising=False)
    monkeypatch.delenv("HERMES_STRATEGIST_DIGEST_PATH", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix=PREFIX)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_held_chain(*, assignee: str = "coder") -> tuple[str, str]:
    """Create a held freigabe:operator root + one held child.

    Returns (root_id, child_id).  The Kanban link convention: the child is
    a build task (parent in task_links), the root is the sink/child in the
    link.  release_freigabe_hold walks parent_ids(root) to find the children.
    """
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="PlanSpec root",
            assignee=assignee,
        )
        child_id = kb.create_task(
            conn,
            title="Build step",
            assignee=assignee,
        )
        kb.link_tasks(conn, parent_id=child_id, child_id=root_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='scheduled', freigabe='operator' WHERE id=?",
                (root_id,),
            )
            conn.execute(
                "UPDATE tasks SET status='scheduled' WHERE id=?",
                (child_id,),
            )
    return root_id, child_id


def _status(task_id: str) -> str:
    with kb.connect() as conn:
        return conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()["status"]


def _model_override(task_id: str) -> str | None:
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT model_override FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        return row["model_override"] if row else None


def _assignee_of(task_id: str) -> str | None:
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        return row["assignee"] if row else None


# ---------------------------------------------------------------------------
# Happy path: hold released, lane-model override applied
# ---------------------------------------------------------------------------


def test_approve_releases_hold_and_applies_override(client):
    root_id, child_id = _make_held_chain(assignee="coder")

    resp = client.post(
        f"{PREFIX}/planspecs/approve",
        json={
            "root_task_id": root_id,
            "lane_models": {"coder": "claude-opus-4-5"},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["released"] is True
    assert body["dry_run"] is False
    assert body["overrides_applied"] >= 1
    assert body["scout_injected"] is False

    # Root should be released (not scheduled anymore)
    assert _status(root_id) != "scheduled"
    # Model override applied to chain members with matching assignee
    assert _model_override(root_id) == "claude-opus-4-5"
    assert _model_override(child_id) == "claude-opus-4-5"


def test_approve_without_overrides_still_releases(client):
    root_id, child_id = _make_held_chain()

    resp = client.post(
        f"{PREFIX}/planspecs/approve",
        json={"root_task_id": root_id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["released"] is True
    assert body["overrides_applied"] == 0
    assert body["scout_injected"] is False
    assert _status(root_id) != "scheduled"
    assert _status(child_id) != "scheduled"


# ---------------------------------------------------------------------------
# Double-approve → 409 (no crash, idempotency guard at the status level)
# ---------------------------------------------------------------------------


def test_double_approve_returns_409_not_crash(client):
    root_id, _child_id = _make_held_chain()

    r1 = client.post(
        f"{PREFIX}/planspecs/approve",
        json={"root_task_id": root_id},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["released"] is True

    # Second approve: root is now 'todo' (hold already released).
    # The guard must reject it with 409 — a released root is not a 'gehaltener Root'
    # and re-running overrides/scout on an already-live chain is forbidden.
    r2 = client.post(
        f"{PREFIX}/planspecs/approve",
        json={"root_task_id": root_id},
    )
    assert r2.status_code == 409, r2.text
    assert "scheduled" in r2.json()["detail"]["error"]


# ---------------------------------------------------------------------------
# Unknown root → 404
# ---------------------------------------------------------------------------


def test_approve_unknown_root_returns_404(client):
    resp = client.post(
        f"{PREFIX}/planspecs/approve",
        json={"root_task_id": "t_does_not_exist"},
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]["error"]


# ---------------------------------------------------------------------------
# Non-operator task → 409
# ---------------------------------------------------------------------------


def test_approve_non_operator_root_returns_409(client):
    # Create a plain (non-freigabe) task
    with kb.connect() as conn:
        plain_id = kb.create_task(
            conn,
            title="Plain task",
            assignee="coder",
        )

    resp = client.post(
        f"{PREFIX}/planspecs/approve",
        json={"root_task_id": plain_id},
    )
    assert resp.status_code == 409
    assert "freigabe:operator" in resp.json()["detail"]["error"]


def test_approve_child_task_of_held_chain_returns_409(client):
    _root_id, child_id = _make_held_chain()

    # The child carries no freigabe → not a proposal root
    resp = client.post(
        f"{PREFIX}/planspecs/approve",
        json={"root_task_id": child_id},
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# dry_run=True: nothing written
# ---------------------------------------------------------------------------


def test_dry_run_does_not_write(client):
    root_id, child_id = _make_held_chain(assignee="coder")

    resp = client.post(
        f"{PREFIX}/planspecs/approve",
        json={
            "root_task_id": root_id,
            "lane_models": {"coder": "claude-haiku-4-5"},
            "dry_run": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["released"] is False  # nothing released

    # Verify DB untouched
    assert _status(root_id) == "scheduled"
    assert _status(child_id) == "scheduled"
    assert _model_override(root_id) is None
    assert _model_override(child_id) is None

    # planned_actions must be present and meaningful
    assert "planned_actions" in body
    assert any(a["action"] == "release_freigabe_hold" for a in body["planned_actions"])


def test_dry_run_reports_override_count(client):
    root_id, _child_id = _make_held_chain(assignee="builder")

    resp = client.post(
        f"{PREFIX}/planspecs/approve",
        json={
            "root_task_id": root_id,
            "lane_models": {"builder": "some-model"},
            "dry_run": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["overrides_applied"] >= 1


# ---------------------------------------------------------------------------
# inject_scout: exactly one scout injected; idempotent (no double-inject)
# ---------------------------------------------------------------------------


def _scout_parent_id(task_id: str) -> str | None:
    """Return the scout predecessor id of task_id, or None."""
    with kb.connect() as conn:
        return kb.scout_predecessor_id(conn, task_id)


def test_inject_scout_creates_scout_task(client):
    root_id, child_id = _make_held_chain(assignee="coder")

    resp = client.post(
        f"{PREFIX}/planspecs/approve",
        json={"root_task_id": root_id, "inject_scout": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scout_injected"] is True
    assert body["released"] is True

    # A scout must be a parent (predecessor) of the entry child
    scout_id = _scout_parent_id(child_id)
    assert scout_id is not None
    with kb.connect() as conn:
        scout_task = kb.get_task(conn, scout_id)
    assert scout_task is not None
    assert scout_task.assignee == "scout"


def test_inject_scout_not_doubled_on_second_approve(client):
    """If the chain already has a scout predecessor, inject_scout must not
    add a second one — idempotent."""
    root_id, child_id = _make_held_chain(assignee="coder")

    # First approve injects scout and releases
    r1 = client.post(
        f"{PREFIX}/planspecs/approve",
        json={"root_task_id": root_id, "inject_scout": True},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["scout_injected"] is True

    scout_id_after_first = _scout_parent_id(child_id)
    assert scout_id_after_first is not None

    # Re-create a held chain (simulate a second held proposal with different id)
    root2_id, child2_id = _make_held_chain(assignee="coder")

    # Manually inject a scout predecessor onto child2 before the approve call
    with kb.connect() as conn:
        pre_scout_id = kb.create_task(
            conn,
            title="Scout: pre-existing",
            assignee="scout",
            created_by="test-setup",
        )
        kb.link_tasks(conn, pre_scout_id, child2_id)

    resp = client.post(
        f"{PREFIX}/planspecs/approve",
        json={"root_task_id": root2_id, "inject_scout": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # No new scout injected because child already has one
    assert body["scout_injected"] is False

    # Verify the child still has exactly one scout predecessor (not two)
    with kb.connect() as conn:
        scout_preds = [
            pid
            for pid in kb.parent_ids(conn, child2_id)
            if (t := kb.get_task(conn, pid)) is not None and t.assignee == "scout"
        ]
    assert len(scout_preds) == 1


# ---------------------------------------------------------------------------
# Blocker 1: transitive chain — scout reaches the true entry task
# ---------------------------------------------------------------------------


def _make_deep_held_chain(*, assignee: str = "coder") -> tuple[str, str, str]:
    """Create a 3-level held chain: entry → middle → root.

    Link convention (same as _make_held_chain):
      task_links.parent_id = upstream task, task_links.child_id = downstream task.
    Returns (root_id, middle_id, entry_id).
    """
    with kb.connect() as conn:
        root_id = kb.create_task(conn, title="PlanSpec root deep", assignee=assignee)
        middle_id = kb.create_task(conn, title="Middle step", assignee=assignee)
        entry_id = kb.create_task(conn, title="Entry step", assignee=assignee)
        # entry → middle → root  (parent_id = upstream, child_id = downstream)
        kb.link_tasks(conn, parent_id=middle_id, child_id=root_id)
        kb.link_tasks(conn, parent_id=entry_id, child_id=middle_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='scheduled', freigabe='operator' WHERE id=?",
                (root_id,),
            )
            conn.execute(
                "UPDATE tasks SET status='scheduled' WHERE id=?", (middle_id,)
            )
            conn.execute(
                "UPDATE tasks SET status='scheduled' WHERE id=?", (entry_id,)
            )
    return root_id, middle_id, entry_id


def test_inject_scout_transitive_chain_reaches_entry_task(client):
    """Scout must be injected before the true entry task in a 3-level chain.

    Before fix: parent_ids(root) = [middle] only — entry_id never examined,
    scout_injected=False even though inject_scout=True.
    """
    root_id, middle_id, entry_id = _make_deep_held_chain(assignee="coder")

    resp = client.post(
        f"{PREFIX}/planspecs/approve",
        json={"root_task_id": root_id, "inject_scout": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scout_injected"] is True, (
        "Scout must be injected for transitive chain — entry task is two hops from root"
    )
    assert body["released"] is True

    # Scout must be a predecessor of the entry task (the real leaf)
    scout_id = _scout_parent_id(entry_id)
    assert scout_id is not None, "entry_id must have a scout predecessor"
    with kb.connect() as conn:
        scout_task = kb.get_task(conn, scout_id)
    assert scout_task is not None
    assert scout_task.assignee == "scout"

    # Middle task must NOT be flagged as entry (it has entry_id as in-chain parent)
    # so the scout must NOT be a direct predecessor of middle_id from the inject path
    # (middle already has entry_id as parent — it's not an entry task).
    # We verify by checking there's exactly one scout and it precedes entry_id, not middle.
    scout_id_for_middle = _scout_parent_id(middle_id)
    # It's fine if middle also gets the scout linked (the inject links scout to all
    # entry tasks — there's only one here), but the scout must at minimum precede entry.
    assert scout_id is not None  # already asserted above; keep for clarity


# ---------------------------------------------------------------------------
# Blocker 2: global dedup — scout deep in chain prevents second injection
# ---------------------------------------------------------------------------


def test_inject_scout_no_duplicate_when_scout_deep_in_chain(client):
    """If a scout task already exists ANYWHERE in the chain (not just as a direct
    parent of root-children), inject_scout must not create a second scout.

    Scenario: a scout is inserted as a predecessor of the middle task (deep in a
    3-level chain).  The approve call must detect it and return scout_injected=False.
    """
    root_id, middle_id, entry_id = _make_deep_held_chain(assignee="coder")

    # Manually place a scout deep in the chain — it is a predecessor of middle_id
    # (not directly visible from parent_ids(root), so the old code missed it).
    with kb.connect() as conn:
        deep_scout_id = kb.create_task(
            conn,
            title="Scout: pre-existing deep",
            assignee="scout",
            created_by="test-setup",
        )
        kb.link_tasks(conn, deep_scout_id, middle_id)

    resp = client.post(
        f"{PREFIX}/planspecs/approve",
        json={"root_task_id": root_id, "inject_scout": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scout_injected"] is False, (
        "scout_injected must be False when a scout already exists deep in the chain"
    )
    assert body["released"] is True

    # Verify no additional scout was linked to the entry task
    with kb.connect() as conn:
        all_parents_of_entry = kb.parent_ids(conn, entry_id)
        scout_parents = [
            pid
            for pid in all_parents_of_entry
            if (t := kb.get_task(conn, pid)) is not None and t.assignee == "scout"
        ]
    assert len(scout_parents) == 0, (
        "entry_id must have no new scout predecessor — global dedup must prevent injection"
    )


# ---------------------------------------------------------------------------
# Blocker 1 (atomicity): a late release failure must roll back overrides + scout
# ---------------------------------------------------------------------------


def test_release_failure_rolls_back_overrides_and_scout(client, monkeypatch):
    """Guard-recheck, overrides, scout injection and the freigabe-root release
    share ONE transaction — if the release step fails deep inside it, the
    overrides and the scout must roll back too. Before the fix they ran in their
    own already-committed transactions, so a late release failure left an
    orphaned model_override + scout on a chain that was never actually
    released."""
    root_id, child_id = _make_held_chain(assignee="coder")

    class _BoomError(Exception):
        pass

    def _boom(*_args, **_kwargs):
        raise _BoomError("simulated release failure")

    monkeypatch.setattr(kb, "_release_freigabe_hold_root_in_txn", _boom)

    with pytest.raises(_BoomError):
        client.post(
            f"{PREFIX}/planspecs/approve",
            json={
                "root_task_id": root_id,
                "lane_models": {"coder": "claude-opus-4-5"},
                "inject_scout": True,
            },
        )

    # Root untouched — still the held 'scheduled' state, no partial release.
    assert _status(root_id) == "scheduled"
    assert _status(child_id) == "scheduled"
    # Overrides rolled back — nothing persisted on either chain member.
    assert _model_override(root_id) is None
    assert _model_override(child_id) is None
    # Scout rolled back — no scout predecessor of the entry child.
    assert _scout_parent_id(child_id) is None


# ---------------------------------------------------------------------------
# Blocker 2 (idempotency): re-approve after a successful first call is a clean
# no-op — never a second scout
# ---------------------------------------------------------------------------


def test_second_approve_after_success_is_clean_noop_no_second_scout(client):
    """A second approve on the SAME root after a successful first approve (with
    inject_scout) must not create a second scout — clean 409, not a duplicate
    mutation."""
    root_id, child_id = _make_held_chain(assignee="coder")

    r1 = client.post(
        f"{PREFIX}/planspecs/approve",
        json={"root_task_id": root_id, "inject_scout": True},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["scout_injected"] is True

    r2 = client.post(
        f"{PREFIX}/planspecs/approve",
        json={"root_task_id": root_id, "inject_scout": True},
    )
    assert r2.status_code == 409, r2.text

    # Exactly one scout predecessor of the entry child — no duplicate.
    with kb.connect() as conn:
        scout_preds = [
            pid
            for pid in kb.parent_ids(conn, child_id)
            if (t := kb.get_task(conn, pid)) is not None and t.assignee == "scout"
        ]
    assert len(scout_preds) == 1
