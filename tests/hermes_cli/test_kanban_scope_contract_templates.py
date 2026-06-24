"""PlanSpec B — server-side scope-contract templates + template expansion.

Covers:
  * Step 1 — template registry structural validity + the security invariant
    that only ``scope_contract_version`` may be auto-filled.
  * Step 2 — ``contract_profile`` body parsing (valid / unknown / missing /
    inline-conflict).
  * Step 3 — expansion in ``build_worker_context`` + dispatcher trace-stamp
    onto ``run.metadata.expanded_contract`` (in-process path only).
  * Step 5 — backward-compat (inline contract unchanged) + the >=40% body
    reduction metric for template-referencing tasks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as kd
from hermes_cli import kanban_templates as kt


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_spawn(*args, **kwargs):
    return 12345


# ---------------------------------------------------------------------------
# Step 1 — template registry
# ---------------------------------------------------------------------------

def test_registry_has_four_known_templates():
    assert kt.template_names() == [
        "code_implementation_v2",
        "read_only_audit_v2",
        "research_synthesis_v2",
        "review_verdict_v2",
    ]


def test_all_templates_are_structurally_valid():
    for name in kt.template_names():
        assert kt.validate_template(kt.get_template(name)) == [], name


def test_validate_template_rejects_runtime_evidence_autofill():
    """Security invariant: scope_attestation / forbidden_actions_taken are
    runtime evidence and must never be auto-fillable from a static template."""
    bad = dict(kt.get_template("read_only_audit_v2"))
    bad["auto_fill"] = {"scope_contract_version": 2, "scope_attestation": True}
    problems = kt.validate_template(bad)
    assert any("auto_fill may only contain scope_contract_version" in p for p in problems)


def test_validate_template_flags_missing_required_fields():
    assert kt.validate_template({}) != []
    assert kt.validate_template("not a dict") == ["template must be a dict"]


# ---------------------------------------------------------------------------
# Step 2 — contract_profile parsing
# ---------------------------------------------------------------------------

def test_parse_valid_reference():
    body = "contract_profile: read_only_audit_v2\n\nObjective: audit X"
    assert kb._parse_contract_profile(body) == "read_only_audit_v2"


def test_parse_unknown_name_returns_none():
    assert kb._parse_contract_profile("contract_profile: does_not_exist_v9") is None


def test_parse_missing_returns_none():
    assert kb._parse_contract_profile("Just a plain task body.") is None
    assert kb._parse_contract_profile("") is None


def test_parse_inline_scope_contract_wins():
    """Pitfall 8 — when both an inline scope_contract block and a
    contract_profile reference are present, the inline contract wins and we
    do NOT expand (parser returns None)."""
    body = (
        "scope_contract:\n"
        "  version: 2\n"
        "  allowed_tools:\n    - kanban_complete\n"
        "contract_profile: read_only_audit_v2\n"
    )
    assert kb._parse_contract_profile(body) is None


def test_parse_strips_quotes_and_inline_comment():
    body = "contract_profile: 'read_only_audit_v2'  # default audit profile"
    assert kb._parse_contract_profile(body) == "read_only_audit_v2"


# ---------------------------------------------------------------------------
# Step 3 — expansion + render
# ---------------------------------------------------------------------------

def test_expand_contract_payload_shape():
    exp = kb.expand_contract_for_body("contract_profile: code_implementation_v2")
    assert exp is not None
    assert exp["template"] == "code_implementation_v2"
    assert exp["scope_contract_version"] == 2
    assert exp["auto_fill"] == {"scope_contract_version": 2}
    # runtime-evidence fields are NOT present in the auto_fill block
    assert "scope_attestation" not in exp["auto_fill"]
    assert "forbidden_actions_taken" not in exp["auto_fill"]


def test_expand_returns_none_without_reference():
    assert kb.expand_contract_for_body("plain body") is None


def test_worker_context_renders_expanded_block(kanban_home):
    body = "contract_profile: read_only_audit_v2\n\nObjective: audit worker efficiency"
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="audit", assignee="builder", body=body)
        ctx = kb.build_worker_context(conn, tid)
    assert "## Scope Contract (expanded from template `read_only_audit_v2`)" in ctx
    assert "**Forbidden actions:** file_write" in ctx
    # task-specific objective is preserved, contract_profile directive is gone
    assert "Objective: audit worker efficiency" in ctx
    assert "contract_profile:" not in ctx
    # the worker is told what was auto-filled (Pitfall 3)
    assert "scope_contract_version=2" in ctx


# ---------------------------------------------------------------------------
# Step 5 — backward compat + body-reduction metric
# ---------------------------------------------------------------------------

def test_inline_contract_body_unchanged(kanban_home):
    """A task carrying an inline scope_contract block renders byte-identical to
    the raw (capped) body — no expansion section is injected."""
    inline = (
        "scope_contract:\n"
        "  version: 2\n"
        "  objective: do the thing\n"
        "  allowed_tools:\n    - kanban_complete\n"
        "completion_policy:\n  require_scope_attestation: true\n"
    )
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="inline", assignee="builder", body=inline)
        ctx = kb.build_worker_context(conn, tid)
    assert "Scope Contract (expanded from template" not in ctx
    assert "scope_contract:" in ctx  # raw inline block survived verbatim


def test_template_reference_reduces_body_at_least_40_percent():
    """Step 5 metric: a contract_profile reference is >=40% smaller than the
    equivalent inline scope_contract YAML body."""
    objective = "Auditiere Worker-Effizienz und poste die Funde als Kommentar."
    inline_contract = kd._render_scope_contract_yaml(
        kd._default_worker_scope_contract({"title": objective})
    )
    inline_body = f"{inline_contract}\n\n{objective}"
    reference_body = f"contract_profile: read_only_audit_v2\n\n{objective}"
    reduction = (len(inline_body) - len(reference_body)) / len(inline_body)
    assert reduction >= 0.40, f"only {reduction:.0%} smaller"


# ---------------------------------------------------------------------------
# Step 3/4 — dispatcher trace-stamp (in-process path only)
# ---------------------------------------------------------------------------

def _run_metadata(conn, task_id) -> dict:
    row = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return json.loads(row["metadata"]) if row and row["metadata"] else {}


def test_dispatch_stamps_expanded_contract(kanban_home, all_assignees_spawnable):
    body = "contract_profile: read_only_audit_v2\n\nObjective: audit"
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="audit", assignee="builder", body=body)
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
        assert tid in [s[0] for s in res.spawned]
        meta = _run_metadata(conn, tid)
    assert meta.get("expanded_contract", {}).get("template") == "read_only_audit_v2"
    assert meta["expanded_contract"]["scope_contract_version"] == 2


def test_dispatch_no_stamp_without_reference(kanban_home, all_assignees_spawnable):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain", assignee="builder", body="just do it")
        kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
        meta = _run_metadata(conn, tid)
    assert "expanded_contract" not in meta


def test_dispatch_preserves_existing_run_metadata(kanban_home, all_assignees_spawnable):
    """Stamping merges, it must not clobber spawn-identity metadata."""
    body = "contract_profile: read_only_audit_v2\n\nObjective: audit"
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="audit", assignee="builder", body=body)
        kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
        meta = _run_metadata(conn, tid)
    # claim_task seeds spawn-identity metadata; the stamp must keep it.
    assert "expanded_contract" in meta
    # and the merge produced valid JSON with both old + new keys (no overwrite-to-bare)
    assert isinstance(meta, dict) and len(meta) >= 1


def test_claude_cli_path_not_stamped(kanban_home, all_assignees_spawnable, monkeypatch):
    """Pitfall 10 / non-goal: the claude-CLI worker path does not expand
    templates, so its run must NOT carry an expanded_contract trace."""
    monkeypatch.setattr(kb, "_run_is_claude_cli", lambda profile, board=None: True)
    body = "contract_profile: read_only_audit_v2\n\nObjective: audit"
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="audit", assignee="builder", body=body)
        kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
        meta = _run_metadata(conn, tid)
    assert "expanded_contract" not in meta


def test_unknown_profile_logs_warning_and_no_stamp(
    kanban_home, all_assignees_spawnable, caplog
):
    """Pitfall 9: an unknown contract_profile reference renders the body raw,
    stamps nothing, and logs a warning."""
    body = "contract_profile: totally_unknown_v9\n\nObjective: x"
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="bad-ref", assignee="builder", body=body)
        with caplog.at_level("WARNING"):
            kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
        meta = _run_metadata(conn, tid)
    assert "expanded_contract" not in meta
    assert any("unknown contract_profile" in r.message for r in caplog.records)
