"""P2 run-history + P3 nightly rotation."""
from __future__ import annotations

import datetime as _dt
import importlib.util
from pathlib import Path

import pytest

from hermes_cli import autoresearch_runs

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_live_state(tmp_path, monkeypatch):
    """Keep EVERY test off the live proposal store / kanban DB / strategist state.
    ``_proposals_dir()`` resolves CWD-based, so HERMES_HOME alone does NOT isolate
    the backlog — a test running the real reconciler (``main()`` with
    ``_run_reconciler`` unmocked, e.g. test_nightly_main_routes_by_lane) would
    mutate the live 77-proposal store. Regression fence for the 2026-06-22 incident."""
    iso = tmp_path / "_iso"
    (iso / "skill-audit").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(iso / ".hermes"))
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(iso / "skill-audit"))
    monkeypatch.setenv("HERMES_AUTORESEARCH_DIGEST_PATH", str(iso / "digest.json"))
    monkeypatch.setenv("HERMES_AUTORESEARCH_RECONCILE_SUMMARY_PATH", str(iso / "last-reconcile.json"))
    monkeypatch.setenv("HERMES_STRATEGIST_VETOED_PATH", str(iso / "vetoed_levers.json"))


@pytest.fixture()
def audit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(tmp_path))
    return tmp_path


def test_append_and_read_newest_first(audit):
    autoresearch_runs.append_run(lane="skill", request_id="r1", tokens=100, proposed=2, errors=0, scanned=3)
    autoresearch_runs.append_run(
        lane="code", request_id="r2", tokens=50, proposed=1, errors=1,
        scanned=4, vetoed=2, model="test-model",
    )
    runs = autoresearch_runs.read_runs()
    assert [r["request_id"] for r in runs] == ["r2", "r1"]  # newest first
    assert runs[0]["lane"] == "code" and runs[0]["tokens"] == 50 and runs[0]["errors"] == 1
    assert runs[0]["vetoed"] == 2 and runs[0]["model"] == "test-model"


def test_history_capped_to_30(audit):
    for i in range(35):
        autoresearch_runs.append_run(lane="skill", request_id=f"r{i}", tokens=i)
    runs = autoresearch_runs.read_runs(100)
    assert len(runs) == 30
    assert runs[0]["request_id"] == "r34"  # newest kept
    assert runs[-1]["request_id"] == "r5"  # oldest within the cap


def test_read_tolerates_garbage(audit):
    (audit / "autoresearch-runs.json").write_text("{ not json", encoding="utf-8")
    assert autoresearch_runs.read_runs() == []


def test_invalid_lane_defaults_to_skill(audit):
    autoresearch_runs.append_run(lane="bogus", tokens=1)
    assert autoresearch_runs.read_runs()[0]["lane"] == "skill"


def test_append_is_atomic_no_temp_leftover(audit):
    """append_run writes via tempfile + os.replace → no half-written temp lingers
    and the on-disk file is always complete, valid JSON."""
    import json as _json
    for i in range(5):
        autoresearch_runs.append_run(lane="code", request_id=f"c{i}", tokens=i)
    assert list(audit.glob("autoresearch-runs.json.tmp.*")) == []  # no torn temp left behind
    data = _json.loads((audit / "autoresearch-runs.json").read_text(encoding="utf-8"))
    assert len(data["runs"]) == 5 and data["runs"][0]["request_id"] == "c4"


def _load_nightly():
    spec = importlib.util.spec_from_file_location(
        "autoresearch_nightly_under_test", _ROOT / "scripts" / "autoresearch_nightly.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_nightly_lane_parity(monkeypatch):
    mod = _load_nightly()

    class _Stub:
        fixed = None

        @classmethod
        def now(cls, tz=None):
            return cls.fixed

    _Stub.fixed = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)  # yday 1 (odd → code)
    monkeypatch.setattr(mod, "datetime", _Stub)
    assert mod._is_code_night() is True
    _Stub.fixed = _dt.datetime(2026, 1, 2, tzinfo=_dt.timezone.utc)  # yday 2 (even → skill)
    assert mod._is_code_night() is False


def test_nightly_code_night_uses_deep_caps(monkeypatch):
    """The unattended code lane raises max_files/limit beyond the interactive default."""
    mod = _load_nightly()
    captured = {}
    import hermes_cli.autoresearch_proposals as arp
    monkeypatch.setattr(arp, "generate_code_weakness_proposals",
                        lambda **k: captured.update(k) or {"created_count": 0, "files_seen": 0})
    assert mod._run_code_night() == 0
    assert captured["max_files"] == 40 and captured["limit"] == 8


def test_nightly_main_routes_by_lane(monkeypatch):
    mod = _load_nightly()
    called = {}
    monkeypatch.setattr(mod, "_run_code_night", lambda: (called.__setitem__("lane", "code"), 0)[1])
    monkeypatch.setattr(mod, "_run_skill_night", lambda: (called.__setitem__("lane", "skill"), 0)[1])
    monkeypatch.setattr(mod, "_is_code_night", lambda: True)
    assert mod.main() == 0 and called["lane"] == "code"
    monkeypatch.setattr(mod, "_is_code_night", lambda: False)
    assert mod.main() == 0 and called["lane"] == "skill"


def test_nightly_main_runs_reconciler_after_lane(monkeypatch):
    mod = _load_nightly()
    order = []
    monkeypatch.setattr(mod, "_is_code_night", lambda: True)
    monkeypatch.setattr(mod, "_run_code_night", lambda: order.append("code") or 0)
    monkeypatch.setattr(mod, "_run_reconciler", lambda: order.append("reconcile") or {"ok": True}, raising=False)

    assert mod.main() == 0
    assert order == ["code", "reconcile"]
