"""Kanban DB tests: heiler.

Split from test_kanban_db.py (pure move; no test logic changes).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import types
import unittest.mock
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb

from tests.hermes_cli._kanban_test_helpers import (
    _kinds_for,
    _escalation_event,
    _heiler_events,
)

def _make_running_worker(
    conn, *, profile, pid, claim_lock=None, last_heartbeat_at=None,
    started_at=None, title="claude-cli-live", workspace_path=None,
):
    """Set up a ``running`` task + matching ``task_runs`` row directly.

    Mirrors the raw-SQL setup used by the dashboard worker tests so we can
    pin ``profile`` / ``worker_pid`` / ``claim_lock`` / ``last_heartbeat_at``
    (and optionally ``workspace_path`` for the claude-CLI transcript probe)
    without going through the code-task contract gate in ``claim_task``.
    Returns ``(task_id, run_id)``.
    """
    now = int(time.time())
    t = kb.create_task(conn, title=title)
    lock = claim_lock if claim_lock is not None else kb._claimer_id()
    start = started_at if started_at is not None else now
    with kb.write_txn(conn):
        run_id = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, "
            "claim_lock, worker_pid) VALUES (?, ?, 'running', ?, ?, ?)",
            (t, profile, start, lock, pid),
        ).lastrowid
        conn.execute(
            "UPDATE tasks SET status = 'running', current_run_id = ?, "
            "claim_lock = ?, worker_pid = ?, started_at = ?, "
            "last_heartbeat_at = ?, "
            "workspace_kind = CASE WHEN ? IS NULL THEN workspace_kind ELSE 'worktree' END, "
            "workspace_path = COALESCE(?, workspace_path) "
            "WHERE id = ?",
            (run_id, lock, pid, start, last_heartbeat_at,
             workspace_path, workspace_path, t),
        )
    return t, run_id


def _seed_claude_transcript(
    monkeypatch, tmp_path, workspace_path, *, body, mtime,
    filename="34ffd866-1d4b-49c8-81ea-8e7c0cca07c9.jsonl",
):
    """Plant a Claude Code session transcript for ``workspace_path`` under an
    isolated CLAUDE_CONFIG_DIR and return the file path.

    Claude Code stores each session under ``<config>/projects/<munged-cwd>/``
    where the munged name is the absolute cwd with every non-alphanumeric char
    replaced by ``-``. We reproduce that mapping so the heartbeat probe finds it.
    """
    config_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    munged = re.sub(r"[^a-zA-Z0-9]", "-", str(workspace_path))
    proj_dir = config_dir / "projects" / munged
    proj_dir.mkdir(parents=True, exist_ok=True)
    jsonl = proj_dir / filename
    jsonl.write_text(body)
    os.utime(jsonl, (mtime, mtime))
    return jsonl


def _emit_real_bug(conn, task_id, excerpt):
    """Append a real-bug heiler_classification whose payload is built by the
    production helper (so the fingerprint stamping is exercised, not faked)."""
    payload = kb._heiler_classification_payload(
        heiler_class=kb.HEILER_CLASS_REAL_BUG,
        evidence={"matched": "tests failed", "signal_source": "text",
                  "excerpt": excerpt},
        source="test", blocked=True,
    )
    kb.add_event(conn, task_id, kb.HEILER_CLASSIFICATION_EVENT, payload)


def _raw_escalation(conn, task_id, *, why_now="legacy escalation", evidence=None):
    """Emit a bare ``operator_escalation`` with NO inline classification.

    Stands in for a legacy/forgotten/future escalation writer the safety-net
    ``classify_escalations_sweep`` must still cover. Every *known* inline writer
    (failure breaker, stall park, budget-runaway park, release-gate) now
    classifies atomically — see ESCALATION-INLINE-CLASSIFY-S1 — so the sweep's
    own derivation can no longer be exercised through one of them.
    """
    payload = {
        "task": {"id": task_id},
        "why_now": why_now,
        "evidence": evidence or {},
    }
    with kb.write_txn(conn):
        return kb._append_event(
            conn, task_id, kb.OPERATOR_ESCALATION_EVENT, payload,
        )


def test_claude_cli_heartbeat_refreshes_and_emits_honest_note(
    kanban_home, monkeypatch,
):
    """A live claude-CLI run with no prior heartbeat gets last_heartbeat_at
    refreshed, a heartbeat event appended, and an honest note (criteria 1/2/4)."""
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)

    with kb.connect_closing() as conn:
        t, run_id = _make_running_worker(conn, profile="coder-claude", pid=4242)
        # Worker log present → note carries the honest log detail.
        log_dir = kb.worker_logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{t}.log").write_text("claude working...\n" * 100)

        beat = kb.heartbeat_live_claude_cli_workers(conn)
        assert beat == [t]

        task = kb.get_task(conn, t)
        assert task.last_heartbeat_at is not None
        run_hb = conn.execute(
            "SELECT last_heartbeat_at FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()["last_heartbeat_at"]
        assert run_hb is not None

        ev = conn.execute(
            "SELECT json_extract(payload, '$.note') AS note FROM task_events "
            "WHERE task_id = ? AND kind = 'heartbeat' ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        assert ev is not None
        note = ev["note"]
        assert note.startswith("claude-cli running")
        assert "log" in note  # honest log detail, no fake percentage
        assert "%" not in note


def test_claude_cli_heartbeat_skips_hermes_runtime_worker(kanban_home, monkeypatch):
    """Hermes-runtime workers self-heartbeat; the dispatcher must NOT touch
    their heartbeat or it would mask a genuine stall (criterion 5)."""
    import hermes_cli.kanban_db as _kb
    # "worker" is deliberately NOT in HERMES_CLAUDE_CLI_PROFILES.
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)

    with kb.connect_closing() as conn:
        t, _ = _make_running_worker(conn, profile="worker", pid=4243)
        beat = kb.heartbeat_live_claude_cli_workers(conn)
        assert beat == []
        assert kb.get_task(conn, t).last_heartbeat_at is None
        n_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'heartbeat'",
            (t,),
        ).fetchone()[0]
        assert n_events == 0


def test_claude_cli_heartbeat_skips_dead_pid(kanban_home, monkeypatch):
    """A dead PID is detect_crashed_workers' job — the heartbeat step leaves
    it alone so a crashed worker is not falsely kept alive."""
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)

    with kb.connect_closing() as conn:
        t, _ = _make_running_worker(conn, profile="coder-claude", pid=4244)
        beat = kb.heartbeat_live_claude_cli_workers(conn)
        assert beat == []
        assert kb.get_task(conn, t).last_heartbeat_at is None


def test_claude_cli_heartbeat_skips_other_host_claim(kanban_home, monkeypatch):
    """Only host-local claims are candidates — a claim owned by another host
    is checked by that host's dispatcher, not ours."""
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)

    with kb.connect_closing() as conn:
        t, _ = _make_running_worker(
            conn, profile="coder-claude", pid=4245,
            claim_lock="someotherhost:9999",
        )
        beat = kb.heartbeat_live_claude_cli_workers(conn)
        assert beat == []
        assert kb.get_task(conn, t).last_heartbeat_at is None


def test_claude_cli_heartbeat_rate_limited(kanban_home, monkeypatch):
    """A fresh heartbeat is not re-emitted (no timeline spam); a stale one is
    refreshed."""
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
    now = int(time.time())

    with kb.connect_closing() as conn:
        # Fresh heartbeat (10s ago) → skipped.
        t_fresh, _ = _make_running_worker(
            conn, profile="coder-claude", pid=4246,
            last_heartbeat_at=now - 10, title="fresh",
        )
        # Stale heartbeat (well beyond the min gap) → refreshed.
        t_stale, _ = _make_running_worker(
            conn, profile="coder-claude", pid=4247,
            last_heartbeat_at=now - (kb._CLAUDE_CLI_HEARTBEAT_MIN_GAP_SECONDS + 60),
            title="stale",
        )
        beat = kb.heartbeat_live_claude_cli_workers(conn)
        assert t_stale in beat
        assert t_fresh not in beat
        # Fresh run keeps its original beat untouched.
        assert kb.get_task(conn, t_fresh).last_heartbeat_at == now - 10
        # Stale run advanced to ~now.
        assert kb.get_task(conn, t_stale).last_heartbeat_at >= now


def test_claude_cli_heartbeat_note_failsoft_without_log(kanban_home, monkeypatch):
    """No worker log → the note degrades to the honest base, never raises."""
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
    with kb.connect_closing() as conn:
        t, _ = _make_running_worker(conn, profile="coder-claude", pid=4248)
        beat = kb.heartbeat_live_claude_cli_workers(conn)
        assert beat == [t]
        ev = conn.execute(
            "SELECT json_extract(payload, '$.note') AS note FROM task_events "
            "WHERE task_id = ? AND kind = 'heartbeat' ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        assert ev["note"] == "claude-cli running"


def test_claude_cli_heartbeat_note_surfaces_jsonl_when_log_stale(
    kanban_home, tmp_path, monkeypatch,
):
    """AC-1: an empty/stale stdout log but a freshly-written Claude transcript
    must surface the transcript activity, not only the misleading ``log 0B``.

    Reproduces the t_c16549e9 incident: ``claude -p`` writes its real output to
    a session JSONL, leaving the per-task stdout log at 0B; the old note read
    ``claude-cli running · log 0B · last output 1080s`` and looked hung.
    """
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
    now = int(time.time())
    workspace = "/home/x/.hermes/hermes-agent/.worktrees/kanban/t_360a4052"
    secret = "ANTHROPIC_API_KEY=sk-ant-shouldnotleak"

    with kb.connect_closing() as conn:
        t, _ = _make_running_worker(
            conn, profile="coder-claude", pid=5721, workspace_path=workspace,
        )
        # Per-task stdout log present but empty + stale (the misleading signal).
        log_dir = kb.worker_logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{t}.log"
        log_path.write_text("")
        os.utime(log_path, (now - 1080, now - 1080))
        # Live Claude transcript: freshly modified, holds a secret in its body.
        _seed_claude_transcript(
            monkeypatch, tmp_path, workspace,
            body='{"type":"assistant"}\n' + secret + "\n", mtime=now - 3,
        )

        beat = kb.heartbeat_live_claude_cli_workers(conn)
        assert beat == [t]
        note = conn.execute(
            "SELECT json_extract(payload, '$.note') AS note FROM task_events "
            "WHERE task_id = ? AND kind = 'heartbeat' ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()["note"]

    # Existing wording preserved (prefix + honest stdout-log detail)…
    assert note.startswith("claude-cli running")
    # …but the live-session signal is now present so operators can tell a live
    # claude session from a genuinely hung process.
    assert "claude session" in note
    # AC-2: only stat metadata is reported — never transcript contents.
    assert secret not in note
    assert "sk-ant" not in note


def test_claude_cli_heartbeat_note_unchanged_without_transcript(
    kanban_home, tmp_path, monkeypatch,
):
    """AC-3: when no Claude transcript exists for the workspace, the note is
    byte-for-byte the pre-existing wording (no spurious session clause)."""
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    # Point CLAUDE_CONFIG_DIR at an empty dir → no transcript for this workspace.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-claude"))
    now = int(time.time())
    workspace = "/home/x/.hermes/hermes-agent/.worktrees/kanban/t_nope"

    with kb.connect_closing() as conn:
        t, _ = _make_running_worker(
            conn, profile="coder-claude", pid=5722, workspace_path=workspace,
        )
        log_dir = kb.worker_logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{t}.log").write_text("x" * 2048)
        os.utime(log_dir / f"{t}.log", (now - 5, now - 5))
        note = _kb._claude_cli_heartbeat_note(t, workspace_path=workspace)

    assert note == "claude-cli running · log 2KB · last output 5s"
    assert "claude session" not in note


def test_claude_jsonl_activity_reads_only_metadata(
    kanban_home, tmp_path, monkeypatch,
):
    """AC-2: the transcript probe returns (mtime, size) only — it never reads
    the JSONL body into the result, and ignores non-jsonl noise files."""
    import hermes_cli.kanban_db as _kb
    now = int(time.time())
    workspace = "/home/x/work/t_probe"
    jsonl = _seed_claude_transcript(
        monkeypatch, tmp_path, workspace,
        body="secret-line\n" * 50, mtime=now - 7,
    )
    # A newer non-jsonl sibling must be ignored (only *.jsonl counts).
    (jsonl.parent / "notes.txt").write_text("ignore me")
    os.utime(jsonl.parent / "notes.txt", (now, now))

    activity = _kb._claude_jsonl_activity(workspace)
    assert activity is not None
    mtime, size = activity
    assert mtime == now - 7
    assert size == jsonl.stat().st_size
    # Unknown / missing workspace → None (AC-3 fail-soft path).
    assert _kb._claude_jsonl_activity(None) is None
    assert _kb._claude_jsonl_activity("/no/such/workspace/here") is None


def test_dispatch_once_heartbeats_live_claude_cli_and_prevents_false_stale(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """End-to-end: dispatch_once refreshes a live claude-CLI heartbeat BEFORE
    the stale reclaimer runs, so a healthy long run (no self-heartbeat) is not
    false-positive reclaimed, and the run shows up in result.heartbeated."""
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
    five_hours_ago = int(time.time()) - (5 * 3600)

    with kb.connect_closing() as conn:
        t, _ = _make_running_worker(
            conn, profile="coder-claude", pid=4249,
            started_at=five_hours_ago, last_heartbeat_at=None,
        )
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *a, **k: None,
            stale_timeout_seconds=14400,  # 4h — would reclaim a NULL-hb run
            board="default",
        )
        assert t in result.heartbeated
        assert t not in result.stale
        assert kb.get_task(conn, t).status == "running"


def test_phase4_tree_root_woke_excludes_plain_dependency_task(kanban_home):
    """Phase4 F: tree_root_woke only reports real decomposed roots, not any dependent ready task."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="Parent")
        child = kb.create_task(conn, title="Plain dependent")
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        kb.link_tasks(conn, parent, child)
        result = kb.decision_queue(conn)
    assert "tree_root_woke" not in _kinds_for(child, result)


# ---------------------------------------------------------------------------
# S4 Heiler: structured failure-classification + escalation ledger
# ---------------------------------------------------------------------------

def test_s4_classify_failure_transient():
    """dirty-overlap / git-op / wrong-branch and provisioning outcomes ->
    transient. Pure function, no DB."""
    cls, ev = kb._classify_failure(error="dirty worktree overlap on branch X")
    assert cls == kb.HEILER_CLASS_TRANSIENT
    assert ev["signal_source"] == "text"
    assert ev["matched"]

    cls, ev = kb._classify_failure(error="checkout is on the wrong branch")
    assert cls == kb.HEILER_CLASS_TRANSIENT

    # Structural outcome mapping wins without any error wording.
    cls, ev = kb._classify_failure(outcome="spawn_retry")
    assert cls == kb.HEILER_CLASS_TRANSIENT
    assert ev["signal_source"] == "outcome"


def test_s4_classify_failure_real_bug_and_default():
    """Red gate / reviewer findings -> real-bug, but an opaque failure with no
    transient/spec/flaky signal defaults to unclassified."""
    cls, _ = kb._classify_failure(error="gate failed: pytest 3 tests failed")
    assert cls == kb.HEILER_CLASS_REAL_BUG

    cls, _ = kb._classify_failure(error="reviewer findings: REQUEST_CHANGES")
    assert cls == kb.HEILER_CLASS_REAL_BUG

    cls, ev = kb._classify_failure(error="something entirely opaque happened")
    assert cls == kb.HEILER_CLASS_UNCLASSIFIED
    assert ev["signal_source"] == "default"


def test_s4_classify_failure_flaky():
    cls, _ = kb._classify_failure(error="test flake: passed on retry")
    assert cls == kb.HEILER_CLASS_FLAKY


def test_s4_classify_failure_bad_spec():
    cls, _ = kb._classify_failure(error="acceptance criteria cannot be met")
    assert cls == kb.HEILER_CLASS_BAD_SPEC

    # Structural stall_class mapping: repeated decompose failure = spec gap.
    cls, ev = kb._classify_failure(stall_class="triage_decompose_failed")
    assert cls == kb.HEILER_CLASS_BAD_SPEC
    assert ev["signal_source"] == "stall_class"


def test_s4_classify_failure_conflict_wins_over_stall_class():
    """Unambiguous merge-conflict markers win even on the integration_parked
    stall path (which otherwise has no structural mapping)."""
    cls, _ = kb._classify_failure(error="CONFLICT (content): merge conflict in api.ts")
    assert cls == kb.HEILER_CLASS_CONFLICT

    cls, _ = kb._classify_failure(
        stall_class="integration_parked",
        reason="integration parked: merge conflict in web/src/App.tsx",
    )
    assert cls == kb.HEILER_CLASS_CONFLICT


# HEILER-OUTCOME-RECLASSIFY-S1 ------------------------------------------------

def test_capacity_class_registered():
    """The capacity class exists and is a valid Heiler class, but is NOT counted
    as a non-transient 'real problem' (it is pure observability/routing)."""
    assert kb.HEILER_CLASS_CAPACITY == "capacity"
    assert kb.HEILER_CLASS_CAPACITY in kb.HEILER_CLASSES
    from hermes_cli import vision_metrics as vm
    assert kb.HEILER_CLASS_CAPACITY not in vm._NON_TRANSIENT_HEILER_CLASSES


def test_unclassified_class_registered_but_not_non_transient():
    """The opaque default class is valid, but not a known defect signal."""
    assert kb.HEILER_CLASS_UNCLASSIFIED == "unclassified"
    assert kb.HEILER_CLASS_UNCLASSIFIED in kb.HEILER_CLASSES
    from hermes_cli import vision_metrics as vm
    assert kb.HEILER_CLASS_UNCLASSIFIED not in vm._NON_TRANSIENT_HEILER_CLASSES


def test_operator_intent_class_registered_but_not_non_transient():
    """A deliberate operator/hold state (supersede, green-run-yet-still-
    blocked) is not a self-healing signal but also not a product defect —
    like capacity, it is pure observability (HEILER-CLASSIFY-SIGNAL-GAP-S2)."""
    assert kb.HEILER_CLASS_OPERATOR_INTENT == "operator-intent"
    assert kb.HEILER_CLASS_OPERATOR_INTENT in kb.HEILER_CLASSES
    from hermes_cli import vision_metrics as vm
    assert kb.HEILER_CLASS_OPERATOR_INTENT not in vm._NON_TRANSIENT_HEILER_CLASSES


# ESCALATION-OPERATOR-GATE-DECLASSIFY-S1 -------------------------------------

def test_operator_gated_class_registered_but_not_non_transient():
    """A held-before-release / operator-question gate (the operator must
    release/answer) is a deliberate operator state, not a product defect — like
    capacity/operator-intent it is a terminal NON-error class and must NOT be a
    non-transient 'real problem' signal (else it would inflate the autonomy
    counter)."""
    assert kb.HEILER_CLASS_OPERATOR_GATED == "operator-gated"
    assert kb.HEILER_CLASS_OPERATOR_GATED in kb.HEILER_CLASSES
    from hermes_cli import vision_metrics as vm
    assert kb.HEILER_CLASS_OPERATOR_GATED not in vm._NON_TRANSIENT_HEILER_CLASSES


def test_classify_operator_gate_held_before_release_is_operator_gated():
    """The canonical freigabe hold reason (planspecs.py:
    'Planspec ingest: held before release') classifies as operator-gated, not
    the opaque default — it is the dominant live unclassified cluster (AC-1)."""
    cls, ev = kb._classify_failure(error="Planspec ingest: held before release")
    assert cls == kb.HEILER_CLASS_OPERATOR_GATED
    assert ev["signal_source"] == "text"
    assert ev["matched"] == "held before release"


def test_classify_operator_gate_operator_hold_and_human_input():
    """A manual operator hold (hold_task synthesizes summary='operator hold')
    and an explicit human-input/manual-completion park are operator gates, not
    defaults."""
    for reason in (
        "operator hold",
        "Operator manual completion in progress; do not redispatch",
        "need human input on the credential rotation",
        "awaiting operator decision before proceeding",
    ):
        cls, _ = kb._classify_failure(error=reason)
        assert cls == kb.HEILER_CLASS_OPERATOR_GATED, reason


def test_classify_operator_gate_does_not_mask_real_defect():
    """AC-2 guardrail: the operator-gate signals sit BELOW every real-defect
    signal, so an escalation that mentions an operator gate but also carries a
    genuine defect signal stays in its real class — no masking of real defects
    as operator-gated."""
    # red gate wins over an operator-hold mention
    cls, _ = kb._classify_failure(
        error="operator hold pending; but gate failed: 3 tests failed"
    )
    assert cls == kb.HEILER_CLASS_REAL_BUG
    # bad-spec wins over a held-before-release mention
    cls, _ = kb._classify_failure(
        error="held before release; acceptance criteria cannot be met"
    )
    assert cls == kb.HEILER_CLASS_BAD_SPEC
    # a plain opaque failure is still unclassified (no over-firing)
    cls, _ = kb._classify_failure(error="something entirely opaque happened")
    assert cls == kb.HEILER_CLASS_UNCLASSIFIED


def test_classify_escalation_held_before_release_silent_block_is_operator_gated():
    """End-to-end via the silent-block escalation payload shape: a settled
    freigabe hold (last_error='Planspec ingest: held before release',
    trigger_outcome='scheduled', blocked_kind='operator_question') classifies as
    operator-gated instead of unclassified."""
    cls, _ = kb._classify_escalation_payload({
        "why_now": "settled block (last run outcome: scheduled) with no "
                   "operator_escalation — the self-healing retry lane will not "
                   "(further) act on it",
        "evidence": {
            "trigger_outcome": "scheduled",
            "last_error": "Planspec ingest: held before release",
            "blocked_kind": "operator_question",
        },
    })
    assert cls == kb.HEILER_CLASS_OPERATOR_GATED


# ESCALATION-CLASSIFY-RELEASE-GATE-PARK-S1 -----------------------------------

def test_classify_escalation_release_gate_park_is_operator_gated():
    """AC-1: the dominant live cluster — a pre-run 'awaiting release-gate GO'
    park surfaced by the silent-block sweep (release_gate_candidate=True, a bare
    'blocked' trigger_outcome, and a reason that matches no free-text signal) —
    reclassifies from unclassified to the existing operator-gated class by
    reading the structural release_gate_candidate flag the escalation already
    carries."""
    cls, ev = kb._classify_escalation_payload({
        "why_now": "settled block (last run outcome: blocked) with no "
                   "operator_escalation — the self-healing retry lane will not "
                   "(further) act on it",
        "evidence": {
            "trigger_outcome": "blocked",
            "last_error": kb.RELEASE_GATE_BLOCK_REASON,
            "blocked_kind": "operator_question",
            "release_gate_candidate": True,
        },
    })
    assert cls == kb.HEILER_CLASS_OPERATOR_GATED
    assert ev["signal_source"] == "release_gate_candidate"
    assert ev["matched"] == "release_gate_candidate"


def test_classify_escalation_release_gate_park_end_to_end_payload():
    """End-to-end through the real payload builder: the silent-block sweep's
    ``_silent_block_escalation_payload`` for a release-gate park classifies
    operator-gated, proving the emitted evidence shape (not just a hand-built
    dict) closes the unclassified gap."""
    class _Row(dict):
        def keys(self):  # sqlite3.Row-compatible surface used by the builder
            return super().keys()

    row = _Row({
        "id": "t_relgate1",
        "title": "chain root awaiting release gate",
        "status": "blocked",
        "assignee": "default",
        "auto_retry_count": 0,
    })
    payload = kb._silent_block_escalation_payload(
        row=row,
        reason=kb.RELEASE_GATE_BLOCK_REASON,
        blocked_kind="operator_question",
        trigger_outcome="blocked",
    )
    assert payload["evidence"]["release_gate_candidate"] is True
    cls, _ = kb._classify_escalation_payload(payload)
    assert cls == kb.HEILER_CLASS_OPERATOR_GATED


def test_classify_escalation_ran_release_gate_red_stays_real_bug():
    """AC-2 guardrail: a release gate that actually RAN and ended red carries
    trigger_outcome='release_gate_red' (from _escalate_release_gate) and NO
    release_gate_candidate flag → stays real-bug, never masked as
    operator-gated."""
    cls, _ = kb._classify_escalation_payload({
        "why_now": "Release gate for chain t_root still red after 3 bounded "
                   "fixer attempt(s)",
        "evidence": {
            "trigger_outcome": "release_gate_red",
            "last_error": "opaque gate output with no free-text signal",
            "root_id": "t_root",
        },
    })
    assert cls == kb.HEILER_CLASS_REAL_BUG


def test_classify_escalation_release_gate_candidate_with_ran_red_not_masked():
    """AC-2 defensive guard: even if a payload carried BOTH the pre-run flag AND
    a ran-red/infra trigger_outcome, the ran-outcome exclusion keeps it in its
    real class — release_gate_red stays real-bug, release_gate_infra transient —
    so an actually-run red is never declassified to operator-gated."""
    cls_red, _ = kb._classify_escalation_payload({
        "why_now": "settled block (last run outcome: release_gate_red)",
        "evidence": {
            "trigger_outcome": "release_gate_red",
            "last_error": kb.RELEASE_GATE_BLOCK_REASON,
            "release_gate_candidate": True,
        },
    })
    assert cls_red == kb.HEILER_CLASS_REAL_BUG
    cls_infra, _ = kb._classify_escalation_payload({
        "why_now": "settled block (last run outcome: release_gate_infra)",
        "evidence": {
            "trigger_outcome": "release_gate_infra",
            "last_error": kb.RELEASE_GATE_BLOCK_REASON,
            "release_gate_candidate": True,
        },
    })
    assert cls_infra == kb.HEILER_CLASS_TRANSIENT


def test_classify_escalation_release_gate_reason_without_flag_stays_unclassified():
    """No over-firing: the reclassification keys on the STRUCTURAL
    release_gate_candidate flag, not the free text. A payload whose reason
    merely reads 'awaiting release-gate GO' but carries no flag (a legacy /
    non-park writer) is NOT reclassified — the flag is the honest signal."""
    cls, _ = kb._classify_escalation_payload({
        "why_now": "settled block (last run outcome: blocked)",
        "evidence": {
            "trigger_outcome": "blocked",
            "last_error": kb.RELEASE_GATE_BLOCK_REASON,
        },
    })
    assert cls == kb.HEILER_CLASS_UNCLASSIFIED


def test_classify_escalation_release_gate_park_with_real_defect_stays_defect():
    """AC-2: the reclassification only fires when _classify_failure returned
    unclassified. A park flag alongside a genuine defect signal in the reason
    (e.g. a reviewer NEEDS_REVISION that leaked into the block reason) stays in
    its real class — the operator flag never steals a real-error class."""
    cls, _ = kb._classify_escalation_payload({
        "why_now": "settled block (last run outcome: blocked)",
        "evidence": {
            "trigger_outcome": "blocked",
            "last_error": "reviewer finding: needs_revision on the diff",
            "release_gate_candidate": True,
        },
    })
    assert cls == kb.HEILER_CLASS_REAL_BUG


def test_classify_escalation_operator_question_with_real_defect_stays_defect():
    """AC-2: an operator_question-kind escalation whose real reason is a genuine
    defect (a placeholder/null-body spec gap that also trips the question regex)
    stays bad-spec — the block kind does NOT override the defect signal."""
    cls, _ = kb._classify_escalation_payload({
        "why_now": "settled block (last run outcome: blocked) with no "
                   "operator_escalation",
        "evidence": {
            "trigger_outcome": "blocked",
            "last_error": "Task body is a placeholder: it contains only the "
                          "generic Hermes Coder Contract template",
            "blocked_kind": "operator_question",
        },
    })
    assert cls == kb.HEILER_CLASS_BAD_SPEC


def test_classify_nonspawnable_assignee_is_bad_spec():
    """A ready-stage mis-assignment (outcome='nonspawnable_assignee') is a
    structural config/spec gap, not the opaque default (live: t_23415f60,
    assignee 'ui-verifier')."""
    cls, ev = kb._classify_escalation_payload({
        "why_now": "assignee 'ui-verifier' is neither a spawnable Hermes "
                   "profile nor a known terminal lane — the task can never "
                   "auto-dispatch and would rot in ready without this "
                   "escalation",
        "evidence": {"trigger_outcome": "nonspawnable_assignee",
                     "assignee": "ui-verifier"},
    })
    assert cls == kb.HEILER_CLASS_BAD_SPEC
    assert ev["signal_source"] == "outcome"


def test_classify_input_token_runaway_is_capacity():
    """A per-task input-token runaway park reuses the existing capacity class
    (HEILER-CLASSIFY-SIGNAL-GAP-S2: no new class per anti-scope), not the
    opaque default (live budget-runaway escalation why_now shape)."""
    cls, _ = kb._classify_escalation_payload({
        "why_now": "per-task input-token runaway: 2718064 cumulative input "
                   "tokens across 4 run(s) exceeded the cap of 2000000",
        "evidence": {},
    })
    assert cls == kb.HEILER_CLASS_CAPACITY


def test_s4_classify_crashed_worker_is_transient():
    """A bare crashed-worker outcome (dead pid, no content defect) reclassifies
    from the real-bug default to transient, so it flows into the bounded
    transient-retry budget and self-heals (HEILER-OUTCOME-RECLASSIFY-S1 AC-1).
    Reclassification is a fallback: it only fires when no real-bug/flaky/bad-spec
    signal is present in the error text."""
    for err in (
        "pid 12345 exited with code 1",
        "pid 999 not alive",
        "pid 7 killed by signal 9",
    ):
        cls, ev = kb._classify_failure(outcome="crashed", error=err)
        assert cls == kb.HEILER_CLASS_TRANSIENT, err
        assert ev["signal_source"] == "outcome_fallback"
        assert ev["matched"] == "crashed"


def test_s4_classify_crashed_with_real_defect_text_stays_triagierbar():
    """AC-2: a crash whose error text reveals a genuine defect (red gate /
    reviewer findings) is NOT masked as transient — the real-bug text signal
    wins over the crashed->transient fallback, so it stays triagierbar."""
    cls, _ = kb._classify_failure(
        outcome="crashed", error="gate failed: pytest reported 2 tests failed",
    )
    assert cls == kb.HEILER_CLASS_REAL_BUG

    cls, _ = kb._classify_failure(
        outcome="crashed", error="reviewer findings: REQUEST_CHANGES",
    )
    assert cls == kb.HEILER_CLASS_REAL_BUG


def test_s4_classify_iteration_budget_exhausted_is_capacity():
    """iteration_budget_exhausted reclassifies from the real-bug default into a
    distinct capacity class (AC-1), whether the signal arrives as a stall_class
    or as a run outcome (robust to the carrier)."""
    cls, ev = kb._classify_failure(stall_class="iteration_budget_exhausted")
    assert cls == kb.HEILER_CLASS_CAPACITY
    assert ev["signal_source"] == "stall_fallback"
    assert ev["matched"] == "iteration_budget_exhausted"

    cls, ev = kb._classify_failure(outcome="iteration_budget_exhausted")
    assert cls == kb.HEILER_CLASS_CAPACITY
    assert ev["signal_source"] == "outcome_fallback"


def test_s4_classify_iteration_budget_real_defect_stays_triagierbar():
    """AC-2: a task that exhausts its iteration budget BECAUSE of a real defect
    (a red gate / reviewer finding surfaced in the text) stays a real-bug — the
    capacity reclassification is a fallback that text signals override, so the
    genuinely broken task remains triagierbar instead of hidden as capacity."""
    cls, _ = kb._classify_failure(
        outcome="iteration_budget_exhausted",
        error="gate failed: assertion failed in test_loop",
    )
    assert cls == kb.HEILER_CLASS_REAL_BUG


def test_s4_strong_outcome_mapping_still_wins_over_text():
    """Regression guard: the pre-existing STRONG outcome mappings (spawn_retry /
    spawn_failed / rate_limited) still win over error text — only the new
    crashed/iteration_budget fallbacks sit below the text signals."""
    cls, ev = kb._classify_failure(
        outcome="spawn_retry", error="gate failed: 3 tests failed",
    )
    assert cls == kb.HEILER_CLASS_TRANSIENT
    assert ev["signal_source"] == "outcome"


def test_release_gate_red_outcome_is_real_bug():
    """ESCALATION-RELEASE-GATE-ERROR-CONTEXT-S1: a persistent-red release gate,
    carried as the structural ``release_gate_red`` trigger_outcome, classifies
    real-bug even when the gate output text (opaque / visual-gate / empty) matches
    no free-text signal — closing the ``unclassified`` gap that starved by_class."""
    for err in ("", "still broken", "visual-gate: scrollWidth exceeds viewport",
                "visual-gate: dashboard unreachable: Connection refused",
                "error TS2304: Cannot find name Foo"):
        cls, ev = kb._classify_failure(outcome="release_gate_red", error=err)
        assert cls == kb.HEILER_CLASS_REAL_BUG, err
        assert ev["signal_source"] == "outcome_fallback"
        assert ev["matched"] == "release_gate_red"


def test_release_gate_infra_outcome_is_transient():
    """ESCALATION-RELEASE-GATE-ERROR-CONTEXT-S1: a gate the runner could not
    complete (timeout / launch error, carried as ``release_gate_infra``) is
    operational, not a candidate defect → transient, not real-bug."""
    for err in ("release-gate timed out after 1800s",
                "release-gate command error: [Errno 2] No such file"):
        cls, ev = kb._classify_failure(outcome="release_gate_infra", error=err)
        assert cls == kb.HEILER_CLASS_TRANSIENT, err
        assert ev["signal_source"] == "outcome_fallback"


def test_release_gate_outcome_fallback_yields_to_real_text_signal():
    """AC-2 over-mapping guard: the release_gate_* outcome mappings are WEAK
    fallbacks, so a genuine free-text signal in the gate output still classifies
    first — a red gate whose log carries a merge conflict / flaky / reviewer
    finding is NOT force-labelled real-bug by the structural default."""
    cls, ev = kb._classify_failure(
        outcome="release_gate_red",
        error="CONFLICT (content): merge conflict in web/src/App.tsx",
    )
    assert cls == kb.HEILER_CLASS_CONFLICT
    assert ev["signal_source"] == "text"

    cls, _ = kb._classify_failure(
        outcome="release_gate_red", error="flaky: passed on retry",
    )
    assert cls == kb.HEILER_CLASS_FLAKY


def test_s4_crashed_reclassify_stays_bounded(kanban_home):
    """AC-2: crashed->transient is a relabel only — repeated crashes of the same
    task still trip the consecutive-failure breaker and escalate (the bounded
    retry limit is untouched), so there is no unbounded retry storm."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="flapping worker", assignee="coder")
        # First crash: below the DEFAULT_FAILURE_LIMIT=2 breaker -> requeued.
        assert kb.claim_task(conn, tid) is not None
        blocked1 = kb._record_task_failure(
            conn, tid, "pid 111 not alive",
            outcome="crashed", release_claim=True, end_run=True,
        )
        assert blocked1 is False
        # Second crash at the same root: breaker trips -> blocked + escalated.
        assert kb.claim_task(conn, tid) is not None
        blocked2 = kb._record_task_failure(
            conn, tid, "pid 222 not alive",
            outcome="crashed", release_claim=True, end_run=True,
        )
        assert blocked2 is True

        events = kb.list_events(conn, tid)
        heilers = [e for e in events if e.kind == kb.HEILER_CLASSIFICATION_EVENT]
        escalations = [
            e for e in events if e.kind == kb.OPERATOR_ESCALATION_EVENT
        ]
        task = kb.get_task(conn, tid)

    # Every crash classified transient (not real-bug), yet the breaker still
    # blocked the task and raised exactly one operator escalation.
    assert heilers, "expected heiler_classification events"
    assert all(e.payload["class"] == kb.HEILER_CLASS_TRANSIENT for e in heilers)
    assert task.status == "blocked"
    assert len(escalations) == 1


def test_s4_classify_failure_structural_resource_outcomes_do_not_default_real_bug():
    """REASON-FIDELITY-S1: terminal run outcomes that are operational/resource
    limits — not product defects — map structurally, so a settled block carrying
    one as its real trigger_outcome stops defaulting to real-bug."""
    cls, ev = kb._classify_failure(outcome="iteration_budget_exhausted")
    assert cls == kb.HEILER_CLASS_CAPACITY
    assert ev["signal_source"] == "outcome_fallback"

    for outcome in ("timed_out", "reclaimed"):
        cls, ev = kb._classify_failure(outcome=outcome)
        assert cls == kb.HEILER_CLASS_TRANSIENT, outcome
        assert ev["signal_source"] == "outcome"


def test_s4_classify_failure_budget_text_capacity_and_protocol_text_transient():
    """Free-text budget exhaustion -> capacity, while worker-protocol signals
    remain transient harness faults. This covers gave_up budget paths that keep
    their 'gave_up' outcome but carry the budget message."""
    cls, _ = kb._classify_failure(
        error="iteration budget exhausted; continuation limit exhausted (60/60)")
    assert cls == kb.HEILER_CLASS_CAPACITY
    cls, _ = kb._classify_failure(
        error="worker exited cleanly (rc=0) without calling kanban_complete "
              "or kanban_block — protocol violation")
    assert cls == kb.HEILER_CLASS_TRANSIENT


def test_s4_classify_failure_missing_spec_bad_spec():
    """A park reason describing a spec gap -> bad-spec, not the real-bug default
    (the true class of the live silent-block real-bug cluster)."""
    cls, _ = kb._classify_failure(
        error="No actionable implementation spec (3rd run, auto-retry 2/2 "
              "exhausted): title is too vague")
    assert cls == kb.HEILER_CLASS_BAD_SPEC
    cls, _ = kb._classify_failure(
        error="Missing task spec: the card body does not describe what to change")
    assert cls == kb.HEILER_CLASS_BAD_SPEC


# HEILER-CLASSIFY-SIGNAL-GAP-S1 ----------------------------------------------
# Close the classify-coverage hole where settled-block / circuit-breaker
# escalations fell through to unclassified. The genuine signal is the block
# REASON (REASON-FIDELITY design), not the universal "settled block" /
# "retry ladder exhausted" wrappers: a spec-gap reason -> bad-spec, a reviewer
# NEEDS_REVISION verdict -> real-bug. The wrappers themselves are deliberately
# NOT mapped — a wrapper signal would reclassify every bare gave_up (incl.
# genuinely-opaque ones that must stay unclassified), the over-mapping AC-2
# forbids.

def test_s4_classify_reviewer_needs_revision_is_real_bug():
    """A settled block whose reason is a reviewer NEEDS_REVISION verdict is a
    reviewer finding -> real-bug (parallel to request_changes), not the opaque
    default."""
    cls, _ = kb._classify_failure(
        error="Urteil: NEEDS_REVISION\nWarum: die Belege sind widerspruechlich")
    assert cls == kb.HEILER_CLASS_REAL_BUG
    cls, _ = kb._classify_failure(error="reviewer says this needs revision")
    assert cls == kb.HEILER_CLASS_REAL_BUG


def test_s4_classify_no_actionable_spec_beats_broad_transient():
    """A spec-gap reason that incidentally mentions a branch/git must classify
    bad-spec, NOT transient — bad-spec sits ahead of the deliberately-last broad
    git/branch transient catch-alls (the documented precedence intent)."""
    cls, _ = kb._classify_failure(
        error="No actionable review scope (premium/opus, auto-retries exhausted "
              "2/2): title is placeholder 'review'; branch kanban/t_x is empty")
    assert cls == kb.HEILER_CLASS_BAD_SPEC


def test_s4_classify_placeholder_body_is_bad_spec():
    """A settled block whose reason says the task body itself is a placeholder /
    null / empty is a spec gap -> bad-spec."""
    for err in (
        "Task body is a placeholder: it contains only the generic Hermes Coder "
        "Contract v1 template",
        "Unblockable placeholder: body contains only boilerplate",
        "BLOCKED: task body is null — title alone is not an actionable contract",
        "Blocked: current task body is empty/null",
    ):
        cls, _ = kb._classify_failure(error=err)
        assert cls == kb.HEILER_CLASS_BAD_SPEC, err


def test_s4_no_actionable_without_spec_context_stays_unclassified():
    """AC-2 over-mapping guard: bare 'no actionable' is too broad. Only
    concrete scope/body/spec-gap phrases classify bad-spec; opaque missing-proof
    wording remains unclassified until a better signal exists."""
    cls, _ = kb._classify_failure(
        error="settled block: no actionable evidence was provided by worker"
    )
    assert cls == kb.HEILER_CLASS_UNCLASSIFIED


def test_s4_request_changes_mentioning_placeholders_stays_real_bug():
    """AC-2 over-mapping guard: a genuine reviewer REQUEST_CHANGES that merely
    MENTIONS 'placeholders' (e.g. unchecked receipt placeholders) must stay
    real-bug — the placeholder bad-spec signals are precise enough ('body is a
    placeholder') to not hijack a real defect into bad-spec."""
    cls, _ = kb._classify_failure(
        error="REQUEST_CHANGES — AC-3 UNMET: the receipts are still unchecked "
              "OPERATOR-FILL placeholders (`receipt: ____`)")
    assert cls == kb.HEILER_CLASS_REAL_BUG


def test_s4_settled_block_classifies_by_reason_not_wrapper():
    """The 'settled block (last run outcome: …)' why_now is a universal wrapper:
    the class comes from the block REASON in last_error, not the wrapper. A
    spec-gap reason -> bad-spec; a bare wrapper with an opaque reason and a
    trigger_outcome carrying no signal stays honestly unclassified (NOT
    over-mapped, AC-2)."""
    spec = kb._classify_escalation_payload({
        "why_now": "settled block (last run outcome: blocked) with no "
                   "operator_escalation",
        "evidence": {
            "trigger_outcome": "blocked",
            "last_error": "Task body is a placeholder: only boilerplate, no "
                          "actionable specification",
        },
    })
    assert spec[0] == kb.HEILER_CLASS_BAD_SPEC
    opaque = kb._classify_escalation_payload({
        "why_now": "settled block (last run outcome: blocked) with no "
                   "operator_escalation",
        "evidence": {"trigger_outcome": "blocked", "last_error": ""},
    })
    assert opaque[0] == kb.HEILER_CLASS_UNCLASSIFIED


def test_s4_record_task_failure_writes_heiler_classification(kanban_home):
    """A simulated transient block and a red-gate block each write a
    heiler_classification ledger event with the right class + evidence."""
    with kb.connect_closing() as conn:
        transient = kb.create_task(conn, title="transient block", assignee="coder")
        assert kb.claim_task(conn, transient) is not None
        kb._record_task_failure(
            conn, transient,
            "dirty-overlap: worktree had uncommitted foreign work",
            outcome="crashed", failure_limit=5,
            release_claim=True, end_run=True,
        )
        real = kb.create_task(conn, title="red gate", assignee="coder")
        assert kb.claim_task(conn, real) is not None
        kb._record_task_failure(
            conn, real,
            "gate failed: pytest reported 2 tests failed",
            outcome="crashed", failure_limit=5,
            release_claim=True, end_run=True,
        )
        t_events = [
            e for e in kb.list_events(conn, transient)
            if e.kind == kb.HEILER_CLASSIFICATION_EVENT
        ]
        r_events = [
            e for e in kb.list_events(conn, real)
            if e.kind == kb.HEILER_CLASSIFICATION_EVENT
        ]

    assert len(t_events) == 1
    assert t_events[0].payload["class"] == kb.HEILER_CLASS_TRANSIENT
    assert t_events[0].payload["source"] == "record_task_failure"
    assert t_events[0].payload["evidence"]["matched"]

    assert len(r_events) == 1
    assert r_events[0].payload["class"] == kb.HEILER_CLASS_REAL_BUG


def test_s4_stall_park_writes_heiler_classification(kanban_home):
    """no_silent_stall_sweep parking a decompose-failed task writes a
    bad-spec heiler_classification event alongside the operator_escalation."""
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="undecomposable", assignee="coder", triage=True,
        )
        for _ in range(3):
            kb.record_decompose_failure(conn, tid)
        kb.no_silent_stall_sweep(conn, now=now)
        events = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.HEILER_CLASSIFICATION_EVENT
        ]

    assert len(events) == 1
    assert events[0].payload["class"] == kb.HEILER_CLASS_BAD_SPEC
    assert events[0].payload["source"] == "stall_park"
    assert events[0].payload["evidence"]["stall_class"] == "triage_decompose_failed"


def test_s4_read_escalation_ledger_returns_entries_and_rollup(kanban_home):
    """read_escalation_ledger returns the classified entries (newest first),
    a per-class rollup, and honours class/task/limit filters. This is the
    Stratege's (Phase 1.5) input."""
    with kb.connect_closing() as conn:
        transient = kb.create_task(conn, title="transient", assignee="coder")
        kb.claim_task(conn, transient)
        kb._record_task_failure(
            conn, transient, "dirty-overlap git lock contention",
            outcome="crashed", failure_limit=5,
            release_claim=True, end_run=True,
        )
        real = kb.create_task(conn, title="red", assignee="coder")
        kb.claim_task(conn, real)
        kb._record_task_failure(
            conn, real, "gate failed: tests failed",
            outcome="crashed", failure_limit=5,
            release_claim=True, end_run=True,
        )

        ledger = kb.read_escalation_ledger(conn)
        by_task = kb.read_escalation_ledger(conn, task_id=transient)
        only_real = kb.read_escalation_ledger(conn, classes=[kb.HEILER_CLASS_REAL_BUG])
        limited = kb.read_escalation_ledger(conn, limit=1)

    assert ledger["total"] == 2
    assert ledger["by_class"] == {
        kb.HEILER_CLASS_TRANSIENT: 1,
        kb.HEILER_CLASS_REAL_BUG: 1,
    }
    classes_in_order = [e["class"] for e in ledger["entries"]]
    # newest-first ordering
    assert classes_in_order[0] == kb.HEILER_CLASS_REAL_BUG
    assert all("task_title" in e for e in ledger["entries"])

    assert by_task["total"] == 1
    assert by_task["entries"][0]["task_id"] == transient
    assert by_task["entries"][0]["class"] == kb.HEILER_CLASS_TRANSIENT

    assert only_real["total"] == 1
    assert only_real["by_class"] == {kb.HEILER_CLASS_REAL_BUG: 1}

    # limit caps returned entries but the rollup stays over the full window
    assert len(limited["entries"]) == 1
    assert limited["total"] == 2
    assert limited["by_class"] == {
        kb.HEILER_CLASS_TRANSIENT: 1,
        kb.HEILER_CLASS_REAL_BUG: 1,
    }


def test_s4_ledger_by_class_counts_distinct_roots_not_raw_events(kanban_home):
    """LEDGER-BYCLASS-DISTINCT-ROOTS-S1: the read/aggregation path must expose,
    next to the raw event count, a per-class count of *distinct chain roots* so
    one root that escalates repeatedly cannot over-inflate its class. Defense in
    depth complementary to the write-path idempotence: even if some other writer
    duplicates events, the Stratege's input signal stays honest. The raw event
    count is preserved alongside (both values exposed) so recurrence stays
    visible and the class ranking remains explainable."""
    with kb.connect_closing() as conn:
        # Chain A: leaf_a -> mid_a -> root_a. The K2/F1 convention links a leaf
        # (parent) to the orchestration sink/root (child), so root_a is the sink
        # reached by walking child edges downward.
        root_a = kb.create_task(conn, title="root A", assignee="coder")
        mid_a = kb.create_task(conn, title="mid A", assignee="coder")
        leaf_a = kb.create_task(conn, title="leaf A", assignee="coder")
        kb.link_tasks(conn, mid_a, root_a)
        kb.link_tasks(conn, leaf_a, mid_a)
        # The same chain A escalates transient FOUR times across its tasks.
        for _ in range(3):
            kb.add_event(conn, leaf_a, kb.HEILER_CLASSIFICATION_EVENT,
                         {"class": kb.HEILER_CLASS_TRANSIENT})
        kb.add_event(conn, mid_a, kb.HEILER_CLASSIFICATION_EVENT,
                     {"class": kb.HEILER_CLASS_TRANSIENT})

        # Chain B: a second, distinct root that also hits transient once.
        root_b = kb.create_task(conn, title="root B", assignee="coder")
        leaf_b = kb.create_task(conn, title="leaf B", assignee="coder")
        kb.link_tasks(conn, leaf_b, root_b)
        kb.add_event(conn, leaf_b, kb.HEILER_CLASSIFICATION_EVENT,
                     {"class": kb.HEILER_CLASS_TRANSIENT})

        # A standalone (un-linked) task escalates real-bug once → its own root.
        solo = kb.create_task(conn, title="solo", assignee="coder")
        kb.add_event(conn, solo, kb.HEILER_CLASSIFICATION_EVENT,
                     {"class": kb.HEILER_CLASS_REAL_BUG})

        ledger = kb.read_escalation_ledger(conn)
        only_transient = kb.read_escalation_ledger(
            conn, classes=[kb.HEILER_CLASS_TRANSIENT]
        )

    # Raw event count is preserved (guardrail: recurrence stays visible).
    assert ledger["by_class"][kb.HEILER_CLASS_TRANSIENT] == 5
    assert ledger["by_class"][kb.HEILER_CLASS_REAL_BUG] == 1
    assert ledger["total"] == 6

    # Distinct roots: only TWO roots escalated transient (chain A + chain B);
    # the four chain-A events collapse onto root_a. real-bug has one root (solo).
    assert ledger["roots_by_class"][kb.HEILER_CLASS_TRANSIENT] == 2
    assert ledger["roots_by_class"][kb.HEILER_CLASS_REAL_BUG] == 1
    # root_total = distinct roots across all classes (root_a, root_b, solo).
    assert ledger["root_total"] == 3

    # Class filter applies to the distinct-root rollup too.
    assert only_transient["roots_by_class"] == {kb.HEILER_CLASS_TRANSIENT: 2}
    assert only_transient["by_class"] == {kb.HEILER_CLASS_TRANSIENT: 5}
    assert only_transient["root_total"] == 2


# ---------------------------------------------------------------------------
# REALBUG-DETOX: default-sourced real-bug rows are reclassified to unclassified
# at read time so by_class/roots_by_class reflect the true defect signal.
# ---------------------------------------------------------------------------

def test_realbug_detox_default_sourced_rows_reclassified_read_time(kanban_home):
    """REALBUG-DETOX: read_escalation_ledger must re-map real-bug events whose
    evidence.signal_source == 'default' to 'unclassified' at read time, so the
    by_class rollup reflects the true defect signal and not the default-bucket
    residue written by the pre-b2e387669 else-branch.

    Three events:
      (a) real-bug, signal_source='text'  -> stays real-bug
      (b) real-bug, signal_source='default' -> reclassified to unclassified
      (c) transient (no evidence)         -> stays transient

    After the rollup: by_class[real-bug]==1, by_class[unclassified]==1,
    by_class[transient]==1, and event (b)'s task root must NOT appear in
    roots_by_class[real-bug].
    """
    with kb.connect_closing() as conn:
        # (a) legitimate real-bug: signal came from a text match, not the default
        task_a = kb.create_task(conn, title="real gate fail", assignee="coder")
        kb.add_event(conn, task_a, kb.HEILER_CLASSIFICATION_EVENT, {
            "class": kb.HEILER_CLASS_REAL_BUG,
            "evidence": {"signal_source": "text", "matched": "request_changes"},
        })

        # (b) default-bucket residue: written by the pre-fix else-branch
        task_b = kb.create_task(conn, title="default bucket", assignee="coder")
        kb.add_event(conn, task_b, kb.HEILER_CLASSIFICATION_EVENT, {
            "class": kb.HEILER_CLASS_REAL_BUG,
            "evidence": {"signal_source": "default", "matched": "default"},
        })

        # (c) genuine transient: unrelated class, no evidence key
        task_c = kb.create_task(conn, title="transient lock", assignee="coder")
        kb.add_event(conn, task_c, kb.HEILER_CLASSIFICATION_EVENT, {
            "class": kb.HEILER_CLASS_TRANSIENT,
        })

        # (d) default-bucket residue WITH a stamped fingerprint: operational error
        # text (e.g. "iteration budget exhausted") got an excerpt -> a fingerprint,
        # but no real-bug signal. Regression-lock: a fingerprint must NOT exempt a
        # default-sourced row from detox (live evidence: these are operational noise,
        # not code defects).
        task_d = kb.create_task(conn, title="default w/ fingerprint", assignee="coder")
        kb.add_event(conn, task_d, kb.HEILER_CLASSIFICATION_EVENT,
                     kb._heiler_classification_payload(
                         heiler_class=kb.HEILER_CLASS_REAL_BUG,
                         evidence={"signal_source": "default", "matched": "default",
                                   "excerpt": "Iteration budget exhausted (90/90)"},
                         source="test", blocked=True,
                     ))

        ledger = kb.read_escalation_ledger(conn)

    # Raw by_class counts after detox
    assert ledger["by_class"].get(kb.HEILER_CLASS_REAL_BUG, 0) == 1, (
        "only the text-sourced event should count as real-bug"
    )
    assert ledger["by_class"].get(kb.HEILER_CLASS_UNCLASSIFIED, 0) == 2, (
        "both default-sourced rows (b: no fingerprint, d: fingerprinted) must "
        "reclassify to unclassified — a fingerprint does not exempt detox"
    )
    assert ledger["by_class"].get(kb.HEILER_CLASS_TRANSIENT, 0) == 1

    # roots_by_class returns counts of distinct roots per class.
    # task_b (default-sourced) must be counted under unclassified, not real-bug.
    # Each task is unlinked so it is its own chain root -> 1 distinct root each.
    assert ledger["roots_by_class"].get(kb.HEILER_CLASS_REAL_BUG, 0) == 1, (
        "only task_a's root should count under real-bug"
    )
    assert ledger["roots_by_class"].get(kb.HEILER_CLASS_UNCLASSIFIED, 0) == 2, (
        "task_b + task_d roots counted under unclassified, not real-bug"
    )
    assert ledger["roots_by_class"].get(kb.HEILER_CLASS_TRANSIENT, 0) == 1


def test_heiler_classification_payload_stamps_error_fingerprint():
    """The heiler_classification payload carries a normalized error fingerprint
    derived from evidence.excerpt via the existing _error_fingerprint. Two
    excerpts that differ only in host-specific noise (pid / timestamp) collapse
    onto one fingerprint; genuinely distinct excerpts differ; an excerpt-less
    evidence carries no fingerprint. class/evidence are left untouched."""
    ev_a = {"matched": "default", "signal_source": "default",
            "excerpt": "pid 4242 AssertionError: total mismatch at 1718000000000"}
    ev_b = {"matched": "default", "signal_source": "default",
            "excerpt": "pid 9999 AssertionError: total mismatch at 1719999999999"}
    ev_c = {"matched": "default", "signal_source": "default",
            "excerpt": "TypeError: NoneType has no attribute foo"}

    p_a = kb._heiler_classification_payload(
        heiler_class=kb.HEILER_CLASS_REAL_BUG, evidence=ev_a,
        source="test", blocked=True)
    p_b = kb._heiler_classification_payload(
        heiler_class=kb.HEILER_CLASS_REAL_BUG, evidence=ev_b,
        source="test", blocked=True)
    p_c = kb._heiler_classification_payload(
        heiler_class=kb.HEILER_CLASS_REAL_BUG, evidence=ev_c,
        source="test", blocked=True)

    # Same root cause modulo pid/timestamp → one fingerprint.
    assert p_a["fingerprint"] == p_b["fingerprint"]
    assert p_a["fingerprint"] == kb._error_fingerprint(ev_a["excerpt"])
    # Distinct root cause → distinct fingerprint.
    assert p_a["fingerprint"] != p_c["fingerprint"]
    # Additive only: the signal the Stratege already reads is unchanged.
    assert p_a["class"] == kb.HEILER_CLASS_REAL_BUG
    assert p_a["evidence"] is ev_a

    # No excerpt → no fingerprint key (nothing to fingerprint).
    p_none = kb._heiler_classification_payload(
        heiler_class=kb.HEILER_CLASS_REAL_BUG,
        evidence={"matched": "default", "signal_source": "default"},
        source="test", blocked=True)
    assert "fingerprint" not in p_none


def test_s4_ledger_clusters_recurring_real_bugs_by_fingerprint(kanban_home):
    """AC-1: read_escalation_ledger groups real-bug classifications by error
    signature (the stamped _error_fingerprint over evidence.excerpt). Two
    escalations with the same normalized error text form ONE cluster with
    count=2; a distinct error text stays its own cluster. The cluster rollup is
    scoped to real-bug and is additive: by_class / roots_by_class are unchanged
    (AC-2 guardrail)."""
    with kb.connect_closing() as conn:
        t1 = kb.create_task(conn, title="bug one a", assignee="coder")
        t2 = kb.create_task(conn, title="bug one b", assignee="coder")
        t3 = kb.create_task(conn, title="bug two", assignee="coder")
        tt = kb.create_task(conn, title="transient noise", assignee="coder")
        # Two distinct roots hit the SAME normalized error (pid/ts differ).
        _emit_real_bug(
            conn, t1, "pid 11 AssertionError: balance != expected at 1700000000001")
        _emit_real_bug(
            conn, t2, "pid 22 AssertionError: balance != expected at 1700000000002")
        # A third task hits a genuinely different error.
        _emit_real_bug(conn, t3, "TypeError: cannot read property 'id' of undefined")
        # A transient classification WITH an excerpt must not enter the rollup.
        kb.add_event(conn, tt, kb.HEILER_CLASSIFICATION_EVENT, {
            "class": kb.HEILER_CLASS_TRANSIENT,
            "evidence": {"excerpt": "pid 5 dirty-overlap git lock contention"},
        })
        ledger = kb.read_escalation_ledger(conn)

    clusters = ledger["real_bug_signatures"]
    # Two real-bug signatures: the recurring one (count=2) and the distinct one.
    assert len(clusters) == 2
    # Most-recurrent first.
    assert clusters[0]["count"] == 2
    assert set(clusters[0]["example_roots"]) == {t1, t2}
    distinct = [c for c in clusters if c["count"] == 1]
    assert len(distinct) == 1
    assert distinct[0]["example_roots"] == [t3]
    # Cluster rollup is real-bug-only: the transient excerpt's signature is absent.
    sigs = {c["signature"] for c in clusters}
    assert kb._error_fingerprint("pid 5 dirty-overlap git lock contention") not in sigs

    # Guardrail (AC-2): the existing rollups are unchanged by the addition.
    assert ledger["by_class"] == {
        kb.HEILER_CLASS_REAL_BUG: 3, kb.HEILER_CLASS_TRANSIENT: 1}
    assert ledger["roots_by_class"] == {
        kb.HEILER_CLASS_REAL_BUG: 3, kb.HEILER_CLASS_TRANSIENT: 1}
    assert ledger["total"] == 4


def test_s4_ledger_real_bug_clusters_no_false_collision(kanban_home):
    """AC-2 cluster purity: a fixture of genuinely distinct error texts must NOT
    be collapsed. Each distinct normalized signature stays its own cluster (zero
    fingerprint collisions across the fixture), so distinct root causes are never
    merged into one recurrence count."""
    distinct_errors = [
        "AssertionError: expected 200 got 500 in test_login",
        "TypeError: cannot read property 'id' of undefined in cart",
        "KeyError: 'profile' while building the dashboard payload",
        "ValueError: invalid literal for int() with base 10: 'abc'",
        "sqlite3.IntegrityError: UNIQUE constraint failed tasks.id",
        "ModuleNotFoundError: No module named 'hermes_cli.flow'",
        "tsc error TS2345: argument of type string is not assignable",
        "lint error: 'x' is assigned a value but never used",
        "RecursionError: maximum recursion depth exceeded in resolve",
        "ZeroDivisionError: division by zero in cost-per-token rollup",
    ]
    # Sanity: the fixture itself has no two entries sharing a fingerprint.
    assert len({kb._error_fingerprint(e) for e in distinct_errors}) == len(distinct_errors)

    with kb.connect_closing() as conn:
        for i, err in enumerate(distinct_errors):
            tid = kb.create_task(conn, title=f"bug {i}", assignee="coder")
            _emit_real_bug(conn, tid, err)
        ledger = kb.read_escalation_ledger(conn)

    clusters = ledger["real_bug_signatures"]
    # No false merges: one cluster per distinct error, each count=1.
    assert len(clusters) == len(distinct_errors)
    assert all(c["count"] == 1 for c in clusters)
    assert ledger["by_class"] == {kb.HEILER_CLASS_REAL_BUG: len(distinct_errors)}


def test_s4_ledger_clusters_recompute_fingerprint_for_unstamped_events(kanban_home):
    """The signature rollup also covers legacy real-bug events written before the
    fingerprint was stamped: the reader recomputes the signature from
    evidence.excerpt, so two unstamped events with the same normalized error
    still cluster together."""
    with kb.connect_closing() as conn:
        t1 = kb.create_task(conn, title="legacy a", assignee="coder")
        t2 = kb.create_task(conn, title="legacy b", assignee="coder")
        # Raw payloads WITHOUT a stamped fingerprint (pre-S1 shape).
        kb.add_event(conn, t1, kb.HEILER_CLASSIFICATION_EVENT, {
            "class": kb.HEILER_CLASS_REAL_BUG,
            "evidence": {"excerpt": "pid 1 build failed: missing symbol at 1700000000000"},
        })
        kb.add_event(conn, t2, kb.HEILER_CLASSIFICATION_EVENT, {
            "class": kb.HEILER_CLASS_REAL_BUG,
            "evidence": {"excerpt": "pid 2 build failed: missing symbol at 1700000000009"},
        })
        ledger = kb.read_escalation_ledger(conn)

    clusters = ledger["real_bug_signatures"]
    assert len(clusters) == 1
    assert clusters[0]["count"] == 2
    assert set(clusters[0]["example_roots"]) == {t1, t2}


def test_record_task_failure_escalation_carries_escalation_event_id(kanban_home):
    """When the breaker trips, the inline heiler_classification references the
    escalation event it pairs with (the AC-2 documented ledger reference)."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="red gate", assignee="coder")
        assert kb.claim_task(conn, tid) is not None
        kb._record_task_failure(
            conn, tid, "gate failed: tests failed",
            outcome="crashed", failure_limit=1,
            release_claim=True, end_run=True,
        )
        esc = _escalation_event(conn, tid)
        heilers = _heiler_events(conn, tid)

    assert len(heilers) == 1
    assert heilers[0].payload["escalation_event_id"] == esc.id
    assert heilers[0].payload["class"] == kb.HEILER_CLASS_REAL_BUG


def test_park_budget_runaway_writes_inline_heiler_classification(kanban_home):
    """ESCALATION-INLINE-CLASSIFY-S1 (defense-in-depth): the budget-runaway park
    classifies atomically AT the escalation site — exactly one
    heiler_classification, referencing the escalation event, tagged with the
    inline budget-runaway source, with a belegter (signal-source) evidence
    reference rather than a guess (AC-2). No sweep poll required."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="runaway loop", assignee="coder")
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        parked = kb._park_budget_runaway(
            conn, row, token_sum=5000, cap=1000, runs=3,
        )
        esc = _escalation_event(conn, tid)
        heilers = _heiler_events(conn, tid)

    assert parked is True
    assert len(heilers) == 1
    assert heilers[0].payload["escalation_event_id"] == esc.id
    assert heilers[0].payload["source"] == kb.HEILER_SOURCE_BUDGET_RUNAWAY
    assert heilers[0].payload["class"] in kb.HEILER_CLASSES
    assert heilers[0].payload["blocked"] is True
    assert heilers[0].payload["evidence"].get("signal_source")


def test_park_budget_runaway_inline_matches_sweep_and_sweep_skips(kanban_home):
    """The inline class is byte-identical to what the backfill sweep would
    derive from the same persisted payload (defense-in-depth, NOT divergence),
    and the sweep then adds nothing because the escalation is already paired."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="runaway loop", assignee="coder")
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        kb._park_budget_runaway(conn, row, token_sum=5000, cap=1000, runs=3)
        esc = _escalation_event(conn, tid)
        inline = _heiler_events(conn, tid)[0]
        expected_class, _ = kb._classify_escalation_payload(esc.payload)

        summary = kb.classify_escalations_sweep(conn)
        heilers = _heiler_events(conn, tid)

    assert inline.payload["class"] == expected_class
    assert summary["classified"] == []
    assert len(heilers) == 1


def test_classify_escalations_sweep_classifies_unpaired_escalation(kanban_home):
    """A bare escalation from a writer that did NOT classify inline gets exactly
    one backfilled classification from the sweep, referencing the escalation
    event and deriving the class from its evidence."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy escalation", assignee="coder")
        _raw_escalation(conn, tid, why_now="gate failed: tests failed")
        # Pre-sweep: escalation present, no classification.
        assert _heiler_events(conn, tid) == []
        esc = _escalation_event(conn, tid)

        summary = kb.classify_escalations_sweep(conn)

        heilers = _heiler_events(conn, tid)

    assert len(heilers) == 1
    assert heilers[0].payload["escalation_event_id"] == esc.id
    assert heilers[0].payload["source"] == kb.HEILER_SOURCE_ESCALATION_SWEEP
    assert heilers[0].payload["class"] in kb.HEILER_CLASSES
    assert any(c["escalation_event_id"] == esc.id for c in summary["classified"])


def test_classify_escalations_sweep_is_idempotent(kanban_home):
    """Re-running the sweep adds no second classification for the same
    escalation."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy escalation", assignee="coder")
        _raw_escalation(conn, tid, why_now="merge conflict in api.ts")
        first = kb.classify_escalations_sweep(conn)
        second = kb.classify_escalations_sweep(conn)
        heilers = _heiler_events(conn, tid)

    assert len(heilers) == 1
    assert len(first["classified"]) == 1
    assert second["classified"] == []


def test_classify_escalations_sweep_skips_inline_paired(kanban_home):
    """An escalation already paired inline (record_task_failure) is left
    untouched by the sweep — no duplicate classification."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="red gate", assignee="coder")
        assert kb.claim_task(conn, tid) is not None
        kb._record_task_failure(
            conn, tid, "gate failed: tests failed",
            outcome="crashed", failure_limit=1,
            release_claim=True, end_run=True,
        )
        before = len(_heiler_events(conn, tid))
        summary = kb.classify_escalations_sweep(conn)
        after = len(_heiler_events(conn, tid))

    assert before == 1
    assert after == 1
    assert summary["classified"] == []


def test_classify_escalations_sweep_derives_class_from_evidence(kanban_home):
    """The sweep reuses the deterministic classifier over the escalation's own
    persisted evidence — a merge-conflict park is classed 'conflict'."""
    now = 1_900_000_000
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="merge mess", assignee="coder")
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        kb._park_stall_once(
            conn, row,
            stall_class="integration_parked",
            reason="integration parked: merge conflict in api.ts",
            evidence={"attempts": 2},
            now=now,
        )
        # _park_stall_once classifies inline; strip it so we test the sweep's
        # own derivation path on a genuinely unpaired escalation.
        conn.execute(
            "DELETE FROM task_events WHERE task_id = ? AND kind = ?",
            (tid, kb.HEILER_CLASSIFICATION_EVENT),
        )
        conn.commit()
        assert _heiler_events(conn, tid) == []

        kb.classify_escalations_sweep(conn)
        heilers = _heiler_events(conn, tid)

    assert len(heilers) == 1
    assert heilers[0].payload["class"] == kb.HEILER_CLASS_CONFLICT


def test_record_classification_correction_records_event(kanban_home):
    """An operator correction is stored as a distinct
    heiler_classification_corrected event referencing the escalation, leaving
    the auto by_class ledger untouched."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="runaway loop", assignee="coder")
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        kb._park_budget_runaway(conn, row, token_sum=5000, cap=1000, runs=3)
        kb.classify_escalations_sweep(conn)
        esc = _escalation_event(conn, tid)

        ok = kb.record_classification_correction(
            conn, esc.id,
            corrected_to=kb.HEILER_CLASS_BAD_SPEC,
            reason="operator: this was an underspecified AC, not a runaway",
        )
        corrections = [
            e for e in kb.list_events(conn, tid)
            if e.kind == kb.HEILER_CLASSIFICATION_CORRECTED_EVENT
        ]
        # auto ledger unchanged (still exactly one auto classification)
        autos = _heiler_events(conn, tid)

    assert ok is True
    assert len(corrections) == 1
    assert corrections[0].payload["escalation_event_id"] == esc.id
    assert corrections[0].payload["corrected_to"] == kb.HEILER_CLASS_BAD_SPEC
    assert len(autos) == 1


def test_record_classification_correction_rejects_unknown_class(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="coder")
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        kb._park_budget_runaway(conn, row, token_sum=5000, cap=1000, runs=3)
        esc = _escalation_event(conn, tid)
        with pytest.raises(ValueError):
            kb.record_classification_correction(
                conn, esc.id, corrected_to="not-a-class",
            )
        # a non-existent escalation id is a no-op, not a crash
        assert kb.record_classification_correction(
            conn, 999_999, corrected_to=kb.HEILER_CLASS_REAL_BUG,
        ) is False

