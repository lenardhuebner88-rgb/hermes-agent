"""Unit tests for scripts/dogfood_repo_cap_evidence.py — collector edge-case hardening.

Three latent bugs were documented (t_670fd12a) but not fixed; this suite pins the
hardened behaviour so the dogfood evidence collector cannot regress:

  Bug 1 — crash on ``--receipt-dir ''``: ``os.makedirs('')`` raised FileNotFoundError
          in both the dry-run template writer and the real receipt writer.
  Bug 2 — peak-Zaehlfehler: ``max_concurrent_workers`` silently returned 0 when a
          snapshot omitted the (redundant) ``count`` key, ignoring the live ``workers``
          list, so the headline "peak concurrent" metric undercounted.
  Bug 3 — truncated JSON block: the receipt embedded ``json.dumps(...)[:4000]`` which
          sliced mid-token, producing invalid JSON inside the ```json fence.

Plus the operator directive (disposition di_4c5633dd):
  Bug 4 — non-'main' default branch: ``collect_git_log`` hardcoded ``main``; a --repo
          sandbox whose default branch is ``trunk``/``master`` produced no git evidence.
          The collector must support an explicit ``branch`` and fall back to ``HEAD``.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "dogfood_repo_cap_evidence",
        REPO_ROOT / "scripts" / "dogfood_repo_cap_evidence.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _seed_repo(tmp_path: Path, branch: str) -> Path:
    """Init a git repo whose default branch is ``branch`` (renamed, so 'main' is absent)."""
    repo = tmp_path / "sandbox"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed commit")
    _git(repo, "branch", "-m", branch)  # rename current branch -> no 'main'/'master'
    return repo


# ---------------------------------------------------------------------------
# Bug 2 — peak concurrent count
# ---------------------------------------------------------------------------

def test_max_concurrent_counts_workers_when_count_field_absent():
    """Peak must reflect the live workers list even if the redundant count key is gone."""
    mod = _load_module()
    snapshots = [
        {"workers": [{"run_id": 1}, {"run_id": 2}]},          # 2 concurrent, no `count`
        {"workers": [{"run_id": 1}], "count": 1},
    ]
    assert mod.max_concurrent_workers(snapshots) == 2


def test_max_concurrent_uses_larger_of_count_and_workers():
    """Defensive: if count and the workers list disagree, take the larger (no undercount)."""
    mod = _load_module()
    snapshots = [{"workers": [{"run_id": 1}, {"run_id": 2}, {"run_id": 3}], "count": 0}]
    assert mod.max_concurrent_workers(snapshots) == 3


def test_max_concurrent_normal_count_still_works():
    """Regression guard: the happy path (count == len(workers)) is unchanged."""
    mod = _load_module()
    snapshots = [
        {"workers": [{"run_id": 1}], "count": 1},
        {"workers": [{"run_id": 1}, {"run_id": 2}], "count": 2},
    ]
    assert mod.max_concurrent_workers(snapshots) == 2


# ---------------------------------------------------------------------------
# Bug 3 — truncated JSON block
# ---------------------------------------------------------------------------

def test_json_block_stays_valid_when_oversized():
    """An oversized payload must still serialize to PARSEABLE JSON (not a sliced token)."""
    mod = _load_module()
    big = [{"workers": [{"run_id": i, "blob": "x" * 200}], "count": 1} for i in range(50)]
    block = mod._json_block(big, max_chars=500)
    parsed = json.loads(block)  # must not raise
    assert parsed


def test_json_block_passthrough_when_small():
    """Small payloads round-trip unchanged."""
    mod = _load_module()
    obj = [{"count": 2, "workers": []}]
    assert json.loads(mod._json_block(obj, max_chars=4000)) == obj


def test_receipt_json_fence_is_valid_even_with_large_snapshots(tmp_path):
    """End-to-end: the receipt's ```json fence must parse even with bulky snapshots."""
    mod = _load_module()
    big_snaps = [
        {"workers": [{"run_id": i, "junk": "y" * 500}], "count": 1, "_captured_at": "t"}
        for i in range(40)
    ]
    path = tmp_path / "r.md"
    mod.write_receipt(
        str(path),
        scenario="S1",
        task_ids=["t_x"],
        workers_snapshots=big_snaps,
        peak_concurrent=1,
        task_activities={},
        git_log=["abc init"],
        repo="/tmp/x",
        started_at="a",
        finished_at="b",
        notes="",
    )
    text = path.read_text(encoding="utf-8")
    block = text.split("```json", 1)[1].split("```", 1)[0]
    json.loads(block)  # must not raise — i.e. no mid-token truncation


# ---------------------------------------------------------------------------
# Bug 1 — crash on empty --receipt-dir
# ---------------------------------------------------------------------------

def test_dry_run_template_handles_empty_receipt_dir(tmp_path, monkeypatch):
    """``--receipt-dir ''`` (empty) must write to CWD, not crash on os.makedirs('')."""
    mod = _load_module()
    monkeypatch.chdir(tmp_path)
    path = mod.write_dry_run_template("")
    assert os.path.exists(path)


def test_write_receipt_handles_dirless_path(tmp_path, monkeypatch):
    """A bare-filename receipt path (no directory component) must not crash."""
    mod = _load_module()
    monkeypatch.chdir(tmp_path)
    mod.write_receipt(
        "bare-receipt.md",
        scenario="S1",
        task_ids=[],
        workers_snapshots=[],
        peak_concurrent=0,
        task_activities={},
        git_log=[],
        repo="/tmp",
        started_at="a",
        finished_at="b",
        notes="",
    )
    assert (tmp_path / "bare-receipt.md").exists()


def test_main_dry_run_with_empty_receipt_dir(tmp_path, monkeypatch):
    """AC-1: ``--dry-run --receipt-dir ''`` exits 0 cleanly (no crash)."""
    mod = _load_module()
    monkeypatch.chdir(tmp_path)
    rc = mod.main(["--dry-run", "--receipt-dir", ""])
    assert rc == 0


# ---------------------------------------------------------------------------
# Bug 4 — non-'main' default branch (operator directive di_4c5633dd)
# ---------------------------------------------------------------------------

def test_collect_git_log_falls_back_to_head_on_non_main_branch(tmp_path):
    """A sandbox whose default branch != 'main' must still yield commits via HEAD fallback."""
    mod = _load_module()
    repo = _seed_repo(tmp_path, branch="trunk")
    lines = mod.collect_git_log(str(repo), branch="main")
    assert lines, "expected at least one commit line"
    assert not lines[0].startswith("[git"), f"expected commits, got error sentinel: {lines}"
    assert any("seed commit" in line for line in lines)


def test_collect_git_log_supports_explicit_branch(tmp_path):
    """An explicit --branch reads that branch directly."""
    mod = _load_module()
    repo = _seed_repo(tmp_path, branch="trunk")
    lines = mod.collect_git_log(str(repo), branch="trunk")
    assert any("seed commit" in line for line in lines)


def test_collect_git_log_default_branch_main(tmp_path):
    """Regression guard: when the repo really has 'main', it is read directly."""
    mod = _load_module()
    repo = _seed_repo(tmp_path, branch="main")
    lines = mod.collect_git_log(str(repo))
    assert any("seed commit" in line for line in lines)
