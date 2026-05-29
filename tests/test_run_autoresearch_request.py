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
    home = tmp_path / "home"
    skills = home / "skills"
    audit = tmp_path / "audit"
    state = audit / "runner-state"
    (skills / "demo" / "complete-skill").mkdir(parents=True)
    (skills / "demo" / "needy-skill").mkdir(parents=True)
    (skills / "demo" / "complete-skill" / "SKILL.md").write_text(SKILL_COMPLETE, encoding="utf-8")
    (skills / "demo" / "needy-skill" / "SKILL.md").write_text(SKILL_NEEDS_PROCEDURE, encoding="utf-8")
    (home / "config.yaml").write_text("model: MiniMax-M2.7-highspeed\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SKILLS_ROOT", str(skills))
    monkeypatch.setenv("HERMES_AUTORESEARCH_STATE_DIR", str(state))
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(audit))
    runner = _load("run_autoresearch_request", RUNNER)
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
def test_self_test_configured_when_model_in_config(env):
    status, _detail = env["runner"].self_test()
    assert status == "configured"


def test_self_test_unavailable_when_model_absent(env, monkeypatch):
    (env["home"] / "config.yaml").write_text("model: something-else\n", encoding="utf-8")
    status, _detail = env["runner"].self_test()
    assert status == "unavailable"


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
    assert "outside ~/.hermes/skills" in summary["refused"]


def test_apply_downgrades_to_dry_run_when_selftest_not_configured(env):
    (env["home"] / "config.yaml").write_text("model: other\n", encoding="utf-8")
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


# --------------------------------------------------------------------------
# SIGTERM stop on a paced dry-run loop (real subprocess)
# --------------------------------------------------------------------------
def test_sigterm_stops_loop_and_releases_lock(env):
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
