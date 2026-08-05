from __future__ import annotations

import json
import logging
import runpy
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from loops.runner import PACKS_DIR, load_pack
from scripts.dead_code_tombstone import Candidate, instrument_candidate, observe_night


PACK_NAME = "dead-code-scout"


def _candidate() -> Candidate:
    return Candidate(
        path="loops/sample.py",
        line=1,
        name="maybe_live",
        kind="function",
    )


def _write_candidate(repo: Path) -> Path:
    source = repo / "loops" / "sample.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def maybe_live(value):\n    return value + 1\n",
        encoding="utf-8",
    )
    return source


def _state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_manifest_configures_tombstone_threshold_and_documents_rare_path_limit():
    pack = load_pack(PACKS_DIR, PACK_NAME)

    assert pack.params["tombstone_tool"] == "scripts/dead_code_tombstone.py"
    assert pack.params["tombstone_silent_nights"] == "14"
    assert "selten" in pack.params["tombstone_limit"].lower()
    assert "zwei Wochen" in pack.params["tombstone_limit"]


def test_runtime_tombstone_logs_when_instrumented_path_is_reached(
    tmp_path: Path,
    caplog,
):
    repo = tmp_path / "repo"
    source = _write_candidate(repo)
    state_path = tmp_path / "state.json"
    ledger_path = tmp_path / "LEDGER.md"

    candidate_id = instrument_candidate(
        repo_root=repo,
        state_path=state_path,
        ledger_path=ledger_path,
        candidate=_candidate(),
        night=date(2026, 8, 5),
    )

    caplog.set_level(logging.WARNING, logger="hermes.dead_code_tombstone")
    namespace = runpy.run_path(str(source))
    assert namespace["maybe_live"](1) == 2
    assert f"DEAD_CODE_TOMBSTONE_HIT {candidate_id}" in caplog.text


def test_variable_candidate_logs_only_when_its_statement_is_reached(
    tmp_path: Path,
    caplog,
):
    repo = tmp_path / "repo"
    source = repo / "loops" / "sample.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def owner():\n    area_options = ['only-when-called']\n    return 1\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "state.json"
    ledger_path = tmp_path / "LEDGER.md"
    candidate = Candidate(
        path="loops/sample.py",
        line=2,
        name="area_options",
        kind="variable",
    )

    candidate_id = instrument_candidate(
        repo_root=repo,
        state_path=state_path,
        ledger_path=ledger_path,
        candidate=candidate,
        night=date(2026, 8, 5),
    )
    caplog.set_level(logging.WARNING, logger="hermes.dead_code_tombstone")
    namespace = runpy.run_path(str(source))
    assert f"DEAD_CODE_TOMBSTONE_HIT {candidate_id}" not in caplog.text
    assert namespace["owner"]() == 1
    assert f"DEAD_CODE_TOMBSTONE_HIT {candidate_id}" in caplog.text

    observe_night(
        repo_root=repo,
        state_path=state_path,
        ledger_path=ledger_path,
        night=date(2026, 8, 6),
        hit_ids=set(),
        silent_nights_threshold=1,
    )
    assert "area_options" not in source.read_text(encoding="utf-8")
    assert runpy.run_path(str(source))["owner"]() == 1


def test_hit_sets_watermark_and_permanently_prevents_deletion(tmp_path: Path):
    repo = tmp_path / "repo"
    source = _write_candidate(repo)
    state_path = tmp_path / "state.json"
    ledger_path = tmp_path / "LEDGER.md"
    first_night = date(2026, 8, 5)
    candidate_id = instrument_candidate(
        repo_root=repo,
        state_path=state_path,
        ledger_path=ledger_path,
        candidate=_candidate(),
        night=first_night,
    )

    observe_night(
        repo_root=repo,
        state_path=state_path,
        ledger_path=ledger_path,
        night=first_night + timedelta(days=1),
        hit_ids=set(),
        silent_nights_threshold=2,
    )
    observe_night(
        repo_root=repo,
        state_path=state_path,
        ledger_path=ledger_path,
        night=first_night + timedelta(days=2),
        hit_ids={candidate_id},
        silent_nights_threshold=2,
    )
    after_hit = _state(state_path)["candidates"][candidate_id]
    assert after_hit["hit_watermark_night"] == "2026-08-07"
    assert after_hit["silent_nights"] == 0
    assert after_hit["status"] == "protected"

    for offset in (3, 4, 5):
        observe_night(
            repo_root=repo,
            state_path=state_path,
            ledger_path=ledger_path,
            night=first_night + timedelta(days=offset),
            hit_ids=set(),
            silent_nights_threshold=2,
        )
        assert source.exists()
        assert "def maybe_live" in source.read_text(encoding="utf-8")

    protected = _state(state_path)["candidates"][candidate_id]
    assert protected["status"] == "protected"
    assert protected["hit_watermark_night"] == "2026-08-07"
    assert protected["silent_nights"] == 0


def test_never_hit_candidate_is_deleted_and_ledger_names_silent_nights(tmp_path: Path):
    repo = tmp_path / "repo"
    source = _write_candidate(repo)
    state_path = tmp_path / "state.json"
    ledger_path = tmp_path / "LEDGER.md"
    first_night = date(2026, 8, 5)
    candidate_id = instrument_candidate(
        repo_root=repo,
        state_path=state_path,
        ledger_path=ledger_path,
        candidate=_candidate(),
        night=first_night,
    )

    for offset in (1, 2):
        observe_night(
            repo_root=repo,
            state_path=state_path,
            ledger_path=ledger_path,
            night=first_night + timedelta(days=offset),
            hit_ids=set(),
            silent_nights_threshold=2,
        )

    assert "maybe_live" not in source.read_text(encoding="utf-8")
    deleted = _state(state_path)["candidates"][candidate_id]
    assert deleted["status"] == "deleted"
    assert deleted["silent_nights"] == 2
    ledger = ledger_path.read_text(encoding="utf-8")
    assert f"DELETED candidate={candidate_id}" in ledger
    assert "silent_nights=2" in ledger


def test_same_night_is_counted_only_once(tmp_path: Path):
    repo = tmp_path / "repo"
    _write_candidate(repo)
    state_path = tmp_path / "state.json"
    ledger_path = tmp_path / "LEDGER.md"
    first_night = date(2026, 8, 5)
    candidate_id = instrument_candidate(
        repo_root=repo,
        state_path=state_path,
        ledger_path=ledger_path,
        candidate=_candidate(),
        night=first_night,
    )

    for _ in range(2):
        observe_night(
            repo_root=repo,
            state_path=state_path,
            ledger_path=ledger_path,
            night=first_night + timedelta(days=1),
            hit_ids=set(),
            silent_nights_threshold=2,
        )

    candidate_state = _state(state_path)["candidates"][candidate_id]
    assert candidate_state["silent_nights"] == 1
    assert candidate_state["status"] == "instrumented"
