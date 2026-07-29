"""Phase 5 tests: bounded, reversible autonomous runner.

Everything runs against a throwaway skills root + audit dir (env-overridden), so
no real skill or audit file is ever touched.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_autoresearch_request.py"
REQUEST_SCRIPT = ROOT / "scripts" / "autoresearch_request.py"


class _Msg:
    content = "pong"


class _Choice:
    message = _Msg()


class _Resp:
    choices = [_Choice()]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


SKILL_COMPLETE = """---
name: complete-skill
description: a complete skill
---

# Complete

## When to Use
use it when needed.

## Safety
never touch secrets.

## Procedure
do the steps.

## Output
produce a report.
"""

# Missing only the "Procedure / Vorgehen" section group. NOTE: the body must not
# contain any procedure needle (procedure/vorgehen/workflow/steps/schritte/prozess)
# anywhere — including frontmatter — or eval would see the section as present.
SKILL_NEEDS_PROCEDURE = """---
name: needy-skill
description: a demo skill that lacks one recommended section
---

# Needy

## When to Use
Use this when you need the demo behaviour and want a clear trigger described here.

## Safety
Never expose credentials or secrets in this context.

## Output
Produce a structured report as the deliverable for the caller.
"""


@pytest.fixture()
def env(monkeypatch, tmp_path):
    # home lives under a DOTTED dir (mirrors the real ~/.hermes) so the
    # archived/hidden skip can't accidentally disqualify every skill.
    home = tmp_path / ".hermes"
    skills = home / "skills"
    audit = tmp_path / "audit"
    state = audit / "runner-state"
    (skills / "demo" / "complete-skill").mkdir(parents=True)
    (skills / "demo" / "needy-skill").mkdir(parents=True)
    (skills / "demo" / "complete-skill" / "SKILL.md").write_text(SKILL_COMPLETE, encoding="utf-8")
    (skills / "demo" / "needy-skill" / "SKILL.md").write_text(SKILL_NEEDS_PROCEDURE, encoding="utf-8")
    (home / "config.yaml").write_text("model: arbitrary-aux-model\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SKILLS_ROOT", str(skills))
    monkeypatch.setenv("HERMES_AUTORESEARCH_STATE_DIR", str(state))
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(audit))
    runner = _load("run_autoresearch_request", RUNNER)
    monkeypatch.setattr(runner, "_call_auxiliary_llm", lambda **_kwargs: _Resp())
    # These fixtures exercise the LEGACY section-scaffold loop path, which is now
    # opt-in (default off: "kein Schein"). Enable it explicitly so the scaffold
    # apply/revert assertions below still run. (AR3 is a no-op here anyway: the
    # demo skills carry no usage, so the use_count>=5 filter excludes them.)
    monkeypatch.setattr(runner, "_ENABLE_SECTION_SCAFFOLD_DISCOVERY", True)
    arr = _load("autoresearch_request", REQUEST_SCRIPT)
    return {
        "runner": runner, "arr": arr, "home": home, "skills": skills,
        "audit": audit, "state": state, "tmp": tmp_path,
    }


def _make_request(env, *, area="all", approved=False, paths=None) -> Path:
    arr = env["arr"]
    data = arr.build_request(area=area, focus="recommended_sections",
                             hermes_home=env["home"], repo_root=ROOT)
    if paths is not None:
        data["allowed_paths"] = paths
    elif area == "all":
        # restrict to the throwaway skills root only (drop the repo/skills root)
        data["allowed_paths"] = [str(env["skills"])]
    if approved:
        data["approved_by_operator"] = True
    req = env["tmp"] / "request.json"
    req.write_text(json.dumps(data), encoding="utf-8")
    return req


def _needy(env) -> Path:
    return env["skills"] / "demo" / "needy-skill" / "SKILL.md"


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------
def test_discovery_skips_archived_and_hidden_skills(env):
    arch = env["skills"] / ".archive" / "old" / "needy-archived"
    arch.mkdir(parents=True)
    (arch / "SKILL.md").write_text(SKILL_NEEDS_PROCEDURE, encoding="utf-8")
    cands = env["runner"].discover_candidates([env["skills"]], set())
    paths = [str(c["path"]) for c in cands]
    assert not any(".archive" in p for p in paths), "archived skills must be skipped"
    # the live needy skill is still a candidate
    assert any("demo/needy-skill" in p for p in paths)


def test_capability_finding_key_normalizes_missing_fields_to_empty_strings(env):
    """Findings without skill/category/evidence must dedupe under empty
    strings — a literal "None" key would let duplicate findings through
    the attempted-set and re-report the same weakness every night."""
    assert env["runner"]._capability_finding_key({}) == ("", "", "")


def test_self_test_configured_when_model_in_config(env):
    status, detail = env["runner"].self_test()
    assert status == "configured"
    assert "skills_hub" in detail


def test_self_test_configured_without_minimax_string(env, monkeypatch):
    (env["home"] / "config.yaml").write_text("model: something-else\n", encoding="utf-8")
    status, detail = env["runner"].self_test()
    assert status == "configured"
    assert "skills_hub" in detail
    assert "arbitrary-aux-model" not in detail


def test_self_test_yellow_when_model_ping_fails(env, monkeypatch):
    def _boom(**_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(env["runner"], "_call_auxiliary_llm", _boom)
    status, detail = env["runner"].self_test()
    assert status == "yellow"
    assert "RuntimeError" in detail


# --------------------------------------------------------------------------
# Dry-run: mutates nothing
# --------------------------------------------------------------------------
def test_dry_run_mutates_nothing_and_proposes(env):
    before = _needy(env).read_bytes()
    req = _make_request(env)
    summary = env["runner"].run(req, apply=False, confirm=False, max_iterations=3)
    assert summary["ok"] is True
    assert summary["mode"] == "dry-run"
    assert summary["proposed"] >= 1
    assert summary["kept"] == 0 and summary["reverted"] == 0
    assert _needy(env).read_bytes() == before  # untouched
    # results + receipt landed in the throwaway audit dir, not the real one
    assert (env["audit"] / "autoresearch_results.tsv").exists()
    assert summary["receipt"].startswith(str(env["audit"]))
    # lock released → idle
    assert not (env["state"] / "current.lock").exists()
    status = json.loads((env["state"] / "current.status").read_text())
    assert status["state"] == "idle"


# --------------------------------------------------------------------------
# Apply: keeps a genuine improvement, ends clean
# --------------------------------------------------------------------------
def test_apply_keeps_improvement(env):
    req = _make_request(env, approved=True)
    summary = env["runner"].run(req, apply=True, confirm=True, max_iterations=2)
    assert summary["ok"] is True
    assert summary["mode"] == "apply"
    assert summary["kept"] >= 1
    text = _needy(env).read_text()
    assert "## Procedure" in text
    # backup of the original was taken
    assert summary["backup_dir"] and Path(summary["backup_dir"]).exists()
    # eval is clean afterwards
    errs, _warns = env["runner"].evals.check_skill(_needy(env))
    assert errs == []


# --------------------------------------------------------------------------
# Apply: reverts on regression (scaffolder monkeypatched to corrupt the file)
# --------------------------------------------------------------------------
def test_apply_reverts_on_regression(env, monkeypatch):
    original = _needy(env).read_bytes()

    def _corrupt(path, label):
        path.write_text("\x00 broken", encoding="utf-8")  # NUL byte -> eval error
        return "corrupt"

    monkeypatch.setattr(env["runner"], "apply_scaffold", _corrupt)
    req = _make_request(env, approved=True)
    summary = env["runner"].run(req, apply=True, confirm=True, max_iterations=1)
    assert summary["reverted"] >= 1
    assert summary["kept"] == 0
    assert _needy(env).read_bytes() == original  # restored from backup


def test_apply_crash_restores_in_flight_file_and_reports_error(env, monkeypatch):
    original = _needy(env).read_bytes()

    def _boom(_path, _target_warning, _before_warnings):
        raise RuntimeError("crash after mutation")

    monkeypatch.setattr(env["runner"], "eval_gate", _boom)
    req = _make_request(env, approved=True)

    summary = env["runner"].run(req, apply=True, confirm=True, max_iterations=1)

    assert _needy(env).read_bytes() == original
    assert summary["ok"] is False
    assert summary["errored"] is True
    assert summary["error"] == "run failed: RuntimeError"
    receipt_text = Path(summary["receipt"]).read_text(encoding="utf-8")
    assert "- errored: True" in receipt_text
    assert "- error: run failed: RuntimeError" in receipt_text

    assert env["runner"].main([str(req), "--apply", "--confirm", "--max-iterations", "1"]) == 2
    assert _needy(env).read_bytes() == original


def test_apply_crash_restores_only_in_flight_file(env, monkeypatch):
    later = env["skills"] / "demo" / "zz-later-skill" / "SKILL.md"
    later.parent.mkdir(parents=True)
    later.write_text(SKILL_NEEDS_PROCEDURE.replace("needy-skill", "zz-later-skill"), encoding="utf-8")
    first_original = _needy(env).read_text(encoding="utf-8")
    later_original = later.read_text(encoding="utf-8")
    original_eval_gate = env["runner"].eval_gate
    calls = []

    def _crash_second(path, target_warning, before_warnings):
        calls.append(path)
        if len(calls) == 2:
            raise RuntimeError("crash after second mutation")
        return original_eval_gate(path, target_warning, before_warnings)

    monkeypatch.setattr(env["runner"], "eval_gate", _crash_second)
    req = _make_request(env, approved=True)

    summary = env["runner"].run(req, apply=True, confirm=True, max_iterations=2)

    assert summary["ok"] is False
    assert summary["errored"] is True
    assert len(calls) == 2
    assert "## Procedure" in _needy(env).read_text(encoding="utf-8")
    assert _needy(env).read_text(encoding="utf-8") != first_original
    assert later.read_text(encoding="utf-8") == later_original


# --------------------------------------------------------------------------
# Apply gating
# --------------------------------------------------------------------------
def test_apply_refused_without_confirm(env):
    req = _make_request(env, approved=False)
    summary = env["runner"].run(req, apply=True, confirm=False)
    assert summary["ok"] is False
    assert "confirm" in summary["refused"]


def test_apply_refused_outside_skills(env):
    # area "dashboard" resolves to repo scripts/tests -> outside ~/.hermes/skills
    arr = env["arr"]
    data = arr.build_request(area="dashboard", focus="x",
                             hermes_home=env["home"], repo_root=ROOT)
    data["approved_by_operator"] = True
    req = env["tmp"] / "dash.json"
    req.write_text(json.dumps(data), encoding="utf-8")
    summary = env["runner"].run(req, apply=True, confirm=True)
    assert summary["ok"] is False
    assert "under ~/.hermes/skills" in summary["refused"]


def test_apply_succeeds_when_request_also_lists_outside_repo_skills(env):
    """area=all carries both ~/.hermes/skills and repo/skills; the outside repo
    root must NOT block apply — we just don't edit there."""
    arr = env["arr"]
    data = arr.build_request(area="all", focus="recommended_sections",
                             hermes_home=env["home"], repo_root=ROOT)
    # keep the real under-skills root AND an outside sibling repo skills root
    data["allowed_paths"] = [str(env["skills"]), str(ROOT / "skills")]
    data["approved_by_operator"] = True
    req = env["tmp"] / "mixed.json"
    req.write_text(json.dumps(data), encoding="utf-8")
    summary = env["runner"].run(req, apply=True, confirm=True, max_iterations=1)
    assert summary["ok"] is True and summary["mode"] == "apply"
    assert summary["kept"] >= 1
    assert "## Procedure" in _needy(env).read_text()


def test_apply_downgrades_to_dry_run_when_selftest_not_configured(env, monkeypatch):
    monkeypatch.setattr(env["runner"], "_resolve_autoresearch_aux_slot", lambda: ("", ""))
    before = _needy(env).read_bytes()
    req = _make_request(env, approved=True)
    summary = env["runner"].run(req, apply=True, confirm=True, max_iterations=2)
    assert summary["mode"] == "dry-run"
    assert _needy(env).read_bytes() == before


def test_double_run_refused_while_fresh_lock(env):
    state = env["state"]
    state.mkdir(parents=True, exist_ok=True)
    (state / "current.lock").write_text(json.dumps({"pid": os.getpid(), "request_id": "other"}), encoding="utf-8")
    (state / "current.heartbeat").write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
    req = _make_request(env)
    summary = env["runner"].run(req, apply=False, confirm=False)
    assert summary["ok"] is False
    assert "already in progress" in summary["refused"]


def test_apply_confirm_flag_alone_satisfies_operator_gate(env):
    """--confirm and approved_by_operator are ALTERNATIVES: the CLI flag
    alone must open the gate even when the request carries no approval."""
    req = _make_request(env, approved=False)
    summary = env["runner"].run(req, apply=True, confirm=True, max_iterations=1)
    assert summary["ok"] is True
    assert summary["mode"] == "apply"


def test_finish_status_reports_stopped_by_signal_note(env):
    """A SIGTERM-stopped run must surface 'stopped by signal' in the idle
    status note — the dashboard reads it; without it the stop is silent."""
    summary = {"stopped": True, "refused": None, "steps": []}
    env["runner"]._finish_status(env["state"], "configured", summary)
    status = json.loads((env["state"] / "current.status").read_text(encoding="utf-8"))
    assert status["state"] == "idle"
    assert status["note"] == "stopped by signal"


# --------------------------------------------------------------------------
# SIGTERM stop on a paced dry-run loop (real subprocess)
# --------------------------------------------------------------------------
def test_sigterm_stops_loop_and_releases_lock(env):
    (env["home"] / "config.yaml").write_text("model: something-else\n", encoding="utf-8")
    req = _make_request(env)
    e = dict(os.environ)
    e["HERMES_AUTORESEARCH_STEP_SLEEP"] = "2"  # pace the loop so we can interrupt it
    proc = subprocess.Popen(
        [sys.executable, str(RUNNER), str(req), "--max-iterations", "5"],
        env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    lock = env["state"] / "current.lock"
    for _ in range(50):
        if lock.exists():
            break
        time.sleep(0.1)
    assert lock.exists(), "runner never acquired the lock"
    proc.send_signal(signal.SIGTERM)
    out, _err = proc.communicate(timeout=15)
    assert not lock.exists(), "lock not released after SIGTERM"
    status = json.loads((env["state"] / "current.status").read_text())
    assert status["state"] == "idle"


def test_dry_run_nightly_observability_clean_research_counts(env, monkeypatch):
    monkeypatch.setattr(env["runner"], "_usage_min_use_count", lambda: 0)
    orig_research_skills = env["runner"].capability_researcher.research_skills

    class _FindingsMsg:
        content = '{"findings": []}'

    class _FindingsChoice:
        message = _FindingsMsg()

    class _FindingsResp:
        choices = [_FindingsChoice()]

    def _ok_call_llm(**_kwargs):
        return _FindingsResp()

    def _wrapped_research_skills(skills, **kwargs):
        return orig_research_skills(skills, call_llm=_ok_call_llm, **kwargs)

    monkeypatch.setattr(env["runner"].capability_researcher, "research_skills", _wrapped_research_skills)

    req = _make_request(env)
    summary = env["runner"].run(req, apply=False, confirm=False, max_iterations=3)
    assert summary["ok"] is True
    assert summary["skills_researched"] >= 2
    assert summary["research_errors"] == 0


def test_dry_run_nightly_observability_receipt_shows_research_errors(env, monkeypatch):
    monkeypatch.setattr(env["runner"], "_usage_min_use_count", lambda: 0)
    orig_research_skills = env["runner"].capability_researcher.research_skills

    def _boom_call_llm(**_kwargs):
        raise RuntimeError("nightly-research-offline")

    def _wrapped_research_skills(skills, **kwargs):
        return orig_research_skills(skills, call_llm=_boom_call_llm, **kwargs)

    monkeypatch.setattr(env["runner"].capability_researcher, "research_skills", _wrapped_research_skills)

    req = _make_request(env)
    summary = env["runner"].run(req, apply=False, confirm=False, max_iterations=2)
    assert summary["research_errors"] >= 1
    assert summary["outcome"] == "infra_failed"
    assert summary["ok"] is False
    receipt_text = Path(summary["receipt"]).read_text(encoding="utf-8")
    assert "research_errors" in receipt_text
    assert "outcome: infra_failed" in receipt_text


def test_invalid_lane_contract_still_writes_failure_receipt(env, monkeypatch):
    from hermes_cli import autoresearch_lane_contracts

    monkeypatch.setattr(env["runner"], "discover_capability_candidates", lambda *_a, **_k: [])
    monkeypatch.setattr(env["runner"], "_ENABLE_SECTION_SCAFFOLD_DISCOVERY", False)
    monkeypatch.setattr(
        autoresearch_lane_contracts,
        "classify_lane_outcome",
        lambda *_a, **_k: (_ for _ in ()).throw(
            autoresearch_lane_contracts.LaneContractError("invalid test override")
        ),
    )

    summary = env["runner"].run(
        _make_request(env), apply=False, confirm=False, max_iterations=1
    )

    assert summary["ok"] is False
    assert summary["outcome"] == "invalid_output"
    assert Path(summary["receipt"]).exists()
    assert "outcome: invalid_output" in Path(summary["receipt"]).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Second pass: fallback-normalisation units
# ---------------------------------------------------------------------------

def test_resolve_aux_slot_and_model_label_fall_back_cleanly(monkeypatch):
    """A None provider/model must normalise to '' / the configured label —
    the literal string 'None' would leak into receipts and status lines."""
    import types as _types

    runner = _load("run_autoresearch_request", RUNNER)

    monkeypatch.setattr(
        "agent.auxiliary_client._resolve_task_provider_model",
        lambda task: (None, "model-x", "", "", ""),
    )
    assert runner._resolve_autoresearch_aux_slot() == ("", "model-x")

    assert runner._response_model_label(
        _types.SimpleNamespace(model=None), "cfg-label"
    ) == "cfg-label"
    assert runner._response_model_label(
        _types.SimpleNamespace(model=""), "cfg-label"
    ) == "cfg-label"


def test_lock_is_fresh_without_heartbeat_checks_pid_liveness(tmp_path):
    """A lock WITHOUT a heartbeat is fresh only while its pid lives — the
    fallback must read the pid, not crash on a missing heartbeat file."""
    runner = _load("run_autoresearch_request", RUNNER)
    state = tmp_path / "state"
    state.mkdir()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    (state / "current.lock").write_text(
        json.dumps({"pid": proc.pid}), encoding="utf-8"
    )
    assert runner._lock_is_fresh(state) is False


def test_discover_capability_candidates_normalises_sparse_report(tmp_path, monkeypatch):
    """A sparse researcher report (None counters) must normalise to zeros
    and still carry usage_source/budget_stop/findings — int(None) would
    kill the nightly mid-scan."""
    runner = _load("run_autoresearch_request", RUNNER)
    root = tmp_path / "skills"
    (root / "demo" / "my-skill").mkdir(parents=True)
    (root / "demo" / "my-skill" / "SKILL.md").write_text("skill text", encoding="utf-8")
    monkeypatch.setattr(runner, "_load_skill_usage_from_root", lambda r: {"my-skill": 9.0})
    monkeypatch.setattr(runner, "_usage_min_use_count", lambda: 5)
    sparse_report = {
        "skills_seen": 1,
        "errors": None,
        "skills_with_findings": None,
        "tokens": None,
        "usage_source": "estimated",
        "reason": "budget exhausted: weekly cap",
        "findings": [{"skill": "my-skill"}],
    }
    monkeypatch.setattr(
        runner.capability_researcher,
        "research_skills",
        lambda skills, usage, on_skill: sparse_report,
    )

    stats: dict = {}
    cands = runner.discover_capability_candidates([root], set(), stats=stats)

    assert stats["skills_researched"] == 1
    assert stats["research_errors"] == 0
    assert stats["skills_with_findings"] == 0
    assert stats["research_tokens"] == 0
    assert stats["usage_source"] == "estimated"
    assert stats["budget_stop"] == "budget exhausted: weekly cap"
    assert [c["path"].name for c in cands] == ["SKILL.md"]


def test_write_receipt_marks_missing_backup_dir_as_dry_run(tmp_path, monkeypatch):
    """A run without backup_dir records '(none — dry-run)' verbatim — the
    literal 'None' would make a dry-run look like a lost path."""
    runner = _load("run_autoresearch_request", RUNNER)
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(tmp_path / "audit"))

    path = runner.write_receipt({"request_id": "r1"})

    assert "- backup_dir: (none — dry-run)" in path.read_text(encoding="utf-8")
