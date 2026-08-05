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


def test_cli_classifies_exact_allowlisted_file_and_persists_it(
    tmp_path, monkeypatch, state_dir, capsys
):
    gate_log = tmp_path / "python.log"
    gate_log.write_text(
        "=== 1 file with test failures (1 test failed) ===\n"
        "  tests/env/test_clock.py  (1 test failed)\n",
        encoding="utf-8",
    )
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text('["tests/env/test_clock.py"]\n', encoding="utf-8")

    rc = _run_cli(
        [
            "vision",
            "classify-gate-failures",
            "--quarantine-allowlist",
            str(allowlist),
            "--gate-log",
            f"python={gate_log}",
            "--json",
        ],
        monkeypatch,
        tmp_path / "kanban.db",
    )
    assert rc == 0
    classified = json.loads(capsys.readouterr().out)
    assert classified["failure_classification"] == "environment_quarantine"

    rc = _run_cli(
        [
            "vision",
            "record-gate-result",
            "fail",
            "--failure-classification",
            classified["failure_classification"],
            "--failed-test-files-json",
            json.dumps(classified["failed_test_files"]),
            "--quarantined-test-files-json",
            json.dumps(classified["quarantined_test_files"]),
            "--blocking-test-files-json",
            json.dumps(classified["blocking_test_files"]),
            "--unattributed-fail-gates-json",
            json.dumps(classified["unattributed_fail_gates"]),
            "--quarantine-policy-sha256",
            classified["quarantine_policy_sha256"],
        ],
        monkeypatch,
        tmp_path / "kanban.db",
    )
    assert rc == 0
    rec = vm.read_gate_records()[-1]
    assert rec["failure_classification"] == "environment_quarantine"
    assert rec["quarantined_test_files"] == ["tests/env/test_clock.py"]


def test_cli_incomplete_file_attribution_and_missing_gate_both_block(
    tmp_path, monkeypatch, state_dir, capsys
):
    gate_log = tmp_path / "python.log"
    gate_log.write_text(
        "=== 2 files with test failures (2 tests failed) ===\n"
        "  tests/env/test_clock.py  (1 test failed)\n",
        encoding="utf-8",
    )
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text('["tests/env/test_clock.py"]\n', encoding="utf-8")

    rc = _run_cli(
        [
            "vision",
            "classify-gate-failures",
            "--quarantine-allowlist",
            str(allowlist),
            "--gate-log",
            f"python={gate_log}",
            "--unattributed-gate",
            "build",
            "--json",
        ],
        monkeypatch,
        tmp_path / "kanban.db",
    )

    assert rc == 0
    classified = json.loads(capsys.readouterr().out)
    assert classified["failure_classification"] == "regression"
    assert classified["failed_test_files"] == []
    assert classified["unattributed_fail_gates"] == ["build", "python"]


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


# ---------------------------------------------------------------------------
# deflake-check (GATE-FLAKY-RETRY-HONESTY-S1) — CLI wiring
# ---------------------------------------------------------------------------

def test_cli_deflake_check_idle_when_no_flaky(tmp_path, monkeypatch, state_dir, capsys):
    # a clean ledger (one green night) has no flaky file -> idle
    _run_cli(["vision", "record-gate-result", "pass"], monkeypatch, tmp_path / "kanban.db")
    capsys.readouterr()
    rc = _run_cli(
        ["vision", "deflake-check", "--dry-run", "--json"],
        monkeypatch, tmp_path / "kanban.db",
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["triggered"] is False
    assert out["filed"] == []


def test_cli_deflake_check_dry_run_lists_flaky_file(tmp_path, monkeypatch, state_dir, capsys):
    # seed a leaker-only (flaky) night via the record CLI, then detect it
    _run_cli(
        [
            "vision", "record-gate-result", "fail",
            "--leakers-json", json.dumps(["python: tests/agent/test_delegate.py"]),
            "--leaker-only",
        ],
        monkeypatch, tmp_path / "kanban.db",
    )
    capsys.readouterr()
    rc = _run_cli(
        ["vision", "deflake-check", "--dry-run", "--json"],
        monkeypatch, tmp_path / "kanban.db",
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["triggered"] is True
    assert len(out["candidates"]) == 1
    assert out["candidates"][0]["file"] == "tests/agent/test_delegate.py"
    assert out["filed"][0]["dry_run"] is True


# ---------------------------------------------------------------------------
# strategist rendering (run_* patched — no model, no board)
# ---------------------------------------------------------------------------

def _patch_strategist(monkeypatch, **fns):
    import hermes_cli.subcommands.vision as vision_mod

    for name, fn in fns.items():
        monkeypatch.setattr(vision_mod.strategist, name, fn)


def test_cli_strategist_mode_is_required(tmp_path, monkeypatch, state_dir):
    """'vision strategist' without --mode must be a usage error — silently
    falling through would run REFLECT when the operator meant PROPOSE."""
    from hermes_cli.subcommands.vision import build_vision_parser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_vision_parser(sub)
    with pytest.raises(SystemExit):
        parser.parse_args(["vision", "strategist"])


def test_cli_strategist_reflect_json_emits_raw_utf8(tmp_path, monkeypatch, state_dir, capsys):
    """--json must print the result as JSON verbatim (ensure_ascii=False):
    the cron parses this line, and escaped Umlauts would still parse but a
    missing JSON line (human rendering instead) breaks the pipeline."""
    result = {
        "mode": "reflect",
        "note": {"approved": 1, "vetoed": 0, "shipped": 2, "vetoed_levers": [],
                 "comment": "Überblick geschafft"},
    }
    _patch_strategist(monkeypatch, run_reflect=lambda args: result)
    rc = _run_cli(
        ["vision", "strategist", "--mode", "reflect", "--json"],
        monkeypatch, tmp_path / "kanban.db",
    )
    assert rc == 0
    raw = capsys.readouterr().out.strip()
    assert "Überblick geschafft" in raw
    assert json.loads(raw) == result


def test_cli_strategist_reflect_names_suppressed_levers_none(
    tmp_path, monkeypatch, state_dir, capsys
):
    """An empty vetoed_levers list must render as the literal word 'none' —
    an empty tail reads like a truncated line in the cron journal."""
    result = {
        "mode": "reflect",
        "note": {"approved": 3, "vetoed": 0, "shipped": 1, "vetoed_levers": []},
    }
    _patch_strategist(monkeypatch, run_reflect=lambda args: result)
    rc = _run_cli(
        ["vision", "strategist", "--mode", "reflect"],
        monkeypatch, tmp_path / "kanban.db",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "3 approved" in out
    assert "suppressed levers: none" in out


def test_cli_strategist_harvest_watch_triggered_without_harvest_block(
    tmp_path, monkeypatch, state_dir, capsys
):
    """A triggered watch result may carry no harvest payload — the summary
    must still render (0 candidates) instead of crashing the heartbeat."""
    result = {
        "mode": "harvest-watch",
        "triggered": True,
        "open_disposition_items": 3,
        "threshold": 2,
        "rearm": "2026-07-30",
    }
    _patch_strategist(monkeypatch, run_harvest_watch=lambda args: result)
    rc = _run_cli(
        ["vision", "strategist", "--mode", "harvest-watch"],
        monkeypatch, tmp_path / "kanban.db",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "harvest-watch: triggered at 3 open disposition item(s)" in out
    assert "0 candidate(s)" in out


def test_cli_gate_fix_check_triggered_without_ingested_block(
    tmp_path, monkeypatch, state_dir, capsys
):
    """A triggered gate-fix result may omit the ingested payload — the
    summary must still render (key None) instead of crashing the cron."""
    result = {"triggered": True, "gate": "python", "red_nights": 3}
    _patch_strategist(monkeypatch, run_gate_fix=lambda args: result)
    rc = _run_cli(
        ["vision", "gate-fix-check"],
        monkeypatch, tmp_path / "kanban.db",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "gate-fix-check: gate 'python' red 3 nights" in out
    assert "ingested HELD" in out


# ---------------------------------------------------------------------------
# Human-readable output is the default (never silent JSON-by-default)
# ---------------------------------------------------------------------------

def test_strategist_reflect_human_output_is_not_json(
    tmp_path, monkeypatch, state_dir, capsys
):
    """Without --json the reflect summary prints as a human line with raw
    Umlauts — a JSON-by-default flip would break the cron journal grep."""
    from hermes_cli import strategist as _strategist

    monkeypatch.setattr(
        _strategist,
        "run_reflect",
        lambda args: {
            "mode": "reflect",
            "note": {
                "approved": 1,
                "vetoed": 0,
                "shipped": 2,
                "vetoed_levers": ["größe"],
            },
        },
    )
    rc = _run_cli(
        ["vision", "strategist", "--mode", "reflect"],
        monkeypatch,
        tmp_path / "kanban.db",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("strategist reflect: 1 approved")
    assert "größe" in out
    assert not out.strip().startswith("{")


def test_gate_fix_check_idle_human_output_is_not_json(
    tmp_path, monkeypatch, state_dir, capsys
):
    """The idle line of gate-fix-check is human text with raw Umlauts,
    not JSON."""
    from hermes_cli import strategist as _strategist

    monkeypatch.setattr(
        _strategist,
        "run_gate_fix",
        lambda args: {"triggered": False, "reason": "nichts rot gewesen"},
    )
    rc = _run_cli(["vision", "gate-fix-check"], monkeypatch, tmp_path / "kanban.db")
    assert rc == 0
    assert "gate-fix-check: idle — nichts rot gewesen" in capsys.readouterr().out


def test_triage_check_triggered_without_ingested_block_still_renders(
    tmp_path, monkeypatch, state_dir, capsys
):
    """A triggered triage result without an 'ingested' payload must still
    render (key None) instead of crashing on None.get()."""
    from hermes_cli import strategist as _strategist

    monkeypatch.setattr(
        _strategist,
        "run_persistent_red_triage",
        lambda args: {
            "triggered": True,
            "gate": "python",
            "red_count": 3,
            "window": 5,
            "key": "k",
            "fingerprint": "f",
        },
    )
    rc = _run_cli(["vision", "triage-check"], monkeypatch, tmp_path / "kanban.db")
    assert rc == 0
    out = capsys.readouterr().out
    assert "triage-check: gate 'python' red 3 of 5 nights" in out
    assert "ingested HELD" in out


def test_deflake_check_triggered_with_empty_filed_still_renders(
    tmp_path, monkeypatch, state_dir, capsys
):
    """A triggered deflake result without 'filed' degrades to zero opened
    tasks — it must not crash iterating None."""
    from hermes_cli import strategist as _strategist

    monkeypatch.setattr(
        _strategist,
        "run_deflake_check",
        lambda args: {
            "triggered": True,
            "candidates": [{"file": "tests/x.py"}],
            "recurring": [],
        },
    )
    rc = _run_cli(["vision", "deflake-check"], monkeypatch, tmp_path / "kanban.db")
    assert rc == 0
    out = capsys.readouterr().out
    assert "deflake-check: 1 flaky file(s)" in out
    assert "0 de-flake task(s) opened" in out
