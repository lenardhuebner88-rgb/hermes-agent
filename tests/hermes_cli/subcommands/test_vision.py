"""Tests for the ``hermes vision`` CLI wiring (hermes_cli.subcommands.vision).

Focused on the GREEN-GATE-LEAKER-CAUSE-PURITY-S1 surface: ``isolate-fails``
(parse logs -> bounded isolation rerun via a monkeypatched runner -> cleaned
first_fail + leaker list as JSON) and the ``record-gate-result`` leaker flags
that persist the demoted list / suppress a leaker-only cause.
"""

from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import gate_leaker
from hermes_cli import vision_metrics as vm


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("HERMES_VISION_STATE_DIR", str(d))
    return d


def _run_cli(argv, monkeypatch, db_path):
    from hermes_cli.subcommands.vision import build_vision_parser

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_vision_parser(sub)
    args = parser.parse_args(argv)
    return args.func(args)


# ---------------------------------------------------------------------------
# record-gate-result leaker flags
# ---------------------------------------------------------------------------

def test_cli_record_fail_with_leakers(tmp_path, monkeypatch, state_dir):
    rc = _run_cli(
        [
            "vision", "record-gate-result", "fail",
            "--first-fail-gate", "python",
            "--first-fail-detail", "python (isolated): tests/b.py\nFAILED",
            "--leakers-json", json.dumps(["python: tests/a.py"]),
        ],
        monkeypatch, tmp_path / "kanban.db",
    )
    assert rc == 0
    rec = vm.read_gate_records()[-1]
    assert rec["result"] == "fail"
    assert rec["first_fail"]["gate"] == "python"
    assert rec["leakers"] == ["python: tests/a.py"]


def test_cli_record_fail_leaker_only_suppresses_cause(tmp_path, monkeypatch, state_dir):
    rc = _run_cli(
        [
            "vision", "record-gate-result", "fail",
            "--first-fail-gate", "python",
            "--first-fail-detail", "ignored",
            "--leakers-json", json.dumps(["python: tests/a.py"]),
            "--leaker-only",
        ],
        monkeypatch, tmp_path / "kanban.db",
    )
    assert rc == 0
    rec = vm.read_gate_records()[-1]
    assert rec["result"] == "fail"  # still red
    assert "first_fail" not in rec  # but no product cause
    assert rec["leaker_only"] is True
    assert rec["leakers"] == ["python: tests/a.py"]


def test_cli_record_bad_leakers_json_is_ignored(tmp_path, monkeypatch, state_dir):
    rc = _run_cli(
        [
            "vision", "record-gate-result", "fail",
            "--first-fail-gate", "python", "--first-fail-detail", "boom",
            "--leakers-json", "{not json",
        ],
        monkeypatch, tmp_path / "kanban.db",
    )
    assert rc == 0
    rec = vm.read_gate_records()[-1]
    assert "leakers" not in rec
    assert rec["first_fail"]["gate"] == "python"


# ---------------------------------------------------------------------------
# isolate-fails (runner monkeypatched so no subprocess runs)
# ---------------------------------------------------------------------------

def _fake_runner_factory(passing):
    def factory(gate):
        def run_one(f):
            if f in passing:
                return (True, f"isolated {f}: passed")
            return (False, f"isolated {f}: FAILED")
        return run_one
    return factory


def test_cli_isolate_fails_demotes_leaker_and_cleans_cause(
    tmp_path, monkeypatch, state_dir, capsys
):
    py_log = tmp_path / "python.log"
    py_log.write_text(
        "=== 2 files with test failures (2 tests failed) ===\n"
        "  tests/leaky.py  (1 test failed)\n"
        "  tests/real.py  (1 test failed)\n"
    )
    # tests/leaky.py passes alone -> leaker; tests/real.py reproduces
    monkeypatch.setattr(
        gate_leaker, "build_runner",
        lambda gate, repo, **kw: _fake_runner_factory({"tests/leaky.py"})(gate),
    )
    rc = _run_cli(
        [
            "vision", "isolate-fails",
            "--repo", str(tmp_path),
            "--gate-log", f"python={py_log}",
            "--json",
        ],
        monkeypatch, tmp_path / "kanban.db",
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["first_fail_gate"] == "python"
    assert "tests/real.py" in out["first_fail_detail"]
    assert out["leakers"] == ["python: tests/leaky.py"]
    assert out["leaker_only"] is False
    assert out["reproduced_total"] == 1
    assert out["leaker_total"] == 1


def test_cli_isolate_fails_all_leakers_is_leaker_only(
    tmp_path, monkeypatch, state_dir, capsys
):
    py_log = tmp_path / "python.log"
    py_log.write_text(
        "=== 1 file with test failures (1 tests failed) ===\n"
        "  tests/leaky.py  (1 test failed)\n"
    )
    monkeypatch.setattr(
        gate_leaker, "build_runner",
        lambda gate, repo, **kw: _fake_runner_factory({"tests/leaky.py"})(gate),
    )
    rc = _run_cli(
        [
            "vision", "isolate-fails",
            "--repo", str(tmp_path),
            "--gate-log", f"python={py_log}",
            "--json",
        ],
        monkeypatch, tmp_path / "kanban.db",
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["leaker_only"] is True
    assert out["first_fail_gate"] is None
    assert out["first_fail_detail"] == ""
    assert out["leakers"] == ["python: tests/leaky.py"]


def test_cli_isolate_fails_missing_log_is_safe(
    tmp_path, monkeypatch, state_dir, capsys
):
    # a non-existent log must not crash the heartbeat — empty result, fall back.
    rc = _run_cli(
        [
            "vision", "isolate-fails",
            "--repo", str(tmp_path),
            "--gate-log", f"python={tmp_path}/does-not-exist.log",
            "--json",
        ],
        monkeypatch, tmp_path / "kanban.db",
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    # empty/unparseable python log -> no files parsed -> deterministic, not demoted
    assert out["leaker_only"] is False
