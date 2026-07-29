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


# ---------------------------------------------------------------------------
# HTTP layer — URL validation, 2xx window, login contract
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, body: str, status: int):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakeOpener:
    def __init__(self, body: str, status: int = 200):
        self._body = body
        self._status = status

    def open(self, request, timeout=None):
        return _FakeResponse(self._body, self._status)


def test_base_url_rejects_scheme_without_netloc():
    """A scheme alone is not a dashboard URL — 'http://' must be rejected
    or every later request would go nowhere with a confusing error."""
    mod = _load_module()
    try:
        mod._base_url("http://")
    except mod.CollectorError:
        pass
    else:
        raise AssertionError("scheme-only URL must be rejected")
    assert mod._base_url("https://h:9119/") == "https://h:9119"


def test_json_request_enforces_2xx_status_window():
    """Only 200..299 may pass; 199 and 300 (the window edges) must raise,
    and the inclusive boundaries (200, 299) must succeed."""
    mod = _load_module()
    for status, ok in ((199, False), (200, True), (299, True), (300, False)):
        opener = _FakeOpener('{"ok": true}', status=status)
        if ok:
            assert mod._json_request(opener, "GET", "https://h/x", timeout=1) == {"ok": True}
        else:
            try:
                mod._json_request(opener, "GET", "https://h/x", timeout=1)
            except mod.CollectorError:
                pass
            else:
                raise AssertionError(f"status {status} must be rejected")


def test_authenticate_requires_literal_ok_true(monkeypatch):
    """The login response must carry ok=true literally — any other shape
    (ok=false, missing) is a failed login and must raise."""
    mod = _load_module()
    kwargs = {"provider": "local", "username": "u", "password": "p", "timeout": 1}

    monkeypatch.setattr(
        mod, "_json_request",
        lambda opener, method, url, *, payload=None, timeout: {"ok": True},
    )
    assert mod._authenticate("https://h", **kwargs) is not None

    monkeypatch.setattr(
        mod, "_json_request",
        lambda opener, method, url, *, payload=None, timeout: {"ok": False},
    )
    try:
        mod._authenticate("https://h", **kwargs)
    except mod.CollectorError:
        pass
    else:
        raise AssertionError("login without ok=true must be rejected")


# ---------------------------------------------------------------------------
# JSON-block boundaries, receipt placeholders, request payload
# ---------------------------------------------------------------------------

def test_json_request_serializes_payload_with_content_type():
    """A payload is sent as JSON bytes WITH the Content-Type header —
    a silent drop would POST form-less garbage to the dashboard."""
    mod = _load_module()

    class _Resp:
        status = 200

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Opener:
        last = None

        def open(self, request, timeout=None):
            _Opener.last = request
            return _Resp()

    out = mod._json_request(_Opener(), "POST", "https://h/login", payload={"a": 1}, timeout=1)
    assert out == {"ok": True}
    assert _Opener.last.data == b'{"a": 1}'
    assert _Opener.last.get_header("Content-type") == "application/json"


def test_json_block_passthrough_boundary_is_inclusive_and_raw_utf8():
    """Serialization at exactly max_chars passes through unchanged (the
    boundary is inclusive) and keeps Umlauts raw."""
    mod = _load_module()
    exact = json.dumps("x", indent=2, ensure_ascii=False)
    assert mod._json_block("x", max_chars=len(exact)) == exact
    assert "ü" in mod._json_block({"t": "ü"}, max_chars=1000)


def test_json_block_list_truncation_keeps_boundary_item_raw():
    """A prefix serialization at exactly max_chars still KEEPS that item;
    the truncated wrapper stays raw UTF-8 and parseable."""
    mod = _load_module()
    umlaut = "ü" * 10
    items = [umlaut, "b"]
    cap = len(json.dumps([umlaut], indent=2, ensure_ascii=False))

    block = mod._json_block(items, max_chars=cap)

    parsed = json.loads(block)
    assert parsed["items"] == [umlaut]
    assert umlaut in block  # raw, not \u-escaped


def test_write_receipt_renders_none_placeholders(tmp_path):
    """Empty task list and empty notes render the literal '(none)'
    placeholders — an empty string reads like a broken template."""
    mod = _load_module()
    receipt = tmp_path / "receipt.md"
    mod.write_receipt(
        str(receipt),
        scenario="S-test",
        task_ids=[],
        workers_snapshots=[],
        peak_concurrent=0,
        task_activities={},
        git_log=["abc fix"],
        repo="/repo",
        started_at="t0",
        finished_at="t1",
        notes="",
        branch="main",
    )
    text = receipt.read_text(encoding="utf-8")
    assert "**Task IDs:** (none)" in text
    assert "\n(none)\n" in text  # the Notes block placeholder
