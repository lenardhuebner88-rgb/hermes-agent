"""Unit tests for the deterministic ``idee:`` demand-funnel path."""

import json
import subprocess
from types import SimpleNamespace
from unittest import mock

from bridges.discord_prefilter.wish import (
    CREATED_BY,
    build_create_argv,
    extract_wish,
    normalize_wish,
    run_wish_create,
    wish_title,
)


def _config():
    return SimpleNamespace(hermes_argv=["/venv/python", "-m", "hermes_cli.main"])


# --- prefix matching --------------------------------------------------------


def test_extract_wish_matches_prefix():
    assert extract_wish("idee: dunkles theme fürs dashboard") == "dunkles theme fürs dashboard"


def test_extract_wish_case_insensitive_and_spacing():
    assert extract_wish("  IDEE :   mehr statistik  ") == "mehr statistik"


def test_extract_wish_none_without_prefix():
    assert extract_wish("bau mir einen neuen tab") is None
    assert extract_wish("eine gute idee: trotzdem kein wunsch") is None


def test_extract_wish_empty_wish_is_none():
    assert extract_wish("idee:") is None
    assert extract_wish("idee:   ") is None


# --- normalization / title --------------------------------------------------


def test_normalize_collapses_case_and_whitespace():
    assert normalize_wish("  Mehr   STATISTIK\nbitte ") == "mehr statistik bitte"


def test_title_truncates_long_first_line():
    title = wish_title("x" * 200)
    assert len(title) <= 80
    assert title.endswith("…")


# --- argv construction ------------------------------------------------------


def test_build_create_argv_shape():
    argv = build_create_argv("Dunkles Theme", "Piet", _config())
    assert argv[:3] == ["/venv/python", "-m", "hermes_cli.main"]
    assert argv[3:6] == ["kanban", "create", "Dunkles Theme"]
    assert "--triage" in argv
    assert argv[argv.index("--created-by") + 1] == CREATED_BY
    assert argv[argv.index("--idempotency-key") + 1] == "wish:dunkles theme"
    body = argv[argv.index("--body") + 1]
    assert "Piet" in body
    assert "NICHT ungefragt bauen" in body
    assert "--json" in argv


# --- subprocess wrapper (mocked) ---------------------------------------------


def test_run_wish_create_parses_task_id():
    completed = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({"id": "abc123", "status": "triage"}), stderr="",
    )
    with mock.patch("bridges.discord_prefilter.wish.subprocess.run",
                    return_value=completed) as run:
        ok, detail = run_wish_create("dunkles theme", "Piet", _config())
    assert ok and detail == "abc123"
    assert run.call_args.args[0][:3] == ["/venv/python", "-m", "hermes_cli.main"]


def test_run_wish_create_nonzero_exit_fails_with_stderr():
    completed = subprocess.CompletedProcess(
        args=[], returncode=2, stdout="", stderr="kanban: kaputt",
    )
    with mock.patch("bridges.discord_prefilter.wish.subprocess.run",
                    return_value=completed):
        ok, detail = run_wish_create("x", "Piet", _config())
    assert not ok and "kaputt" in detail


def test_run_wish_create_unparseable_output_fails():
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="Created abc123", stderr="",
    )
    with mock.patch("bridges.discord_prefilter.wish.subprocess.run",
                    return_value=completed):
        ok, detail = run_wish_create("x", "Piet", _config())
    assert not ok


def test_run_wish_create_timeout_fails():
    with mock.patch(
        "bridges.discord_prefilter.wish.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=30),
    ):
        ok, detail = run_wish_create("x", "Piet", _config())
    assert not ok and "Timeout" in detail
