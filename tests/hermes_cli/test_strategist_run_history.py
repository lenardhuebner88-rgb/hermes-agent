from __future__ import annotations
from pathlib import Path
import json
from hermes_cli import strategist


def test_append_and_read_last_runs(tmp_path: Path):
    sd = tmp_path
    strategist.append_run_history(sd, {"ts": 100, "mode": "harvest", "receipts": 5, "candidates": 2})
    strategist.append_run_history(sd, {"ts": 200, "mode": "propose", "candidates": 4, "ingested": 1})
    strategist.append_run_history(sd, {"ts": 300, "mode": "harvest", "receipts": 9, "candidates": 3})
    strategist.append_run_history(sd, {"ts": 400, "mode": "harvest-watch", "triggered": False})
    last = strategist.read_last_runs(sd)
    assert last["harvest"] == {"ts": 300, "mode": "harvest", "receipts": 9, "candidates": 3}
    assert last["harvest-watch"] == {"ts": 400, "mode": "harvest-watch", "triggered": False}
    assert last["propose"] == {"ts": 200, "mode": "propose", "candidates": 4, "ingested": 1}


def test_read_last_runs_missing_file(tmp_path: Path):
    assert strategist.read_last_runs(tmp_path) == {
        "harvest": None,
        "harvest-watch": None,
        "propose": None,
        "digest": None,
    }


def test_read_last_runs_skips_corrupt_lines(tmp_path: Path):
    (tmp_path / "run-history.jsonl").write_text(
        '{"ts":1,"mode":"harvest","receipts":1,"candidates":1}\nNOT JSON\n', encoding="utf-8"
    )
    last = strategist.read_last_runs(tmp_path)
    assert last["harvest"]["ts"] == 1
    assert last["propose"] is None
