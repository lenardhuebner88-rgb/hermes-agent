from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import test_foundry
from hermes_cli._ast_mutator import Mutant


def test_affected_tests_derivation_finds_top_level_and_subdir(tmp_path):
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "test_kanban_db.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "unit" / "test_kanban_db.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_other.py").write_text("", encoding="utf-8")

    assert test_foundry._affected_tests(tmp_path, "hermes_cli/kanban_db.py") == [
        "tests/test_kanban_db.py",
        "tests/unit/test_kanban_db.py",
    ]


def _make_fake_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    target_rel = "hermes_cli/sample_target.py"
    (repo / "hermes_cli").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / target_rel).write_text(
        "def classify(value):\n"
        "    if value > 0:\n"
        "        return 'positive'\n"
        "    return 'other'\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_sample_target.py").write_text(
        "from hermes_cli.sample_target import classify\n\n"
        "def test_existing():\n"
        "    assert classify(1) == 'positive'\n",
        encoding="utf-8",
    )
    return repo, target_rel


def _install_fake_isolation(monkeypatch, tmp_path: Path, repo: Path):
    worktrees: list[Path] = []

    def create_worktree() -> Path:
        wt = tmp_path / f"worktree-{len(worktrees)}"
        shutil.copytree(repo, wt)
        worktrees.append(wt)
        return wt

    monkeypatch.setattr(test_foundry, "_REPO", repo)
    monkeypatch.setattr(test_foundry, "_target_is_clean", lambda _rel: True)
    monkeypatch.setattr(test_foundry, "_create_worktree", create_worktree)
    monkeypatch.setattr(test_foundry, "_remove_worktree", lambda path: shutil.rmtree(path, ignore_errors=True))
    monkeypatch.setattr(test_foundry, "_create_hermes_home", lambda: tmp_path / "hermes-home")
    monkeypatch.setattr(test_foundry, "_remove_hermes_home", lambda _path: None)
    monkeypatch.setattr(test_foundry, "_record_roi", lambda **_kwargs: None)
    return worktrees


def _mutants() -> list[Mutant]:
    return [
        Mutant(
            mutated_source=(
                "def classify(value):\n"
                "    # SURVIVOR_MUTANT\n"
                "    if value >= 0:\n"
                "        return 'positive'\n"
                "    return 'other'\n"
            ),
            operator="comparison_swap",
            lineno=2,
            description="> -> >=",
        ),
        Mutant(
            mutated_source=(
                "def classify(value):\n"
                "    # OTHER_MUTANT\n"
                "    if value > 1:\n"
                "        return 'positive'\n"
                "    return 'other'\n"
            ),
            operator="const_offset",
            lineno=2,
            description="0 -> 1",
        ),
    ]


def test_survivor_generated_test_kept_after_head_and_mutant_gate(tmp_path, monkeypatch):
    repo, target_rel = _make_fake_repo(tmp_path)
    worktrees = _install_fake_isolation(monkeypatch, tmp_path, repo)
    monkeypatch.setattr(test_foundry, "generate_mutants", lambda _source, **_kwargs: _mutants())
    saved = {}

    def save_proposal(**kwargs):
        saved["kwargs"] = kwargs
        return "proposal-1"

    monkeypatch.setattr(test_foundry, "_save_test_proposal", save_proposal)

    def llm_call(**kwargs):
        assert kwargs["task"] == "test_hardening"
        return {
            "test_code": (
                "from hermes_cli.sample_target import classify\n\n"
                "def test_foundry_public_behavior():\n"
                "    assert classify(0) == 'other'\n"
            ),
            "tokens": 9,
            "model": "unit-hardener",
        }

    def run_suite(paths, *, cwd, env):
        assert env["HERMES_HOME"].endswith("hermes-home")
        assert "tests/test_sample_target.py" in paths
        source = (cwd / target_rel).read_text(encoding="utf-8")
        generated = cwd / "tests" / "test_sample_target_foundry.py"
        if generated.exists() and "SURVIVOR_MUTANT" in source:
            return False
        if not generated.exists() and "OTHER_MUTANT" in source:
            return False
        return True

    result = test_foundry.run_test_foundry(target_rel, llm_call=llm_call, run_suite=run_suite)

    assert result["ok"] is True
    assert result["tests_kept"] == 1
    assert result["proposals"] == ["proposal-1"]
    assert result["tokens"] == 9
    assert result["model"] == "unit-hardener"
    assert result["mutants_run"] == 2
    assert saved["kwargs"]["target_module"] == target_rel
    assert not (worktrees[0] / "tests" / "test_sample_target_foundry.py").exists()


def test_tautology_test_green_on_mutant_is_rejected(tmp_path, monkeypatch):
    repo, target_rel = _make_fake_repo(tmp_path)
    _install_fake_isolation(monkeypatch, tmp_path, repo)
    monkeypatch.setattr(test_foundry, "generate_mutants", lambda _source, **_kwargs: _mutants())
    monkeypatch.setattr(test_foundry, "_save_test_proposal", lambda **_kwargs: "should-not-save")

    def llm_call(**_kwargs):
        return {
            "test_code": "def test_tautology():\n    assert True\n",
            "tokens": 3,
            "model": "unit-hardener",
        }

    result = test_foundry.run_test_foundry(target_rel, llm_call=llm_call, run_suite=lambda *_a, **_k: True)

    assert result["ok"] is False
    assert result["tests_kept"] == 0
    assert result["proposals"] == []
    assert "green_head=True, red_mutant=False" in result["survivors"][0]["reason"]


def test_source_inspecting_generated_test_is_rejected(tmp_path, monkeypatch):
    repo, target_rel = _make_fake_repo(tmp_path)
    _install_fake_isolation(monkeypatch, tmp_path, repo)
    monkeypatch.setattr(test_foundry, "generate_mutants", lambda _source, **_kwargs: _mutants())
    monkeypatch.setattr(test_foundry, "_save_test_proposal", lambda **_kwargs: "should-not-save")
    calls = {"suite": 0}

    def llm_call(**_kwargs):
        return {
            "test_code": (
                "import inspect\n"
                "from hermes_cli.sample_target import classify\n\n"
                "def test_bad():\n"
                "    assert '>= 0' not in inspect.getsource(classify)\n"
            ),
            "tokens": 2,
            "model": "unit-hardener",
        }

    def run_suite(*_args, **_kwargs):
        calls["suite"] += 1
        return True

    result = test_foundry.run_test_foundry(target_rel, llm_call=llm_call, run_suite=run_suite)

    assert result["tests_kept"] == 0
    assert result["survivors"][0]["reason"] == "generated test inspects source"
    assert calls["suite"] == 3  # baseline plus one run per survivor mutant


def test_apply_none_leaves_real_target_unchanged(tmp_path, monkeypatch):
    repo, target_rel = _make_fake_repo(tmp_path)
    original = (repo / target_rel).read_text(encoding="utf-8")
    _install_fake_isolation(monkeypatch, tmp_path, repo)
    monkeypatch.setattr(test_foundry, "generate_mutants", lambda _source, **_kwargs: _mutants()[:1])
    monkeypatch.setattr(test_foundry, "_save_test_proposal", lambda **_kwargs: "proposal-1")

    def llm_call(**_kwargs):
        return {"test_code": "def test_public():\n    assert True\n"}

    def run_suite(paths, *, cwd, env):
        source = (cwd / target_rel).read_text(encoding="utf-8")
        generated = cwd / "tests" / "test_sample_target_foundry.py"
        if generated.exists() and "SURVIVOR_MUTANT" in source:
            return False
        return True

    test_foundry.run_test_foundry(target_rel, llm_call=llm_call, run_suite=run_suite)

    assert (repo / target_rel).read_text(encoding="utf-8") == original
    assert not (repo / "tests" / "test_sample_target_foundry.py").exists()


def test_endpoint_trigger_returns_409_when_test_foundry_running(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_FOUNDRY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(tmp_path / "audit"))
    import hermes_cli.autoresearch_view as view

    importlib.reload(test_foundry)
    view = importlib.reload(view)
    monkeypatch.setattr(view, "_spawn_test_foundry_runner", lambda _path: 4242)

    app = FastAPI()
    view.register_autoresearch_routes(app)
    client = TestClient(app)

    first = client.post(
        "/api/autoresearch/test-foundry/trigger",
        json={"target": "hermes_cli/kanban_db.py", "max_mutants": 1, "apply": False},
    )
    assert first.status_code == 200, first.text
    assert first.json()["pid"] == 4242

    busy = client.post(
        "/api/autoresearch/test-foundry/trigger",
        json={"target": "hermes_cli/kanban_db.py"},
    )
    assert busy.status_code == 409

    status = client.get("/api/autoresearch/test-foundry/status").json()
    assert status["state"] == "running"
    assert status["target"] == "hermes_cli/kanban_db.py"

    targets = client.get("/api/autoresearch/test-foundry/targets").json()
    assert "hermes_cli/kanban_db.py" in targets["targets"]


# ---------------------------------------------------------------------------
# FIX 1 — unique test names when several kept blocks are combined for apply
# ---------------------------------------------------------------------------
def test_uniquify_test_names_disambiguates_colliding_defs():
    block_a = (
        "from hermes_cli.sample_target import classify\n\n"
        "def test_foundry_public_behavior():\n"
        "    assert classify(0) == 'other'\n"
    )
    block_b = (
        "from hermes_cli.sample_target import classify\n\n"
        "def helper():\n"
        "    return 1\n\n"
        "def test_foundry_public_behavior(  ):\n"
        "    assert classify(1) == 'positive'\n"
    )
    ua = test_foundry._uniquify_test_names(block_a.strip(), "comparison_swap_2_0")
    ub = test_foundry._uniquify_test_names(block_b.strip(), "const_offset_2_1")
    combined = "\n\n".join([ua, ub]) + "\n"

    # Both originally-colliding test functions survive under distinct names.
    assert "def test_foundry_public_behavior__comparison_swap_2_0(" in combined
    assert "def test_foundry_public_behavior__const_offset_2_1(" in combined
    assert combined.count("def test_") == 2
    # Non-test helpers are left untouched.
    assert "def helper():" in combined
    # The merged module is valid Python (no duplicate top-level names).
    import ast

    tree = ast.parse(combined)
    test_names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    assert len(test_names) == len(set(test_names)) == 2


def test_apply_path_uniquifies_each_kept_block_with_stable_suffix(tmp_path, monkeypatch):
    """The combine site feeds every kept block through ``_uniquify_test_names``
    with a per-block suffix derived from the mutant (operator/lineno/index), so
    colliding LLM function names can never silently overwrite one another."""
    repo, target_rel = _make_fake_repo(tmp_path)
    _install_fake_isolation(monkeypatch, tmp_path, repo)
    monkeypatch.setattr(test_foundry, "generate_mutants", lambda _source, **_kwargs: _mutants())
    monkeypatch.setattr(test_foundry, "_save_test_proposal", lambda **_kwargs: "pid")
    monkeypatch.setattr(test_foundry, "_annotate_applied_proposals", lambda *a, **k: None)

    uniq_calls: list[tuple[str, str]] = []
    real_uniquify = test_foundry._uniquify_test_names

    def spy_uniquify(code, suffix):
        uniq_calls.append((code, suffix))
        return real_uniquify(code, suffix)

    monkeypatch.setattr(test_foundry, "_uniquify_test_names", spy_uniquify)

    captured: dict[str, str] = {}

    def fake_apply(*, worktree, branch, target_module, test_code, affected_tests, run_suite, env):
        captured["test_code"] = test_code
        return {"ok": True, "branch": branch, "commit": "deadbeef", "test_file": "tests/x_foundry.py"}

    monkeypatch.setattr(test_foundry, "_apply_branch_commit", fake_apply)

    def llm_call(**_kwargs):
        return {
            "test_code": (
                "from hermes_cli.sample_target import classify\n\n"
                "def test_foundry_public_behavior():\n"
                "    assert classify(0) == 'other'\n"
            ),
            "tokens": 1,
            "model": "m",
        }

    # Mirrors the proven single-survivor gate: the generated test kills the
    # comparison_swap survivor while staying green on HEAD and the other mutant.
    def run_suite(paths, *, cwd, env):
        source = (cwd / target_rel).read_text(encoding="utf-8")
        generated = cwd / "tests" / "test_sample_target_foundry.py"
        if generated.exists() and "SURVIVOR_MUTANT" in source:
            return False
        if not generated.exists() and "OTHER_MUTANT" in source:
            return False
        return True

    result = test_foundry.run_test_foundry(
        target_rel, apply_branch="f-test-foundry", llm_call=llm_call, run_suite=run_suite
    )

    assert result["tests_kept"] == 1
    # Exactly one kept block was uniquified, with a stable, mutant-derived suffix.
    assert len(uniq_calls) == 1
    _code, suffix = uniq_calls[0]
    assert suffix == "comparison_swap_2_0"  # operator_lineno_index of the survivor
    # The committed body carries the suffixed test name (collision-proof).
    assert "def test_foundry_public_behavior__comparison_swap_2_0(" in captured["test_code"]


# ---------------------------------------------------------------------------
# FIX 2 — apply branch/commit surfaced in result and persisted on proposals
# ---------------------------------------------------------------------------
def test_apply_branch_and_commit_surfaced_and_persisted(tmp_path, monkeypatch):
    repo, target_rel = _make_fake_repo(tmp_path)
    _install_fake_isolation(monkeypatch, tmp_path, repo)
    monkeypatch.setattr(test_foundry, "generate_mutants", lambda _source, **_kwargs: _mutants())
    monkeypatch.setattr(test_foundry, "_save_test_proposal", lambda **_kwargs: "pid-7")

    annotated: dict[str, object] = {}

    def fake_annotate(proposal_ids, *, branch, commit, test_file):
        annotated.update(ids=list(proposal_ids), branch=branch, commit=commit, test_file=test_file)

    monkeypatch.setattr(test_foundry, "_annotate_applied_proposals", fake_annotate)

    def fake_apply(*, worktree, branch, target_module, test_code, affected_tests, run_suite, env):
        return {"ok": True, "branch": branch, "commit": "abc123def456", "test_file": "tests/t_foundry.py"}

    monkeypatch.setattr(test_foundry, "_apply_branch_commit", fake_apply)

    def llm_call(**_kwargs):
        return {
            "test_code": (
                "from hermes_cli.sample_target import classify\n\n"
                "def test_public():\n"
                "    assert classify(0) == 'other'\n"
            ),
            "tokens": 1,
            "model": "m",
        }

    def run_suite(paths, *, cwd, env):
        source = (cwd / target_rel).read_text(encoding="utf-8")
        generated = cwd / "tests" / "test_sample_target_foundry.py"
        if generated.exists() and "SURVIVOR_MUTANT" in source:
            return False
        if not generated.exists() and "OTHER_MUTANT" in source:
            return False
        return True

    result = test_foundry.run_test_foundry(
        target_rel, apply_branch="f-test-foundry", llm_call=llm_call, run_suite=run_suite
    )

    assert result["apply_branch"] == "f-test-foundry"
    assert result["apply_commit"] == "abc123def456"
    assert annotated["branch"] == "f-test-foundry"
    assert annotated["commit"] == "abc123def456"
    assert annotated["ids"] == result["proposals"]


def test_annotate_applied_proposals_records_branch_on_store(tmp_path, monkeypatch):
    """The real persistence helper stamps the saved proposal with branch/commit
    while keeping it out of the manual apply-gate (status flips to applied)."""
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(tmp_path / "audit"))
    from hermes_cli import autoresearch_proposals

    autoresearch_proposals = importlib.reload(autoresearch_proposals)
    importlib.reload(test_foundry)

    pid = "test-foundry-sample-deadbeefdeadbeef"
    autoresearch_proposals.save_proposal(
        {
            "id": pid,
            "schema": autoresearch_proposals.PROPOSAL_SCHEMA,
            "mode": "test",
            "status": "proposed",
            "apply_blocked_reason": "Use the test-foundry apply branch gate to apply automatically.",
            "new_text": "def test_x():\n    assert True\n",
        }
    )

    test_foundry._annotate_applied_proposals(
        [pid], branch="f-test-foundry", commit="abcdef1234567890", test_file="tests/t_foundry.py"
    )

    stored = autoresearch_proposals.load_proposal(pid)
    assert stored is not None
    assert stored["status"] == "applied"
    assert stored["apply_branch"] == "f-test-foundry"
    assert stored["apply_commit"] == "abcdef1234567890"
    assert stored["apply_test_file"] == "tests/t_foundry.py"
    # Still blocked from the manual autoresearch apply-gate.
    assert stored["apply_blocked_reason"]
    # Reload modules so later tests see default (unpatched) audit dir.
    importlib.reload(autoresearch_proposals)
    importlib.reload(test_foundry)


# ---------------------------------------------------------------------------
# FIX 3 — security invariants that the mocked happy-path tests do NOT cover
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("branch", ["main", "master", "f-autoresearch-v2"])
def test_apply_branch_commit_refuses_protected_branches(tmp_path, branch):
    """(a) The commit path bails on protected branches BEFORE touching git, so no
    real repo or worktree is required to prove the refusal."""
    sentinel = tmp_path / "must-not-be-touched"
    # If the guard let execution through, git would run against this bogus cwd
    # and raise; instead we expect a clean {ok: False} refusal.
    out = test_foundry._apply_branch_commit(
        worktree=sentinel,
        branch=branch,
        target_module="hermes_cli/kanban_db.py",
        test_code="def test_x():\n    assert True\n",
        affected_tests=["tests/test_kanban_db.py"],
        run_suite=lambda *_a, **_k: True,
        env={},
    )
    assert out["ok"] is False
    assert branch in out["reason"]
    assert "protected" in out["reason"]
    # No worktree dir was created as a side effect.
    assert not sentinel.exists()


def test_target_relpath_rejects_escapes_and_foreign_checkouts(tmp_path, monkeypatch):
    """(b) Path resolution rejects parent-dir escapes, absolute paths outside the
    repo, and paths that live in a *different* checkout — pure function, no git."""
    repo = tmp_path / "repo"
    (repo / "hermes_cli").mkdir(parents=True)
    monkeypatch.setattr(test_foundry, "_REPO", repo)

    # Sanity: a legitimate repo-relative path resolves unchanged.
    assert test_foundry._target_relpath("hermes_cli/kanban_db.py") == "hermes_cli/kanban_db.py"

    # Relative parent-dir escapes.
    for bad in ["../x", "..", "a/../../etc/passwd", "../../hermes-agent/secret.py"]:
        with pytest.raises(ValueError):
            test_foundry._target_relpath(bad)

    # Absolute path outside the repo.
    with pytest.raises(ValueError):
        test_foundry._target_relpath(str(tmp_path / "outside" / "x.py"))

    # Absolute path inside ANOTHER checkout (sibling working copy).
    foreign = tmp_path / "other-checkout" / "hermes_cli" / "kanban.py"
    with pytest.raises(ValueError):
        test_foundry._target_relpath(str(foreign))

    # The literal live-checkout path must also be refused from this worktree.
    with pytest.raises(ValueError):
        test_foundry._target_relpath("/home/piet/.hermes/hermes-agent/hermes_cli/kanban.py")


def test_run_refuses_dirty_target_without_creating_worktree(tmp_path, monkeypatch):
    """(c) When the target is dirty in the main checkout the run aborts with a
    reason and NEVER creates a worktree."""
    repo, target_rel = _make_fake_repo(tmp_path)
    monkeypatch.setattr(test_foundry, "_REPO", repo)
    monkeypatch.setattr(test_foundry, "_target_is_clean", lambda _rel: False)
    monkeypatch.setattr(test_foundry, "_record_roi", lambda **_kwargs: None)

    created = {"n": 0}

    def boom_create():
        created["n"] += 1
        raise AssertionError("worktree must not be created when target is dirty")

    monkeypatch.setattr(test_foundry, "_create_worktree", boom_create)

    result = test_foundry.run_test_foundry(
        target_rel, llm_call=lambda **_k: {"test_code": ""}, run_suite=lambda *_a, **_k: True
    )

    assert result["ok"] is False
    assert "not clean" in result["reason"]
    assert created["n"] == 0


def test_target_is_clean_detects_real_dirty_file(tmp_path, monkeypatch):
    """(c, complementary) The real git-backed cleanliness probe returns True for a
    committed file and False once it is modified — a tiny hermetic git repo."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "hermes_cli").mkdir()
    target = "hermes_cli/sample.py"
    (repo / target).write_text("x = 1\n", encoding="utf-8")
    git("add", target)
    git("commit", "-q", "-m", "init")

    monkeypatch.setattr(test_foundry, "_REPO", repo)
    assert test_foundry._target_is_clean(target) is True

    (repo / target).write_text("x = 2\n", encoding="utf-8")
    assert test_foundry._target_is_clean(target) is False


def test_worktree_removed_when_loop_raises(tmp_path, monkeypatch):
    """(d) If an exception is raised mid-run, the worktree is still torn down
    exactly once (finally) and the function returns {ok: False} instead of
    propagating the exception."""
    repo, target_rel = _make_fake_repo(tmp_path)
    monkeypatch.setattr(test_foundry, "_REPO", repo)
    monkeypatch.setattr(test_foundry, "_target_is_clean", lambda _rel: True)
    monkeypatch.setattr(test_foundry, "_record_roi", lambda **_kwargs: None)

    fake_wt = tmp_path / "wt"
    shutil.copytree(repo, fake_wt)
    monkeypatch.setattr(test_foundry, "_create_worktree", lambda: fake_wt)
    monkeypatch.setattr(test_foundry, "_create_hermes_home", lambda: tmp_path / "home")
    monkeypatch.setattr(test_foundry, "_remove_hermes_home", lambda _p: None)

    removed = {"calls": [], "n": 0}

    def spy_remove(path):
        removed["n"] += 1
        removed["calls"].append(path)

    monkeypatch.setattr(test_foundry, "_remove_worktree", spy_remove)

    def exploding_suite(*_a, **_k):
        raise RuntimeError("suite blew up")

    result = test_foundry.run_test_foundry(
        target_rel, llm_call=lambda **_k: {"test_code": ""}, run_suite=exploding_suite
    )

    # Did not propagate; reported failure with the exception message.
    assert result["ok"] is False
    assert "suite blew up" in result["reason"]
    # Teardown happened exactly once, on the worktree we created.
    assert removed["n"] == 1
    assert removed["calls"] == [fake_wt]
