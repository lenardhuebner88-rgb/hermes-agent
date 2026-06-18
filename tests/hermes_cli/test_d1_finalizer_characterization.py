"""D1 — Characterization test: decompose-root finalizer multi-child merge.

Drives the REAL ``maybe_integrate_on_complete`` path (called by
``kb.complete_task``) for a root with TWO children, each making a distinct,
non-conflicting file change committed to the SHARED chain branch
(``kanban/<root_id>``).

Architecture note
-----------------
All children of a chain root share a SINGLE worktree and branch.
"Multi-branch" is therefore: multiple workers sequentially commit to the same
branch; the finalizer merges that one accumulated branch into the live target
when the LAST child completes.  There is no per-child merge; the
``integrate_chain`` call happens exactly once, driven by
``maybe_integrate_on_complete`` on the last-completing child.

D1 verdict (#15 — contract made truthful)
------------------------------------------
The characterization was run and found **NO merge-loss bug**: both children's
commits land on the target, exactly one merge commit is produced, and the
worktree/branch are cleaned up.  Every assertion below therefore pins
VERIFIED-CORRECT behaviour — this is a spec contract, not a known-buggy state
held green by ``xfail``.  There is no ``xfail`` in this file precisely because
no bug exists to suppress (a ``strict`` xfail over a passing assertion would
itself fail as XPASS).

These assertions double as regression tripwires: if one ever fails, the
finalizer's merge behaviour has regressed (e.g. a child commit silently
dropped) — investigate as a real bug rather than relaxing the assertion.  It
does NOT change production code.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worktrees as kwt


# ---------------------------------------------------------------------------
# Helpers (mirror what the existing test module uses)
# ---------------------------------------------------------------------------

def _git(repo, *args) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )
    return proc.stdout.strip()


def _commit_in(worktree, relpath, content, msg="change"):
    p = Path(worktree) / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", msg)


def _ok_gate(_repo, _files):
    return True, "stub gate ok"


def _events(conn, task_id, kind):
    import json
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(r["payload"]) if r["payload"] else {} for r in rows]


# ---------------------------------------------------------------------------
# Fixtures (independent; deliberately NOT imported from the sibling module so
# this file stays stable against refactors there).
# ---------------------------------------------------------------------------

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


@pytest.fixture
def repo(tmp_path):
    """Real git repo on branch ``main`` with one base commit."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "tester")
    (r / "base.txt").write_text("base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "base commit")
    return r


# ---------------------------------------------------------------------------
# D1 Characterization test
# ---------------------------------------------------------------------------

def test_d1_finalizer_merges_both_children_changes(
    kanban_home, repo, monkeypatch
):
    """Characterization: root finalizer merges ALL children's commits.

    Flow exercised
    --------------
    1. Create decompose root + child_a + child_b (root is their parent).
    2. Root claims its worktree via provision_for_task.
    3. Root completes (no commits of its own) → deferred while children open
       → root goes to ``done``.
    4. Both children are SQL-promoted to ``running`` with workspace_path
       pointing at the shared chain worktree (mirrors dispatch_once behaviour).
    5. child_a commits a DISTINCT file (child_a.py) to the shared branch.
    6. child_b commits a DISTINCT file (child_b.py) to the shared branch.
    7. child_a completes → deferred (child_b still open).
    8. child_b completes → last open child → triggers maybe_integrate_on_complete
       → integrate_chain merges branch ``kanban/<root>`` into ``main``.

    Characterized outcome (pinned current behavior)
    ------------------------------------------------
    * outcome["action"] == "merged"  (merge did land)
    * ``child_a.py`` is present and correct on main
    * ``child_b.py`` is present and correct on main
    * Exactly ONE merge commit exists on main (one integrate_chain call per chain)
    * The shared worktree is cleaned up (removed)
    """
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)

    with kb.connect() as conn:
        # --- 1. Task graph --------------------------------------------------
        root = kb.create_task(
            conn,
            title="decompose root",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        child_a = kb.create_task(
            conn,
            title="child A",
            assignee="coder",
            parents=[root],
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        child_b = kb.create_task(
            conn,
            title="child B",
            assignee="coder",
            parents=[root],
            workspace_kind="dir",
            workspace_path=str(repo),
        )

        # --- 2. Root claims and provisions the shared worktree --------------
        rtask = kb.claim_task(conn, root)
        shared_wt = kwt.provision_for_task(conn, rtask, str(repo))
        # Verify the worktree and branch were created.
        chain_branch_name = f"kanban/{root}"
        assert _git(shared_wt, "symbolic-ref", "--short", "HEAD") == chain_branch_name

        # --- 3. Root completes (no commits) → deferred → root goes done -----
        # Root has no commits of its own; it completes to signal children may
        # proceed.  maybe_integrate_on_complete defers (open children).
        assert kb.complete_task(conn, root, result="done")
        root_task_after = kb.get_task(conn, root)
        assert root_task_after.status == "done", (
            f"Root must be done after complete_task; got {root_task_after.status}"
        )
        # No merge yet.
        assert _git(repo, "log", "--merges", "--oneline") == ""

        # --- 4. Promote both children to ``running`` with shared worktree ---
        # This mirrors dispatch_once: it sets workspace_path at claim time.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running', workspace_path = ? "
                "WHERE id = ?",
                (str(shared_wt), child_a),
            )
            conn.execute(
                "UPDATE tasks SET status = 'running', workspace_path = ? "
                "WHERE id = ?",
                (str(shared_wt), child_b),
            )

        # --- 5+6. Each child commits a DISTINCT file to the shared branch ---
        _commit_in(
            shared_wt, "child_a.py",
            "CHILD_A = 'alpha'\n",
            msg=f"kanban({child_a}): child_a work",
        )
        _commit_in(
            shared_wt, "child_b.py",
            "CHILD_B = 'beta'\n",
            msg=f"kanban({child_b}): child_b work",
        )

        # Sanity: both files are on the chain branch BEFORE the merge.
        assert (shared_wt / "child_a.py").read_text() == "CHILD_A = 'alpha'\n"
        assert (shared_wt / "child_b.py").read_text() == "CHILD_B = 'beta'\n"

        # --- 7. child_a completes → must defer (child_b still open) ---------
        assert kb.complete_task(conn, child_a, result="done")
        assert _events(conn, child_a, "integration_merged") == [], (
            "child_a completion must defer — child_b still open"
        )
        # No merge yet.
        assert _git(repo, "log", "--merges", "--oneline") == ""
        assert shared_wt.exists(), "worktree must survive while chain is open"

        # --- 8. child_b completes → finalizer fires -------------------------
        assert kb.complete_task(conn, child_b, result="done")

        # Reload task states while connection is still open.
        child_b_task = kb.get_task(conn, child_b)
        child_a_task = kb.get_task(conn, child_a)

        merged_events = _events(conn, child_b, "integration_merged")

    # =========================================================================
    # CHARACTERIZATION ASSERTIONS — pin actual current behavior
    # =========================================================================

    # (a) The finalizer fired and merged.
    assert len(merged_events) == 1, (
        "Exactly one integration_merged event expected on the triggering child"
    )
    assert merged_events[0]["action"] == "merged"
    assert merged_events[0]["target"] == "main"

    # (b) child_a's file must be present on main.
    #
    # REGRESSION TRIPWIRE (#15): D1 verified child_a's commit DOES survive the
    # merge today.  If child_a.py is ever missing here, the finalizer regressed
    # to capturing only child_b's last rebase state and silently dropping
    # child_a's commit — a real merge-loss bug to investigate, not to relax.
    assert (repo / "child_a.py").exists(), (
        "child_a.py must be on main after merge — "
        "REGRESSION (#15): child_a commit dropped if this assertion fails"
    )
    assert (repo / "child_a.py").read_text() == "CHILD_A = 'alpha'\n", (
        "child_a.py content must survive the merge intact"
    )

    # (c) child_b's file must be present on main.
    assert (repo / "child_b.py").exists(), (
        "child_b.py must be on main after merge"
    )
    assert (repo / "child_b.py").read_text() == "CHILD_B = 'beta'\n", (
        "child_b.py content must survive the merge intact"
    )

    # (d) Exactly one merge commit (one integrate_chain call per chain).
    merge_log = _git(repo, "log", "--merges", "--oneline").splitlines()
    assert len(merge_log) == 1, (
        f"Expected exactly 1 merge commit; got {len(merge_log)}: {merge_log}"
    )

    # (e) The shared worktree is cleaned up.
    assert not shared_wt.exists(), (
        "integrate_chain must remove the worktree on successful merge"
    )

    # (f) The chain branch is also gone (remove_worktree deletes it).
    branches = _git(repo, "branch", "--list", "kanban/*")
    assert chain_branch_name not in branches, (
        "chain branch must be deleted after successful integration"
    )

    # (g) child_b (the trigger) is done.
    assert child_b_task.status == "done", (
        f"child_b must be done; got {child_b_task.status}"
    )

    # (h) child_a is also done (completed in step 7).
    assert child_a_task.status == "done", (
        f"child_a must be done; got {child_a_task.status}"
    )
