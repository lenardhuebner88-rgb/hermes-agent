from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from hermes_cli import planspecs
from hermes_cli.subcommands.plan import plan_command


def test_plan_list_all_json_serializes_closed_at_datetime(monkeypatch, capsys):
    closed_at = datetime(2026, 7, 26, 18, 30, 45, tzinfo=timezone.utc)
    calls: list[tuple[str, bool]] = []

    def fake_list_planspecs(*, scope: str, include_kanban_status: bool):
        calls.append((scope, include_kanban_status))
        return [
            {
                "path": "/tmp/closed-planspec.md",
                "status": "shipped",
                "closed_at": closed_at,
            }
        ]

    monkeypatch.setattr(planspecs, "list_planspecs", fake_list_planspecs)
    args = argparse.Namespace(plan_action="list", all=True, json=True)

    exit_code = plan_command(args)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert calls == [("all", True)]
    assert payload["planspecs"][0]["closed_at"] == "2026-07-26T18:30:45+00:00"
