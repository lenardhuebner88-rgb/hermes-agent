"""Regression coverage for persisted Strategist PlanSpec decisions."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import strategist
from hermes_cli import strategist_specs


def _action_args(action: str, *tokens: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kanban_cli.build_parser(sub)
    return parser.parse_args(["kanban", action, *tokens])


def _held_root_with_spec(conn, path: Path) -> str:
    root = kb.create_task(
        conn,
        title="PlanSpec TEST-LEVER: held",
        body="held",
        assignee=None,
        created_by="strategist-cron",
    )
    conn.execute(
        "UPDATE tasks SET status='scheduled', freigabe='operator', planspec_source=? WHERE id=?",
        (str(path), root),
    )
    conn.commit()
    return root


def _frontmatter(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("---\n")
    return yaml.safe_load(raw.split("---", 2)[1])


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [("veto-freigabe", "vetoed"), ("release-freigabe", "released")],
)
def test_cli_freigabe_decision_persists_real_spec_frontmatter(
    kanban_home, tmp_path: Path, action: str, expected_status: str
):
    spec = tmp_path / "real-spec.md"
    spec.write_text(
        "---\nstatus: vorgeschlagen\nslice: TEST-LEVER\n---\n\n# Real PlanSpec\n",
        encoding="utf-8",
    )
    with kb.connect() as conn:
        root = _held_root_with_spec(conn, spec)

    assert kanban_cli.kanban_command(_action_args(action, root, "--author", "operator")) == 0

    metadata = _frontmatter(spec)
    assert metadata["status"] == expected_status
    assert metadata["decision_by"] == "operator"
    assert metadata["decision_at"].endswith("Z")


@pytest.mark.parametrize("spec_exists", [False, True])
def test_cli_freigabe_decision_survives_missing_or_unwritable_spec(
    kanban_home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spec_exists: bool
):
    spec = tmp_path / "missing-or-unwritable.md"
    if spec_exists:
        spec.write_text("---\nstatus: vorgeschlagen\n---\n", encoding="utf-8")
        monkeypatch.setattr(
            strategist_specs,
            "update_spec_frontmatter",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read-only")),
        )
    with kb.connect() as conn:
        root = _held_root_with_spec(conn, spec)

    assert kanban_cli.kanban_command(
        _action_args("veto-freigabe", root, "--author", "operator")
    ) == 0
    with kb.connect() as conn:
        assert kb.get_task(conn, root).status == "archived"


def test_reconcile_proposed_specs_dry_run_maps_board_and_never_ingested(
    kanban_home, tmp_path: Path
):
    plans_root = tmp_path / "specs"
    plans_root.mkdir()
    archived = plans_root / "archived.md"
    unplayed = plans_root / "unplayed.md"
    for path, lever in ((archived, "GATE-TRIAGE-PYTHON-aaaaaaaa"), (unplayed, "OTHER")):
        path.write_text(
            f"---\nstatus: vorgeschlagen\nslice: {lever}\n---\n\n# {lever}\n",
            encoding="utf-8",
        )
    with kb.connect() as conn:
        root = _held_root_with_spec(conn, archived)
        assert kb.dismiss_freigabe_hold(conn, root, author="operator") is True
        result = strategist_specs.reconcile_proposed_specs(conn, plans_root=plans_root, apply=False)

    by_path = {item["path"]: item for item in result}
    assert by_path[str(archived)]["status"] == "vetoed"
    assert by_path[str(unplayed)]["status"] == "nie-eingespielt"
    assert _frontmatter(archived)["status"] == "vorgeschlagen"
    assert _frontmatter(unplayed)["status"] == "vorgeschlagen"


@pytest.mark.parametrize("status", ["vetoed", "released"])
def test_terminal_decision_dedup_uses_lever_not_random_hash(tmp_path: Path, status: str):
    plans_root = tmp_path / "specs"
    plans_root.mkdir()
    (plans_root / "gate-triage-python-oldhash.md").write_text(
        f"---\nstatus: {status}\nslice: GATE-TRIAGE-PYTHON-a1b2c3d4\n---\n",
        encoding="utf-8",
    )

    assert strategist_specs.has_terminal_decision_for_lever(
        plans_root, "GATE-TRIAGE-PYTHON-deadbeef"
    ) is True


def test_gate_paths_skip_terminal_decisions_before_writing_or_ingesting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    specs = tmp_path / "specs"
    specs.mkdir()
    gate_records = [
        {
            "date": date,
            "result": "fail",
            "ts": f"{date}T03:00:00+00:00",
            "first_fail": {"gate": "python", "detail": "tests/example/test_x.py failed"},
        }
        for date in ("2026-07-01", "2026-07-02", "2026-07-03")
    ]
    flaky_records = [
        {
            "date": "2026-07-01",
            "result": "fail",
            "ts": "2026-07-01T03:00:00+00:00",
            "leakers": ["python: tests/example/test_flaky.py"],
        }
    ]
    dry_gate = strategist.propose_gate_fix(
        out_dir=specs, gate_records=gate_records[:2], do_ingest=False
    )
    dry_triage = strategist.propose_persistent_red_triage(
        out_dir=specs, gate_records=gate_records, do_ingest=False
    )
    dry_deflake = strategist.propose_deflake(
        out_dir=specs, gate_records=flaky_records, do_ingest=False
    )
    for index, key in enumerate(
        (dry_gate["key"], dry_triage["key"], dry_deflake["filed"][0]["key"])
    ):
        variant = f"{key[:-8]}{index:08x}"
        (specs / f"terminal-{index}.md").write_text(
            f"---\nstatus: vetoed\nslice: {variant}\n---\n", encoding="utf-8"
        )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("terminal decision must skip write and ingest")

    monkeypatch.setattr(strategist, "_write_spec", forbidden)
    monkeypatch.setattr(strategist.planspecs, "ingest_planspec", forbidden)

    gate = strategist.propose_gate_fix(out_dir=specs, gate_records=gate_records[:2])
    triage = strategist.propose_persistent_red_triage(out_dir=specs, gate_records=gate_records)
    deflake = strategist.propose_deflake(out_dir=specs, gate_records=flaky_records)

    assert gate["skipped_terminal_decision"] is True
    assert triage["skipped_terminal_decision"] is True
    assert deflake["filed"] == []
    assert deflake["skipped_terminal_decision"] == ["tests/example/test_flaky.py"]
