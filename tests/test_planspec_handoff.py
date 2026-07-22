"""W3-S2 terminal handoff: structured schema, raw artifacts, born-held roots."""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import planspecs, terminal_handoff


@pytest.fixture()
def plans_root(tmp_path, monkeypatch):
    root = tmp_path / "plans"
    root.mkdir()
    monkeypatch.setattr(planspecs, "DEFAULT_PLANS_ROOT", root)
    return root


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SANDBOX_MODE", "1")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    return home


def test_legacy_write_handoff_draft_is_forbidden(plans_root):
    with pytest.raises(terminal_handoff.HandoffSchemaError):
        terminal_handoff.write_handoff_draft("# x", slug="nope", plans_root=plans_root)
    assert list(plans_root.rglob("*.md")) == []


def test_legacy_content_request_rejected_before_write(plans_root):
    with pytest.raises(terminal_handoff.HandoffSchemaError):
        terminal_handoff.parse_structured_request({"content": "# draft"})
    assert list(plans_root.rglob("*")) == []


def test_raw_oversize_rejected(hermes_home):
    big = "x" * (terminal_handoff.RAW_MAX_BYTES + 10)
    with pytest.raises(terminal_handoff.HandoffError, match="bytes"):
        terminal_handoff.normalize_raw_text(big)


def test_raw_too_many_lines_rejected(hermes_home):
    text = "\n".join(["line"] * (terminal_handoff.RAW_MAX_LINES + 1))
    with pytest.raises(terminal_handoff.HandoffError, match="lines"):
        terminal_handoff.normalize_raw_text(text)


def test_materialize_raw_artifact_0600_and_sha(hermes_home):
    from hermes_constants import terminal_runs_root

    body = b"SECRET_CANARY_raw_only\n"
    desc = terminal_handoff.materialize_raw_artifact(
        terminal_run_id="tr_abc", raw_bytes=body
    )
    path = Path(desc.source_path)
    assert path.is_file()
    assert path.read_bytes() == body
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert desc.sha256 == hashlib.sha256(body).hexdigest()
    assert path.is_relative_to(terminal_runs_root())


def test_stable_planspec_path_identity_and_conflict(plans_root):
    corr = "corr_stable_1"
    text1 = terminal_handoff.build_raw_free_planspec_text(
        title="T1", goal="G1", correlation_id=corr
    )
    p1 = terminal_handoff.write_stable_planspec(
        correlation_id=corr, text=text1, plans_root=plans_root
    )
    p2 = terminal_handoff.write_stable_planspec(
        correlation_id=corr, text=text1, plans_root=plans_root
    )
    assert p1 == p2
    assert p1.name == f"{corr}.md"
    with pytest.raises(terminal_handoff.HandoffConflict):
        terminal_handoff.write_stable_planspec(
            correlation_id=corr, text=text1 + "\n# changed\n", plans_root=plans_root
        )


def test_validate_is_write_free(plans_root, hermes_home):
    payload = {
        "schema_version": 1,
        "capsule": {"terminal_run_id": "tr1"},
        "draft": {"title": "Validate only", "goal": "g", "mode": "planspec"},
        "raw": {"text": "capture line"},
        "terminal_run_id": "tr1",
    }
    before = list(plans_root.rglob("*"))
    result = terminal_handoff.validate_structured_handoff(payload, plans_root=plans_root)
    after = list(plans_root.rglob("*"))
    assert before == after
    assert result.get("writes") is False
    # no durable artifact under terminal runs from validate
    from hermes_constants import terminal_runs_root
    assert not any(terminal_runs_root().rglob("handoff-raw.txt"))


def test_create_held_root_never_triage(hermes_home):
    conn = kb.connect()
    try:
        tid = kb.create_held_decompose_root(
            conn,
            title="direct held",
            body="b",
            assignee="coder",
            freigabe="operator",
            live_test_depth="contract",
            hold_reason="Direct handoff: held before release",
            root_kind="handoff_direct",
            correlation_id="c1",
            artifact_digest="ab" * 32,
            handoff_artifact_required=True,
        )
        task = kb.get_task(conn, tid)
        assert task.status == "scheduled"
        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "triage" not in kinds
        assert events[0].kind == "created"
        assert events[0].payload.get("status") == "scheduled"
        assert "handoff_direct" in kinds
        assert "handoff_artifact_required" in kinds
        # no untagged triage status ever
        statuses = [
            (e.payload or {}).get("status")
            for e in events
            if isinstance(e.payload, dict) and "status" in (e.payload or {})
        ]
        assert "triage" not in statuses
    finally:
        conn.close()


def test_canary_not_in_events_metadata(hermes_home, plans_root):
    canary = "SECRET_CANARY_do_not_log"
    body = f"{canary}\nmore lines\n".encode()
    desc = terminal_handoff.materialize_raw_artifact(
        terminal_run_id="tr_canary", raw_bytes=body
    )
    text = terminal_handoff.build_raw_free_planspec_text(
        title="Canary",
        goal="g",
        correlation_id="corr_canary",
        artifact=desc,
    )
    assert canary not in text
    path = terminal_handoff.write_stable_planspec(
        correlation_id="corr_canary", text=text, plans_root=plans_root
    )
    assert canary not in path.read_text()
    conn = kb.connect()
    try:
        tid = kb.create_held_decompose_root(
            conn,
            title="canary root",
            freigabe="operator",
            live_test_depth="contract",
            hold_reason="Direct handoff: held before release",
            root_kind="handoff_direct",
            correlation_id="corr_canary",
            artifact_digest=desc.sha256,
            handoff_artifact_required=True,
            handoff_artifact_descriptor=desc.as_dict(),
        )
        att = kb.add_immutable_handoff_attachment(
            conn,
            tid,
            source_path=desc.source_path,
            sha256=desc.sha256,
            size=desc.size,
        )
        blob = Path(att.stored_path).read_text()
        assert canary in blob
        for e in kb.list_events(conn, tid):
            assert canary not in str(e.payload)
            assert canary not in e.kind
    finally:
        conn.close()
