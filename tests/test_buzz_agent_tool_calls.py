"""Tests for the fork-owned tool-call reader.

The fixture is **real** journal output, captured 2026-08-05 from the live
``buzz-agent@*`` units: ANSI escapes intact, ``MESSAGE`` as the byte array
journalctl actually emits, six units interleaved. A hand-written JSON blob would
have passed every test here while the production path returned nothing, which is
precisely the failure this module exists to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.buzz_agent_tool_calls import (
    ActivityReader,
    Diagnostics,
    activity_command,
    decode_message,
    mask_secrets,
    parse_activity,
    split_kind,
)

FIXTURE = Path(__file__).parent / "fixtures" / "buzz_agent_journal_activity.jsonl"


@pytest.fixture(scope="module")
def journal_stdout() -> str:
    return FIXTURE.read_text(encoding="utf-8")


class FakeResult:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class FakeRunner:
    def __init__(self, result: FakeResult) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    def run(self, args: list[str], *, timeout: float | None = None) -> FakeResult:
        self.calls.append(args)
        return self.result


# --- decoding -------------------------------------------------------------


def test_decode_message_handles_the_byte_array_shape(journal_stdout: str) -> None:
    """Every line in the live journal arrives as a list of ints, not a string."""
    entries = [json.loads(line) for line in journal_stdout.splitlines() if line.strip()]
    assert entries, "fixture is empty"
    assert all(isinstance(entry["MESSAGE"], list) for entry in entries), (
        "fixture no longer exercises the byte-array path — recapture it"
    )
    decoded = decode_message(entries[0]["MESSAGE"])
    assert isinstance(decoded, str)
    assert "tool_call" in decoded


def test_decode_message_passes_plain_strings_through() -> None:
    assert decode_message("tool_call: Read (read)") == "tool_call: Read (read)"


def test_decode_message_rejects_nonsense() -> None:
    assert decode_message(None) is None
    assert decode_message(42) is None


# --- label / kind ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "label", "kind"),
    [
        ("Terminal (execute)", "Terminal", "execute"),
        ("Edit (edit)", "Edit", "edit"),
        ("Guardian Review (think)", "Guardian Review", "think"),
        ("run_terminal_command (unknown)", "run_terminal_command", "unknown"),
        # The kind is the *last* parenthesised word, not the first.
        (
            "terminal: python: from hermes_tools import terminal (execute)",
            "terminal: python: from hermes_tools import terminal",
            "execute",
        ),
        # A heredoc's first physical line carries no kind and must keep its text.
        ("python3 - <<'PY'", "python3 - <<'PY'", "unknown"),
        ("set -o pipefail", "set -o pipefail", "unknown"),
    ],
)
def test_split_kind(raw: str, label: str, kind: str) -> None:
    assert split_kind(raw) == (label, kind)


# --- masking --------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "curl -H 'Authorization: Bearer abcdef1234567890'",
        "export HERMES_TOKEN=abcdef1234567890",
        "psql password=hunter2seventeen",
        "echo sk-proj-AAAABBBBCCCCDDDD",
        "gh auth ghp_AAAABBBBCCCCDDDD1234",
        "buzz import nsec1qqqqqqqqqqqqqqqqqqqqqqqq",
    ],
)
def test_mask_secrets_removes_the_credential(secret: str) -> None:
    masked = mask_secrets(secret)
    assert "***" in masked
    for token in ("abcdef1234567890", "hunter2seventeen", "AAAABBBBCCCCDDDD"):
        assert token not in masked


def test_mask_secrets_leaves_ordinary_commands_alone() -> None:
    command = "rg -n dispatch_once hermes_cli/kanban_db.py"
    assert mask_secrets(command) == command


# --- parsing the real journal --------------------------------------------


def test_parse_activity_understands_the_live_journal(journal_stdout: str) -> None:
    timelines, diagnostics = parse_activity(journal_stdout)

    assert diagnostics.lines_seen > 0
    assert not diagnostics.suspicious
    # Every line in the fixture matched `-g tool_call`, so every line is either
    # a call or an update — nothing should fall through unrecognised.
    assert diagnostics.lines_understood == diagnostics.lines_seen
    assert len(timelines) >= 4, "fixture spans six units; parser lost some"


def test_parse_activity_keeps_units_apart(journal_stdout: str) -> None:
    """`_SYSTEMD_USER_UNIT`, not `_SYSTEMD_UNIT` — the latter merges all agents."""
    timelines, _ = parse_activity(journal_stdout)
    assert set(timelines) <= {"claude", "codex", "qwen", "hermes", "kimi", "fable"}
    assert "user@1000" not in timelines


def test_parse_activity_recovers_real_tool_labels(journal_stdout: str) -> None:
    timelines, _ = parse_activity(journal_stdout)
    labels = [call.label for timeline in timelines.values() for call in timeline.calls]
    assert labels, "no tool call survived parsing"
    assert all(label for label in labels), "an empty label is a parse failure"
    assert all("\x1b" not in label for label in labels), "ANSI leaked into a label"


def test_parse_activity_orders_calls_oldest_first(journal_stdout: str) -> None:
    timelines, _ = parse_activity(journal_stdout)
    for timeline in timelines.values():
        stamps = [call.at for call in timeline.calls]
        assert stamps == sorted(stamps)


def test_parse_activity_reports_a_format_change_instead_of_silence() -> None:
    """Lines arrive, none parse — the fleet is not idle, the format moved."""
    alien = "\n".join(
        json.dumps(
            {
                "_SYSTEMD_USER_UNIT": "buzz-agent@claude.service",
                "__REALTIME_TIMESTAMP": "1000000000000000",
                "MESSAGE": "acp::tool: invoked something entirely different",
            }
        )
        for _ in range(5)
    )
    timelines, diagnostics = parse_activity(alien)
    assert timelines == {}
    assert diagnostics.lines_seen == 5
    assert diagnostics.suspicious


def test_parse_activity_on_an_empty_window_is_not_suspicious() -> None:
    timelines, diagnostics = parse_activity("")
    assert timelines == {}
    assert diagnostics == Diagnostics(lines_seen=0, lines_understood=0)
    assert not diagnostics.suspicious


# --- current call ---------------------------------------------------------


def _entry(unit: str, at: float, message: str) -> str:
    return json.dumps(
        {
            "_SYSTEMD_USER_UNIT": f"buzz-agent@{unit}.service",
            "__REALTIME_TIMESTAMP": str(int(at * 1_000_000)),
            "MESSAGE": message,
        }
    )


def test_latest_looks_open_until_something_completes() -> None:
    stdout = "\n".join(
        [
            _entry("codex", 100, "tool_call: Read (read)"),
            _entry("codex", 101, "tool_call_update: toolu_A → completed"),
            _entry("codex", 200, "tool_call: terminal: rg -n dispatch_once (execute)"),
            _entry("codex", 230, "tool_call_update: toolu_B → in_progress"),
        ]
    )
    timelines, _ = parse_activity(stdout)
    latest = timelines["codex"].latest
    assert latest is not None
    assert latest.label == "terminal: rg -n dispatch_once"
    assert latest.kind == "execute"
    payload = timelines["codex"].as_payload(now=260.0)
    assert payload["looks_open"] is True
    assert payload["open_for_seconds"] == 60
    # The in_progress update is younger than the call: still alive, not hung.
    assert payload["last_signal_seconds_ago"] == 30


def test_looks_open_is_false_once_something_completed() -> None:
    stdout = "\n".join(
        [
            _entry("kimi", 100, "tool_call: Edit (edit)"),
            _entry("kimi", 140, "tool_call_update: toolu_A → completed"),
        ]
    )
    timelines, _ = parse_activity(stdout)
    assert timelines["kimi"].open_since is None
    payload = timelines["kimi"].as_payload(now=200.0)
    assert payload["looks_open"] is False
    assert payload["open_for_seconds"] is None
    # The call itself is still reported — "nothing running" must not mean
    # "nothing to show"; the operator still wants to see what it was.
    assert payload["latest"]["label"] == "Edit"


def test_a_completion_older_than_the_call_does_not_close_it() -> None:
    """Ordering, not identity — a stale completion must not silence a new call."""
    stdout = "\n".join(
        [
            _entry("qwen", 100, "tool_call_update: toolu_OLD → completed"),
            _entry("qwen", 300, "tool_call: Bash (execute)"),
        ]
    )
    timelines, _ = parse_activity(stdout)
    assert timelines["qwen"].open_since is not None


def test_payload_recent_is_newest_first_and_capped() -> None:
    stdout = "\n".join(
        _entry("claude", 100 + i, f"tool_call: Step{i} (execute)") for i in range(30)
    )
    timelines, _ = parse_activity(stdout)
    payload = timelines["claude"].as_payload(now=200.0, limit=5)
    labels = [call["label"] for call in payload["recent"]]
    assert labels == ["Step29", "Step28", "Step27", "Step26", "Step25"]
    assert payload["calls_in_window"] == 30


def test_labels_are_masked_and_clamped() -> None:
    long_tail = "x" * 500
    stdout = _entry("hermes", 100, f"tool_call: terminal: curl -H 'Authorization: Bearer {long_tail}' (execute)")
    timelines, _ = parse_activity(stdout)
    label = timelines["hermes"].calls[0].label
    assert len(label) <= 160
    assert long_tail[:40] not in label
    assert "***" in label


# --- the reader -----------------------------------------------------------


def test_activity_command_asks_for_the_message_body() -> None:
    args = activity_command(["claude", "codex"], window_seconds=600)
    assert "--output-fields=_SYSTEMD_USER_UNIT,MESSAGE" in args
    assert args[args.index("-g") + 1] == "tool_call"
    assert "-u" in args and "buzz-agent@claude.service" in args
    assert "--since" in args and "-600s" in args


def test_reader_distinguishes_a_broken_query_from_a_quiet_fleet() -> None:
    reader = ActivityReader(FakeRunner(FakeResult(stdout="", returncode=1)))
    timelines, diagnostics, error = reader.read(["claude"])
    assert error == "journal_unavailable"
    assert timelines == {}

    reader = ActivityReader(FakeRunner(FakeResult(stdout="", returncode=0)))
    timelines, diagnostics, error = reader.read(["claude"])
    assert error is None
    assert timelines["claude"].calls == []


def test_reader_fills_in_agents_that_made_no_call(journal_stdout: str) -> None:
    reader = ActivityReader(FakeRunner(FakeResult(stdout=journal_stdout)))
    timelines, _, error = reader.read(["claude", "grok"])
    assert error is None
    assert set(timelines) == {"claude", "grok"}
    assert timelines["grok"].calls == []


def test_reader_caches_within_its_ttl() -> None:
    clock = iter([1000.0, 1001.0, 1100.0])
    runner = FakeRunner(FakeResult(stdout=""))
    reader = ActivityReader(runner, ttl_seconds=5.0, clock=lambda: next(clock))
    reader.read(["claude"])
    reader.read(["claude"])
    assert len(runner.calls) == 1, "a phone polling every 8 s must not scan twice"
    reader.read(["claude"])
    assert len(runner.calls) == 2, "the cache must expire"


def test_reader_does_not_serve_one_agent_set_from_another() -> None:
    runner = FakeRunner(FakeResult(stdout=""))
    reader = ActivityReader(runner, ttl_seconds=60.0, clock=lambda: 1000.0)
    reader.read(["claude"])
    reader.read(["claude", "codex"])
    assert len(runner.calls) == 2
