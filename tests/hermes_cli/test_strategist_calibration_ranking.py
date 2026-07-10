"""Tests for STRATEGIST-CALIBRATION-S1 (outcome-calibration learning loop) and
STRATEGIST-RANKING-S1 (CD3/WSJF-lite rank_score sort) + the hardened
STRATEGIST-GROUNDING-HARDEN-S1 gate.

Same fixture pattern as test_strategist_lever_outcomes.py / harness.py: no real
LLM calls, real-shaped outcomes records (schema/field names lifted verbatim
from the live ``~/.hermes/state/strategist/lever-outcomes.json`` shape) rather
than a synthetic ad-hoc dict.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import strategist


@pytest.fixture
def board_home(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_VISION_METRICS_PATH", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return tmp_path


# --------------------------------------------------------------------------- #
# Real-format outcomes fixture — field names/shape lifted verbatim from the
# live lever-outcomes.json (schema_version, lever_key, root_task_id,
# proposed_at, baseline, metric_key, shipped_at, measured_at, current, delta,
# verdict, status), baseline trimmed to the relevant flat keys.
# --------------------------------------------------------------------------- #
def _measured_record(lever_key, verdict, *, root_task_id, metric_key="green_gate_streak.streak"):
    return {
        "schema_version": 1,
        "lever_key": lever_key,
        "root_task_id": root_task_id,
        "proposed_at": 1783049574,
        "baseline": {"green_gate_streak.streak": 0.0, "green_gate_streak.counter.value": 10.0},
        "metric_key": metric_key,
        "measurability": "ok",
        "shipped_at": 1783069506,
        "measured_at": 1783360838,
        "current": 2.0,
        "delta": 2.0 if verdict == "improved" else (-2.0 if verdict == "worsened" else 0.0),
        "verdict": verdict,
        "status": "measured",
    }


# --------------------------------------------------------------------------- #
# 1. compute_lever_calibration — honesty gate (min n) + clamping
# --------------------------------------------------------------------------- #
def test_calibration_skips_class_below_min_n():
    records = [
        _measured_record("GATE-FIX-PY-aaaaaaaa", "improved", root_task_id="t_1"),
        _measured_record("GATE-FIX-PY-bbbbbbbb", "improved", root_task_id="t_2"),
    ]
    calib = strategist.compute_lever_calibration(records)
    assert calib == {}


def test_calibration_emits_factor_at_min_n():
    records = [
        _measured_record("GATE-FIX-PY-aaaaaaaa", "improved", root_task_id="t_1"),
        _measured_record("GATE-FIX-PY-bbbbbbbb", "improved", root_task_id="t_2"),
        _measured_record("GATE-FIX-PY-cccccccc", "neutral", root_task_id="t_3"),
    ]
    calib = strategist.compute_lever_calibration(records)
    # class = stable prefix with the trailing 8-hex digest stripped
    assert "GATE-FIX-PY" in calib
    entry = calib["GATE-FIX-PY"]
    assert entry["n"] == 3
    # mean score = (1 + 1 + 0) / 3 = 0.667 -> factor = 1 + 0.667*0.5 = 1.333
    assert entry["factor"] == pytest.approx(1.3333, abs=1e-3)
    assert "updated_at" in entry


def test_calibration_clamps_to_bounds():
    records = [
        _measured_record(f"GATE-FIX-PY-{'a' * 7}{i}", "improved", root_task_id=f"t_{i}")
        for i in range(5)
    ]
    calib = strategist.compute_lever_calibration(records)
    assert calib["GATE-FIX-PY"]["factor"] == pytest.approx(1.5)

    losers = [
        _measured_record(f"GATE-FIX-PY-{'b' * 7}{i}", "worsened", root_task_id=f"u_{i}")
        for i in range(5)
    ]
    calib_low = strategist.compute_lever_calibration(losers)
    assert calib_low["GATE-FIX-PY"]["factor"] == pytest.approx(0.5)


def test_calibration_ignores_unmeasurable_and_confounded():
    records = [
        _measured_record("GATE-FIX-PY-aaaaaaaa", "unmeasurable", root_task_id="t_1"),
        _measured_record("GATE-FIX-PY-bbbbbbbb", "confounded", root_task_id="t_2"),
        _measured_record("GATE-FIX-PY-cccccccc", "improved", root_task_id="t_3"),
    ]
    # only 1 directional verdict -> below min-n even though 3 records exist
    calib = strategist.compute_lever_calibration(records)
    assert calib == {}


def test_static_key_has_no_hash_suffix_stripped():
    """Static keys (no trailing 8-hex digest) are their own class untouched."""
    records = [
        _measured_record("HEILER-TRANSIENT", "improved", root_task_id="t_1"),
        _measured_record("HEILER-TRANSIENT", "improved", root_task_id="t_2"),
        _measured_record("HEILER-TRANSIENT", "improved", root_task_id="t_3"),
    ]
    calib = strategist.compute_lever_calibration(records)
    assert calib["HEILER-TRANSIENT"]["factor"] == pytest.approx(1.5)


# --------------------------------------------------------------------------- #
# 2. reflect() persists calibration to a sibling ledger file (real-format,
#    end-to-end through the actual write path used in production).
# --------------------------------------------------------------------------- #
def test_reflect_writes_calibration_ledger_from_real_shaped_records(board_home, tmp_path):
    outcomes_path = tmp_path / "lever-outcomes.json"
    notes_path = tmp_path / "reflections.jsonl"
    records = [
        _measured_record("GATE-FIX-PY-11111111", "improved", root_task_id="t_1"),
        _measured_record("GATE-FIX-PY-22222222", "improved", root_task_id="t_2"),
        _measured_record("GATE-FIX-PY-33333333", "neutral", root_task_id="t_3"),
    ]
    outcomes_path.write_text(json.dumps(records), encoding="utf-8")

    with kb.connect() as conn:
        strategist.reflect(conn, since=0, notes_path=notes_path, outcomes_path=outcomes_path)

    calib_path = strategist.default_lever_calibration_path(outcomes_path)
    assert calib_path.exists()
    data = json.loads(calib_path.read_text(encoding="utf-8"))
    assert data["GATE-FIX-PY"]["n"] == 3
    assert data["GATE-FIX-PY"]["factor"] == pytest.approx(1.3333, abs=1e-3)


# --------------------------------------------------------------------------- #
# 3. derive_levers() consumes calibration
# --------------------------------------------------------------------------- #
def test_derive_levers_applies_calibration_factor_to_gain_weight():
    context = {
        "metrics": None,
        "ledger": {"by_class": {kb.HEILER_CLASS_TRANSIENT: 3}, "total": 3, "entries": []},
        "suppressed": set(),
        "lever_calibration": {"HEILER-TRANSIENT": {"factor": 1.2, "n": 4, "updated_at": "x"}},
    }
    levers = strategist.derive_levers(context)
    assert len(levers) == 1
    lv = levers[0]
    # baseline gain_weight for HEILER-TRANSIENT template is 1.0 -> 1.2 after calibration
    assert lv.gain_weight == pytest.approx(1.2)
    assert lv.calibration == "x1.20 (n=4)"
    assert "kalibriert" in lv.rationale


def test_derive_levers_unchanged_when_no_calibration_entry():
    """No calibration for the lever's class -> identical to today's behaviour."""
    context = {
        "metrics": None,
        "ledger": {"by_class": {kb.HEILER_CLASS_TRANSIENT: 3}, "total": 3, "entries": []},
        "suppressed": set(),
        "lever_calibration": {"SOME-OTHER-CLASS": {"factor": 1.4, "n": 5, "updated_at": "x"}},
    }
    levers = strategist.derive_levers(context)
    assert levers[0].gain_weight == pytest.approx(1.0)
    assert levers[0].calibration is None


# --------------------------------------------------------------------------- #
# 4. rank_score sort order (STRATEGIST-RANKING-S1)
# --------------------------------------------------------------------------- #
def test_derive_levers_sorted_by_rank_score_desc():
    context = {
        "metrics": None,
        "ledger": {
            "by_class": {
                kb.HEILER_CLASS_TRANSIENT: 1,   # gain 1.0, cost 0.5 -> roi 0.5, rank 1.0
                kb.HEILER_CLASS_REAL_BUG: 1,     # gain 1.2, cost 0.5 -> roi 0.7, rank 1.4
            },
            "total": 2,
            "entries": [],
        },
        "suppressed": set(),
    }
    levers = strategist.derive_levers(context)
    assert [lv.key for lv in levers] == ["HEILER-REALBUG", "HEILER-TRANSIENT"]
    assert levers[0].rank_score > levers[1].rank_score


def test_rank_score_property_floors_cost():
    lever = strategist.Lever(
        key="X", title="t", lane="coder", target_metric="m", roi="hi",
        counter_metric="c", rationale="r", gain_weight=1.0, cost=0.05, counter_risk=0.1,
    )
    # cost floored at 0.25 -> rank_score = roi_score / 0.25, not / 0.05
    assert lever.rank_score == pytest.approx(lever.roi_score / 0.25)


# --------------------------------------------------------------------------- #
# 5. grounding_gate hardening (STRATEGIST-GROUNDING-HARDEN-S1)
# --------------------------------------------------------------------------- #
def _lever_with_grounding(grounding):
    return strategist.Lever(
        key="G1", title="t", lane="coder", target_metric="m", roi="hi",
        counter_metric="c", rationale="r", gain_weight=1.0, cost=0.3, counter_risk=0.1,
        grounding=grounding,
    )


def test_grounding_gate_rejects_unverifiable_prose():
    lever = _lever_with_grounding("Ich bin mir sicher, das existiert schon irgendwie.")
    result = strategist.grounding_gate(lever)
    assert result.passed is False
    assert "verifizierbar" in result.reason or "kein" in result.reason


def test_grounding_gate_accepts_existing_repo_path():
    lever = _lever_with_grounding("siehe hermes_cli/strategist.py:265 fuer die Implementierung")
    result = strategist.grounding_gate(lever)
    assert result.passed is True


def test_grounding_gate_accepts_known_metric_token():
    lever = _lever_with_grounding(f"Ledger zeigt {kb.HEILER_CLASS_TRANSIENT} Eskalationen")
    result = strategist.grounding_gate(lever)
    assert result.passed is True


def test_grounding_gate_still_rejects_empty():
    lever = _lever_with_grounding("")
    result = strategist.grounding_gate(lever)
    assert result.passed is False


# --------------------------------------------------------------------------- #
# 6. LOOP-HEALTH-S1 — strategist reasons over loop-pack ledger stats
# --------------------------------------------------------------------------- #
def _loop_stats(fails_by_kind, verified=0, rounds=None):
    return {
        "rounds": rounds if rounds is not None else verified,
        "verified": verified,
        "fails_by_kind": fails_by_kind,
        "bounced": 0,
        "avg_build_secs": None,
        "avg_verify_secs": None,
        "last_ts": None,
    }


def test_derive_levers_emits_loop_health_for_unhealthy_pack():
    context = {
        "metrics": None,
        "ledger": {"by_class": {}, "total": 0, "entries": []},
        "suppressed": set(),
        "loop_stats": {
            "docs-pack": _loop_stats({"build_fail": 3}, verified=1),
        },
    }
    levers = strategist.derive_levers(context)
    keys = [lv.key for lv in levers]
    assert "LOOP-HEALTH-DOCS-PACK" in keys
    lever = next(lv for lv in levers if lv.key == "LOOP-HEALTH-DOCS-PACK")
    gate = strategist.self_gate(lever)
    assert gate.passed is True


def test_derive_levers_no_lever_for_healthy_pack():
    context = {
        "metrics": None,
        "ledger": {"by_class": {}, "total": 0, "entries": []},
        "suppressed": set(),
        "loop_stats": {
            "docs-pack": _loop_stats({"build_fail": 1}, verified=10),
        },
    }
    levers = strategist.derive_levers(context)
    assert not [lv for lv in levers if lv.key.startswith("LOOP-HEALTH-")]


def test_derive_levers_no_lever_for_usage_limit_only_fails():
    context = {
        "metrics": None,
        "ledger": {"by_class": {}, "total": 0, "entries": []},
        "suppressed": set(),
        "loop_stats": {
            "docs-pack": _loop_stats({"usage_limit": 5}, verified=1),
        },
    }
    levers = strategist.derive_levers(context)
    assert not [lv for lv in levers if lv.key.startswith("LOOP-HEALTH-")]


def test_derive_levers_missing_loop_stats_unchanged():
    context = {
        "metrics": None,
        "ledger": {"by_class": {}, "total": 0, "entries": []},
        "suppressed": set(),
    }
    levers = strategist.derive_levers(context)
    assert levers == []


def test_grounding_gate_rejects_bare_directory_name_prose():
    """Bare prose tokens like 'tests'/'docs'/'agent' are top-level repo
    directories, but os.path.exists() matching a DIRECTORY must not count as
    a verifiable path — only file-shaped tokens (with '/' or '.') that
    resolve to a real file should pass."""
    lever = _lever_with_grounding("siehe tests und docs sowie agent fuer Details")
    result = strategist.grounding_gate(lever)
    assert result.passed is False
