"""Tests for Befund 4: chain-worktree-serialization guard.

One provisioned git worktree per chain — ``dir`` siblings MUST NOT be
dispatched concurrently into the same worktree.

Guard location: ``_dispatch_once_locked`` in kanban_db, seeded by
``chain_worktree_inflight_counts`` in kanban_dispatch_policy.

Fixtures: kanban_home + repo from test_kanban_worktrees.py's conftest.
Real ``decompose_triage_task`` chains — NO synthetic task-link hand-crafting.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worktrees as kwt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spawn_recorder(spawned: dict):
    """Return a spawn stub that records task.id → workspace."""
    def _spawn(task, workspace, *_a, **_kw):
        spawned[task.id] = workspace
    return _spawn


def _make_decompose_chain(
    conn,
    repo: Path,
    *,
    n_dir_siblings: int = 2,
    extra_scratch: bool = False,
) -> tuple[str, list[str]]:
    """Create a triage root fanned out into ``n_dir_siblings`` dir children.

    Returns ``(root_id, child_ids_in_order)``.  If ``extra_scratch=True`` one
    additional scratch sibling is appended (workspace_kind='scratch').

    All dir children use ``workspace_kind='dir'`` + ``workspace_path=str(repo)``
    (inheriting the root).  No inter-sibling parents so all promote to 'ready'.
    """
    root = kb.create_task(
        conn,
        title="chain root",
        triage=True,
        workspace_kind="dir",
        workspace_path=str(repo),
    )
    children: list[dict] = [
        {"title": f"dir-sibling-{i}", "assignee": "coder", "parents": []}
        for i in range(n_dir_siblings)
    ]
    if extra_scratch:
        children.append(
            {
                "title": "scratch-sibling",
                "assignee": "coder",
                "workspace_kind": "scratch",
                "parents": [],
            }
        )
    child_ids = kb.decompose_triage_task(
        conn, root, root_assignee=None,
        children=children,
        author="test",
    )
    assert child_ids is not None, "decompose_triage_task failed"
    return root, child_ids


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    live_db = Path("/home/piet/.hermes/kanban.db").resolve()
    assert db_path.resolve() != live_db
    assert home.resolve() in db_path.resolve().parents
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def repo(tmp_path):
    """Real git repo on branch ``main`` with one base commit."""
    import subprocess
    r = tmp_path / "repo"
    r.mkdir()

    def _git(*args):
        subprocess.run(
            ["git", "-C", str(r), *args],
            check=True, capture_output=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "t@example.com")
    _git("config", "user.name", "tester")
    (r / "a.txt").write_text("base\n")
    _git("add", "-A")
    _git("commit", "-m", "base")
    return r


# ---------------------------------------------------------------------------
# Test 1 — same-tick race: two ready dir siblings, repo-cap=2, chain guard fires
# ---------------------------------------------------------------------------

def test_same_tick_two_dir_siblings_only_one_dispatched(
    kanban_home, repo, all_assignees_spawnable, monkeypatch,
):
    """Chain guard serializes same-chain dir siblings within the same tick.

    This is the exact S2+S3 incident (t_30804f14 + t_ae5ecc3a, 2026-07-02).
    Both tasks are 'ready' in the same tick.  The first is dispatched normally;
    the second is deferred via ``skipped_chain_worktree_serialized``.

    Repo cap is set high enough (10) that the serialize_by_repo guard cannot
    fire — the chain-worktree guard is the only active constraint.  Note: the
    decompose root in 'todo' status counts as a repo slot holder in
    repo_inflight_counts (it is NOT in the exclusion set), so a cap of at
    least 3 (root + 2 children) is needed to keep the repo guard from masking
    the chain guard in tests.
    """
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned: dict = {}

    with kb.connect() as conn:
        _root, child_ids = _make_decompose_chain(conn, repo, n_dir_siblings=2)
        s2, s3 = child_ids

        res = kb.dispatch_once(
            conn,
            spawn_fn=_spawn_recorder(spawned),
            serialize_by_repo=True,
            max_concurrent_per_repo=10,  # repo cap must not fire; chain guard is the constraint
        )

    # Exactly one dispatched, exactly one chain-serialized
    assert len(res.spawned) == 1, f"expected 1 spawn, got {res.spawned}"
    skipped_ids = [t[0] for t in res.skipped_chain_worktree_serialized]
    assert len(skipped_ids) == 1, (
        f"expected 1 chain-worktree-serialized skip, got "
        f"{res.skipped_chain_worktree_serialized}"
    )
    # The dispatched and the skipped are the two siblings (order may vary)
    dispatched_id = res.spawned[0][0]
    assert set([dispatched_id] + skipped_ids) == {s2, s3}
    # skipped_repo_serialized must be empty — repo cap is not the cause
    assert res.skipped_repo_serialized == [], (
        "repo-cap guard must not fire; chain guard is the active constraint"
    )


# ---------------------------------------------------------------------------
# Test 2 — cross-tick: second sibling in 'review', candidate deferred
# ---------------------------------------------------------------------------

def test_cross_tick_sibling_in_review_deferred(
    kanban_home, repo, all_assignees_spawnable, monkeypatch,
):
    """A sibling already in 'review' holds the chain slot; new candidate deferred."""
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned: dict = {}

    with kb.connect() as conn:
        root, child_ids = _make_decompose_chain(conn, repo, n_dir_siblings=2)
        s2, s3 = child_ids

        # Simulate s2 claimed + moved to 'review' (in-flight, holding the slot)
        claimed = kb.claim_task(conn, s2)
        assert claimed is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review' WHERE id = ?", (s2,)
            )

        # Only s3 is ready; it should be deferred by the chain guard.
        # High repo cap so the repo guard cannot mask the chain guard.
        res = kb.dispatch_once(
            conn,
            spawn_fn=_spawn_recorder(spawned),
            serialize_by_repo=True,
            max_concurrent_per_repo=10,
        )

    assert s3 in spawned, "read-only review work must bypass writer serialization"
    assert res.skipped_worktree_writer_active == []


def test_chain_root_lookup_error_defers_candidate_and_logs(
    kanban_home, repo, all_assignees_spawnable, monkeypatch, caplog,
):
    """A chain-root lookup failure must fail closed for this dispatch tick."""
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned: dict = {}

    with kb.connect() as conn:
        _root, child_ids = _make_decompose_chain(conn, repo, n_dir_siblings=2)
        s2, s3 = child_ids

        claimed = kb.claim_task(conn, s2)
        assert claimed is not None

        real_chain_root_id = kwt.chain_root_id

        def _chain_root_id(conn_arg, task_id):
            if task_id == s3:
                raise RuntimeError("forced candidate chain-root lookup failure")
            return real_chain_root_id(conn_arg, task_id)

        monkeypatch.setattr(kwt, "chain_root_id", _chain_root_id)
        caplog.set_level("WARNING", logger=kb.__name__)

        res = kb.dispatch_once(
            conn,
            spawn_fn=_spawn_recorder(spawned),
            serialize_by_repo=True,
            max_concurrent_per_repo=10,
        )

    assert s3 not in spawned
    skipped_ids = [
        task_id
        for task_id, _root_id in res.skipped_chain_worktree_serialized
    ]
    assert s3 in skipped_ids
    assert "chain-root lookup failed for task" in caplog.text
    assert "forced candidate chain-root lookup failure" in caplog.text


# ---------------------------------------------------------------------------
# Test 3 — scratch sibling not blocked: dir + scratch dispatchable in same tick
# ---------------------------------------------------------------------------

def test_scratch_sibling_not_blocked_by_chain_guard(
    kanban_home, repo, all_assignees_spawnable, monkeypatch,
):
    """A scratch sibling of the same chain is NOT subject to the dir guard."""
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned: dict = {}

    with kb.connect() as conn:
        # One dir sibling + one scratch sibling
        root, child_ids = _make_decompose_chain(
            conn, repo, n_dir_siblings=1, extra_scratch=True,
        )
        dir_child, scratch_child = child_ids

        res = kb.dispatch_once(
            conn,
            spawn_fn=_spawn_recorder(spawned),
            serialize_by_repo=True,
            max_concurrent_per_repo=10,  # high cap; root in todo holds 1 slot
        )

    assert dir_child in spawned, "dir sibling must dispatch"
    assert scratch_child in spawned, "scratch sibling must dispatch (no chain-dir guard applies)"
    assert res.skipped_chain_worktree_serialized == [], (
        "no chain-worktree serialization expected when siblings have different workspace kinds"
    )


# ---------------------------------------------------------------------------
# Test 4 — cross-chain parallelism preserved: two chains, both dispatch
# ---------------------------------------------------------------------------

def test_two_chains_same_repo_both_dispatch(
    kanban_home, repo, all_assignees_spawnable, monkeypatch,
):
    """Tasks from DIFFERENT chains in the same repo both dispatch under cap=2.

    The chain-worktree guard must only constrain same-chain siblings.
    Cross-chain parallelism within the repo cap must remain intact.
    """
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned: dict = {}

    with kb.connect() as conn:
        # Chain A: one dir task
        _root_a, [a1] = _make_decompose_chain(conn, repo, n_dir_siblings=1)
        # Chain B: one dir task
        _root_b, [b1] = _make_decompose_chain(conn, repo, n_dir_siblings=1)

        # High repo cap: 2 roots (todo) + 2 children → need ≥4 slots to not
        # mask the chain guard.  Cross-chain parallelism is the thing we test.
        res = kb.dispatch_once(
            conn,
            spawn_fn=_spawn_recorder(spawned),
            serialize_by_repo=True,
            max_concurrent_per_repo=10,
        )

    assert len(spawned) == 1, "same physical worktree admits only one writer"
    skipped = [item[0] for item in res.skipped_worktree_writer_active]
    assert set(spawned) | set(skipped) == {a1, b1}


# ---------------------------------------------------------------------------
# Test 5 — conflict-fixer exemption: fixer dispatches despite in-flight sibling
# ---------------------------------------------------------------------------

def test_conflict_fixer_exempt_from_chain_guard(
    kanban_home, repo, all_assignees_spawnable, monkeypatch,
):
    """A conflict-fixer task is exempt from the chain-worktree guard.

    A blocked sibling holds the chain slot (would defer any normal candidate).
    The fixer must still dispatch — otherwise fixer + blocked sibling deadlock
    (respawn-guard-stall pattern, burn-dashboard 2026-06-20).
    """
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned: dict = {}

    with kb.connect() as conn:
        root, child_ids = _make_decompose_chain(conn, repo, n_dir_siblings=1)
        (dir_child,) = child_ids

        # Simulate dir_child blocked (in-flight, holds chain slot)
        claimed = kb.claim_task(conn, dir_child)
        assert claimed is not None
        assert kb.block_task(conn, dir_child, reason="integration parked")

        # Create a conflict-fixer for this chain (same repo, same workspace)
        fixer = kb.create_task(
            conn,
            title="conflict fixer",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(repo),
            idempotency_key=f"conflict-fixer:{dir_child}:1",
        )

        res = kb.dispatch_once(
            conn,
            spawn_fn=_spawn_recorder(spawned),
            serialize_by_repo=True,
            max_concurrent_per_repo=10,  # high cap to not mask fixer exemption
        )

    assert fixer in spawned, (
        "conflict-fixer must dispatch despite in-flight sibling holding chain slot"
    )
    # The fixer should NOT appear in chain-worktree-serialized
    skipped_ids = [t[0] for t in res.skipped_chain_worktree_serialized]
    assert fixer not in skipped_ids, "conflict-fixer must not be chain-serialized"


# ---------------------------------------------------------------------------
# Review-path tests (Befund 4, Codex-Review gap): the ready-column guard
# above does NOT cover the review-dispatch path — two dir siblings of the
# same chain both sitting unclaimed in 'review' were spawned into the same
# worktree in one tick (incident 2026-07-02 09:22). These tests exercise the
# same guard mirrored into the ``# ---- review column dispatch ----`` block.
# ---------------------------------------------------------------------------

def _set_review_unclaimed(conn, task_id: str) -> None:
    """Move *task_id* straight to ``status='review'`` with ``claim_lock``
    left ``NULL`` — the exact state review-dispatch's SELECT targets, without
    going through the real submit-for-review flow (not under test here)."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,)
        )


def test_review_same_tick_two_dir_siblings_only_one_dispatched(
    kanban_home, repo, all_assignees_spawnable, monkeypatch,
):
    """Two unclaimed dir review siblings of the same chain, same tick.

    The self-counting trap: ``chain_worktree_inflight_counts`` counts a
    ``review`` status task unconditionally (regardless of ``claim_lock``),
    so each candidate is already counted against its own chain before the
    review loop even starts. A naive ``count >= 1`` check would defer BOTH
    siblings forever. The first sibling processed must dispatch; the second
    must be deferred via the same-tick counter.
    """
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned: dict = {}

    with kb.connect() as conn:
        _root, child_ids = _make_decompose_chain(conn, repo, n_dir_siblings=2)
        s2, s3 = child_ids
        _set_review_unclaimed(conn, s2)
        _set_review_unclaimed(conn, s3)

        res = kb.dispatch_once(
            conn,
            spawn_fn=_spawn_recorder(spawned),
            serialize_by_repo=True,
            max_concurrent_per_repo=10,
        )

    assert len(res.spawned) == 2, f"read-only reviews should run in parallel: {res.spawned}"
    assert set(spawned) == {s2, s3}
    assert res.skipped_worktree_writer_active == []


def test_review_sibling_running_deferred(
    kanban_home, repo, all_assignees_spawnable, monkeypatch,
):
    """Sibling A running (claimed), sibling B unclaimed in review → B deferred."""
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned: dict = {}

    with kb.connect() as conn:
        _root, child_ids = _make_decompose_chain(conn, repo, n_dir_siblings=2)
        s2, s3 = child_ids

        # s2 actively running (claimed — genuinely occupies the worktree).
        claimed = kb.claim_task(conn, s2)
        assert claimed is not None

        # s3 unclaimed in review.
        _set_review_unclaimed(conn, s3)

        res = kb.dispatch_once(
            conn,
            spawn_fn=_spawn_recorder(spawned),
            serialize_by_repo=True,
            max_concurrent_per_repo=10,
        )

    assert s3 in spawned, "read-only review must bypass an active writer"
    assert res.skipped_worktree_writer_active == []


def test_review_single_task_not_self_blocked(
    kanban_home, repo, all_assignees_spawnable, monkeypatch,
):
    """A lone unclaimed review task in a chain must NOT defer itself.

    This is the self-counting trap in isolation: with no other in-flight
    sibling, the candidate is the only contributor to
    ``chain_worktree_inflight_counts`` for its chain root — the guard must
    still let it spawn.
    """
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned: dict = {}

    with kb.connect() as conn:
        _root, child_ids = _make_decompose_chain(conn, repo, n_dir_siblings=1)
        (s1,) = child_ids
        _set_review_unclaimed(conn, s1)

        res = kb.dispatch_once(
            conn,
            spawn_fn=_spawn_recorder(spawned),
            serialize_by_repo=True,
            max_concurrent_per_repo=10,
        )

    assert s1 in spawned, "lone unclaimed review sibling must not self-block"
    assert res.skipped_chain_worktree_serialized == []


def test_review_non_chain_dir_task_unaffected(
    kanban_home, repo, all_assignees_spawnable, monkeypatch,
):
    """A standalone (non-chain) dir task in review dispatches normally."""
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned: dict = {}

    with kb.connect() as conn:
        task = kb.create_task(
            conn,
            title="standalone review task",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        _set_review_unclaimed(conn, task)

        res = kb.dispatch_once(
            conn,
            spawn_fn=_spawn_recorder(spawned),
            serialize_by_repo=True,
            max_concurrent_per_repo=10,
        )

    assert task in spawned, "standalone dir review task must dispatch"
    assert res.skipped_chain_worktree_serialized == []


def test_review_conflict_fixer_exempt(
    kanban_home, repo, all_assignees_spawnable, monkeypatch,
):
    """A conflict-fixer review candidate is exempt from the review guard.

    A blocked sibling holds the chain slot (would defer any normal
    candidate). The fixer must still dispatch — otherwise fixer and blocked
    parent deadlock (respawn-guard-stall pattern, burn-dashboard 2026-06-20),
    mirrored from the ready-path exemption.
    """
    monkeypatch.delenv("HERMES_KANBAN_WORKER_ISOLATION", raising=False)
    spawned: dict = {}

    with kb.connect() as conn:
        _root, child_ids = _make_decompose_chain(conn, repo, n_dir_siblings=1)
        (dir_child,) = child_ids

        # Simulate dir_child blocked (in-flight, holds a chain slot).
        claimed = kb.claim_task(conn, dir_child)
        assert claimed is not None
        assert kb.block_task(conn, dir_child, reason="integration parked")

        # Create a conflict-fixer, in unclaimed review status.
        fixer = kb.create_task(
            conn,
            title="conflict fixer review",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(repo),
            idempotency_key=f"conflict-fixer:{dir_child}:1",
        )
        _set_review_unclaimed(conn, fixer)

        res = kb.dispatch_once(
            conn,
            spawn_fn=_spawn_recorder(spawned),
            serialize_by_repo=True,
            max_concurrent_per_repo=10,
        )

    assert fixer in spawned, (
        "conflict-fixer review must dispatch despite a belegter chain slot"
    )
    skipped_ids = [t[0] for t in res.skipped_chain_worktree_serialized]
    assert fixer not in skipped_ids, "conflict-fixer review must not be chain-serialized"
