from __future__ import annotations

import json

from hermes_cli.buzz_agent_heartbeat import (
    JOURNAL_WORK_PATTERN,
    AgentHeartbeat,
    HeartbeatReader,
    journal_command,
    parse_journal,
    stem_for_unit,
)


class _Result:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class _Runner:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.calls: list[list[str]] = []

    def run(self, args: list[str], *, timeout: float | None = None) -> _Result:
        self.calls.append(list(args))
        return _Result(self.returncode, self.stdout)


# Recorded verbatim from `journalctl --user -u buzz-agent@claude.service -o json
# --output-fields=_SYSTEMD_USER_UNIT -g 'tool_call:' -n 1` on 2026-08-05. Note
# both live traps: _SYSTEMD_UNIT is the *user manager*, not the agent, and
# __REALTIME_TIMESTAMP is a microsecond string.
_REAL_LINE = json.dumps({
    "__REALTIME_TIMESTAMP": "1785918203625264",
    "__SEQNUM_ID": "57c0579715b24a22a37b4920fd4be467",
    "_SYSTEMD_UNIT": "user@1000.service",
    "_SYSTEMD_USER_UNIT": "buzz-agent@claude.service",
    "_BOOT_ID": "63927d52eefe4e1487aba62d739fc046",
})
_REAL_EPOCH = 1785918203.625264


def _line(unit: str, epoch: float) -> str:
    return json.dumps({
        "_SYSTEMD_UNIT": "user@1000.service",
        "_SYSTEMD_USER_UNIT": unit,
        "__REALTIME_TIMESTAMP": str(int(epoch * 1_000_000)),
    })


def test_stem_comes_from_the_user_unit_not_the_user_manager() -> None:
    """The recorded line carries both fields; only one identifies the agent.

    Reading ``_SYSTEMD_UNIT`` would bucket every agent under
    ``user@1000.service`` — eight agents merged into one that always looks busy.
    """
    beats = parse_journal(_REAL_LINE, now=_REAL_EPOCH)

    assert list(beats) == ["claude"]
    assert beats["claude"].tool_calls_window == 1
    assert beats["claude"].last_tool_call_at == _REAL_EPOCH


def test_grep_pattern_does_not_span_the_tracing_target() -> None:
    """`acp::tool: tool_call` never matches: ANSI escapes wrap target and colon.

    Grepping for it returns zero lines and exit 0, which reads exactly like an
    idle agent. The pattern must stay anchored inside the uncoloured body.
    """
    command = journal_command(["claude"], window_seconds=3600)

    assert JOURNAL_WORK_PATTERN == "tool_call:"
    assert "acp::tool: tool_call" not in command
    assert command[command.index("-g") + 1] == "tool_call:"


def test_pattern_excludes_tool_call_update() -> None:
    """`tool_call_update` outnumbers real calls ~6:1 and would drown the count."""
    assert "tool_call_update:".find(JOURNAL_WORK_PATTERN) == -1


def test_command_asks_for_the_user_unit_field_and_a_bounded_window() -> None:
    command = journal_command(["claude", "qwen"], window_seconds=3600)

    assert "--output-fields=_SYSTEMD_USER_UNIT" in command
    assert "-u" in command and "buzz-agent@qwen.service" in command
    # Unbounded is not an option: the same query over 24 h returned 120k lines
    # in 33 s, which no request can afford.
    assert command[command.index("--since") + 1] == "-3600s"
    # MESSAGE arrives as a byte array when the line holds ANSI escapes.
    assert not any("MESSAGE" in part for part in command)


def test_recent_window_separates_working_now_from_worked_this_hour() -> None:
    now = 1_800_000_000.0
    stdout = "\n".join([
        _line("buzz-agent@claude.service", now - 30),
        _line("buzz-agent@claude.service", now - 120),
        _line("buzz-agent@claude.service", now - 2000),
        _line("buzz-agent@fable.service", now - 2500),
    ])

    beats = parse_journal(stdout, now=now, recent_seconds=300)

    assert beats["claude"].tool_calls_window == 3
    assert beats["claude"].tool_calls_recent == 2
    assert beats["claude"].working is True
    # Worked within the hour but not within five minutes: alive, not working.
    assert beats["fable"].tool_calls_window == 1
    assert beats["fable"].tool_calls_recent == 0
    assert beats["fable"].working is False


def test_quiet_agent_reports_zero_and_no_error() -> None:
    runner = _Runner(returncode=0, stdout="")

    beats, error = HeartbeatReader(runner).read(["claude", "terra"])

    assert error is None
    assert sorted(beats) == ["claude", "terra"]
    assert beats["terra"] == AgentHeartbeat("terra", 0, 0, None)
    assert beats["terra"].working is False


def test_failed_query_reports_an_error_instead_of_a_reassuring_idle() -> None:
    """A broken journal call and a quiet hour both yield zero lines.

    Returning zeroes for a failure would paint every agent as merely idle —
    the same false calm ``ActiveState`` already produces, in the other
    direction. The reader must hand the caller an error and no heartbeats.
    """
    runner = _Runner(returncode=1, stdout="")

    beats, error = HeartbeatReader(runner).read(["claude"])

    assert error == "journal_unavailable"
    assert beats == {}


def test_payload_field_names_and_iso_timestamp() -> None:
    payload = AgentHeartbeat("claude", 150, 38, _REAL_EPOCH).as_payload(
        now=_REAL_EPOCH + 90
    )

    assert payload["tool_calls_window"] == 150
    assert payload["tool_calls_recent"] == 38
    assert payload["working"] is True
    # The age travels with the payload so the phone never subtracts two clocks.
    assert payload["last_tool_call_seconds_ago"] == 90
    # An offset is mandatory: the app must not have to guess the server's zone.
    assert payload["last_tool_call_at"].startswith("2026-08-05T")
    assert payload["last_tool_call_at"][-5] in "+-"


def test_payload_distinguishes_no_activity_from_never() -> None:
    """Zero in the window means "not in this hour", not "never worked"."""
    payload = AgentHeartbeat("terra", 0, 0, None).as_payload()

    assert payload["last_tool_call_at"] is None
    assert payload["last_tool_call_seconds_ago"] is None
    assert payload["tool_calls_window"] == 0
    assert payload["working"] is False


def test_unrelated_units_and_junk_lines_are_ignored() -> None:
    stdout = "\n".join([
        "not json at all",
        json.dumps({"_SYSTEMD_USER_UNIT": "hermes-dashboard.service", "__REALTIME_TIMESTAMP": "1"}),
        json.dumps({"_SYSTEMD_USER_UNIT": "buzz-agent@qwen.service"}),  # no timestamp
        _line("buzz-agent@qwen.service", 1_800_000_000.0),
        "",
    ])

    beats = parse_journal(stdout, now=1_800_000_000.0)

    assert list(beats) == ["qwen"]
    assert beats["qwen"].tool_calls_window == 1


def test_stem_for_unit_rejects_anything_but_an_agent_unit() -> None:
    assert stem_for_unit("buzz-agent@claude.service") == "claude"
    assert stem_for_unit("user@1000.service") is None
    assert stem_for_unit("buzz-agent@claude.service.evil") is None


def test_no_stems_makes_no_call() -> None:
    runner = _Runner()

    beats, error = HeartbeatReader(runner).read([])

    assert (beats, error) == ({}, None)
    assert runner.calls == []


def test_age_never_goes_negative_on_a_skewed_clock() -> None:
    """A journal line stamped slightly ahead of ``now`` must not read as
    "worked in -4 seconds"; clamping to zero says "just now", which is true."""
    payload = AgentHeartbeat("claude", 1, 1, _REAL_EPOCH).as_payload(
        now=_REAL_EPOCH - 4
    )

    assert payload["last_tool_call_seconds_ago"] == 0
