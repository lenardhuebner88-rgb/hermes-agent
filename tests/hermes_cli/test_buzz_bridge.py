from __future__ import annotations

import json
import logging
from collections.abc import Callable

import pytest

from hermes_cli.buzz_bridge import (
    DEFAULT_APPROVERS,
    BridgeOutcome,
    BuzzReleaseGateBridge,
    CommandResult,
    ReleaseGateConfig,
    WorkMarker,
    clear_work_marker,
    load_release_gate_config,
    read_work_markers,
    set_work_marker,
)

APPROVER = "a" * 64
OUTSIDER = "b" * 64
EVENT_ID = "c" * 64
AGENT_A = "f1d66f405a79e94579dfdd629dc1b2e09c7e6752bc2baa2867aa925c33db31e9"
AGENT_B = "2" * 64

# Literal stdout captured on 2026-08-01 from the release Buzz binary:
# /mnt/data/services/buzz/target/release/buzz reactions get --event <hex64>
# The binary queried an isolated local protocol stub containing three raw
# kind-7 reaction records. The CLI itself produced this aggregated JSON.
RECORDED_REACTIONS_GET_OUTPUT = (
    '{"reactions":[{"count":2,"emoji":"✅","pubkeys":['
    '"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    '"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]},'
    '{"count":1,"emoji":"👀","pubkeys":['
    '"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]}]}'
)

# Schema and values transcribed from the 2026-08-01 release-CLI probes. Long
# identifiers abbreviated in the operator transcript are expanded here so the
# fixture remains valid JSON while preserving every measured field.
RECORDED_NOTES_LS_OUTPUT = (
    '[{"id":"0085e32c00000000000000000000000000000000000000000000000000000000",'
    '"pubkey":"' + AGENT_A + '","naddr":"naddr1qqxnzd3cxyerxd3h8qerwwfc",'
    '"coordinate":"30023:' + AGENT_A + ':work-marker",'
    '"slug":"work-marker","title":"S9 Probe","summary":null,'
    '"tags":["work-marker"],"published_at":1785613165,'
    '"updated_at":1785613165,"content":"working:t_x"}]'
)
RECORDED_PRESENCE_OUTPUT = (
    '[{"pubkey":"' + AGENT_A + '","status":"online",'
    '"updated_at":1785613178}]'
)


class FakeRunner:
    def __init__(self, reactions: str) -> None:
        self.reactions = reactions
        self.calls: list[list[str]] = []
        self.contents: list[str] = []

    def run(self, args: list[str]) -> CommandResult:
        self.calls.append(list(args))
        if args[1:3] == ["messages", "send"]:
            self.contents.append(args[args.index("--content") + 1])
            return CommandResult(
                0,
                '{"event_id":"' + EVENT_ID + '","accepted":true,"message":""}',
                "",
            )
        if args[1:3] == ["reactions", "get"]:
            return CommandResult(0, self.reactions, "")
        if args[:3] == ["hermes-test", "kanban", "release-gate"]:
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected command: {args}")


class WorkMarkerRunner:
    def __init__(self, notes: str = "[]", presence: str = "[]") -> None:
        self.notes = notes
        self.presence = presence
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> CommandResult:
        self.calls.append(list(args))
        if args[1:3] == ["notes", "set"] or args[1:3] == ["notes", "rm"]:
            return CommandResult(0, "", "")
        if args[1:3] == ["notes", "ls"]:
            return CommandResult(0, self.notes, "")
        if args[1:3] == ["users", "presence"]:
            return CommandResult(0, self.presence, "")
        raise AssertionError(f"unexpected command: {args}")


class AdvancingClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        current = self.value
        self.value += 1.0
        return current


def _config(*, approvers: frozenset[str], emoji: str = "✅") -> ReleaseGateConfig:
    return ReleaseGateConfig(
        approvers=approvers,
        channel_id="channel-123",
        emoji=emoji,
        poll_interval_seconds=1.0,
        timeout_seconds=0.5,
    )


def _bridge(
    runner: FakeRunner,
    *,
    approvers: frozenset[str],
    emoji: str = "✅",
    sleep: Callable[[float], None] = lambda _seconds: None,
) -> BuzzReleaseGateBridge:
    return BuzzReleaseGateBridge(
        _config(approvers=approvers, emoji=emoji),
        runner=runner,
        buzz_binary="buzz-test",
        hermes_binary="hermes-test",
        monotonic=AdvancingClock(),
        sleep=sleep,
    )


def _run(bridge: BuzzReleaseGateBridge) -> BridgeOutcome:
    return bridge.run(
        task_id="t_gate_123",
        chain="t_chain_456",
        merge_commit="abc123def456",
        hold_reason="live_test_depth ui-real",
    )


def _release_calls(runner: FakeRunner) -> list[list[str]]:
    return [
        call
        for call in runner.calls
        if call[:3] == ["hermes-test", "kanban", "release-gate"]
    ]


def test_request_contains_release_context_and_remembers_event_id() -> None:
    runner = FakeRunner('{"reactions":[]}')
    bridge = _bridge(runner, approvers=frozenset({APPROVER}))

    event_id = bridge.post_request(
        task_id="t_gate_123",
        chain="t_chain_456",
        merge_commit="abc123def456",
        hold_reason="live_test_depth ui-real",
    )

    assert event_id == EVENT_ID
    assert bridge.event_id == EVENT_ID
    assert len(runner.contents) == 1
    content = runner.contents[0]
    assert "t_gate_123" in content
    assert "t_chain_456" in content
    assert "abc123def456" in content
    assert "live_test_depth ui-real" in content


def test_recorded_reactions_output_authorizes_allowlisted_pubkey_once() -> None:
    runner = FakeRunner(RECORDED_REACTIONS_GET_OUTPUT)
    bridge = _bridge(runner, approvers=frozenset({APPROVER}))

    outcome = _run(bridge)

    assert outcome.status == "approved"
    assert outcome.event_id == EVENT_ID
    assert outcome.release_gate_called is True
    assert _release_calls(runner) == [
        ["hermes-test", "kanban", "release-gate", "t_gate_123"]
    ]


def test_same_emoji_from_nonapprover_does_nothing_and_logs_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = FakeRunner(
        '{"reactions":[{"count":1,"emoji":"✅","pubkeys":["'
        + OUTSIDER
        + '"]}]}'
    )
    bridge = _bridge(runner, approvers=frozenset({APPROVER}))

    with caplog.at_level(logging.INFO, logger="hermes_cli.buzz_bridge"):
        outcome = _run(bridge)

    assert outcome.status == "timed_out"
    assert outcome.release_gate_called is False
    assert _release_calls(runner) == []
    assert "not in the approver allowlist" in caplog.text


def test_other_emoji_from_approver_does_nothing() -> None:
    runner = FakeRunner(
        '{"reactions":[{"count":1,"emoji":"👀","pubkeys":["'
        + APPROVER
        + '"]}]}'
    )
    bridge = _bridge(runner, approvers=frozenset({APPROVER}))

    outcome = _run(bridge)

    assert outcome.status == "timed_out"
    assert outcome.release_gate_called is False
    assert _release_calls(runner) == []


@pytest.mark.parametrize("approvers", [None, []])
def test_missing_or_empty_approvers_use_owner_default(approvers: object) -> None:
    config = load_release_gate_config(
        {"kanban": {"release_gate": {"approvers": approvers}}}
    )

    assert config.approvers == frozenset(DEFAULT_APPROVERS)
    assert config.approvers


def test_claim_sets_single_tagged_work_marker_note() -> None:
    runner = WorkMarkerRunner()

    set_work_marker("t_x", runner=runner, buzz_binary="buzz-test")

    assert runner.calls == [
        [
            "buzz-test",
            "notes",
            "set",
            "--name",
            "work-marker",
            "--title",
            "Hermes Work Marker",
            "--tag",
            "work-marker",
            "--content",
            "working:t_x",
        ]
    ]


def test_completion_removes_work_marker_note() -> None:
    runner = WorkMarkerRunner()

    clear_work_marker(runner=runner, buzz_binary="buzz-test")

    assert runner.calls == [
        ["buzz-test", "notes", "rm", "--name", "work-marker"]
    ]


def test_recorded_note_and_online_presence_report_active_worker() -> None:
    runner = WorkMarkerRunner(RECORDED_NOTES_LS_OUTPUT, RECORDED_PRESENCE_OUTPUT)

    markers = read_work_markers({}, runner=runner, buzz_binary="buzz-test")

    assert markers == [
        WorkMarker(
            pubkey=AGENT_A,
            display_name=AGENT_A,
            task_id="t_x",
            alive=True,
            stale=False,
        )
    ]


def test_missing_presence_entry_reports_stale_not_active() -> None:
    runner = WorkMarkerRunner(RECORDED_NOTES_LS_OUTPUT, "[]")

    markers = read_work_markers({}, runner=runner, buzz_binary="buzz-test")

    assert len(markers) == 1
    assert markers[0].task_id == "t_x"
    assert markers[0].alive is False
    assert markers[0].stale is True


def test_non_working_note_content_produces_no_worker() -> None:
    notes = RECORDED_NOTES_LS_OUTPUT.replace("working:t_x", "reviewing:t_x")
    runner = WorkMarkerRunner(notes, RECORDED_PRESENCE_OUTPUT)

    markers = read_work_markers({}, runner=runner, buzz_binary="buzz-test")

    assert markers == []
    assert [call for call in runner.calls if call[1:3] == ["users", "presence"]] == []


def test_missing_work_marker_config_falls_back_to_pubkey() -> None:
    runner = WorkMarkerRunner(RECORDED_NOTES_LS_OUTPUT, RECORDED_PRESENCE_OUTPUT)

    marker = read_work_markers({}, runner=runner, buzz_binary="buzz-test")[0]

    assert marker.display_name == AGENT_A


def test_configured_name_is_used_for_pubkey() -> None:
    runner = WorkMarkerRunner(RECORDED_NOTES_LS_OUTPUT, RECORDED_PRESENCE_OUTPUT)
    config = {"kanban": {"work_marker": {AGENT_A: "Codex"}}}

    marker = read_work_markers(config, runner=runner, buzz_binary="buzz-test")[0]

    assert marker.display_name == "Codex"


def test_two_agents_on_same_task_are_informational_and_use_one_presence_call() -> None:
    second_note = {
        "pubkey": AGENT_B,
        "slug": "work-marker",
        "content": "working:t_x",
    }
    notes = json.loads(RECORDED_NOTES_LS_OUTPUT) + [second_note]
    presence = [
        {"pubkey": AGENT_A, "status": "online", "updated_at": 1785613178},
        {"pubkey": AGENT_B, "status": "online", "updated_at": 1785613178},
    ]
    runner = WorkMarkerRunner(json.dumps(notes), json.dumps(presence))

    markers = read_work_markers({}, runner=runner, buzz_binary="buzz-test")

    assert [(marker.pubkey, marker.task_id, marker.alive) for marker in markers] == [
        (AGENT_A, "t_x", True),
        (AGENT_B, "t_x", True),
    ]
    presence_calls = [
        call for call in runner.calls if call[1:3] == ["users", "presence"]
    ]
    assert presence_calls == [
        [
            "buzz-test",
            "users",
            "presence",
            "--pubkeys",
            f"{AGENT_A},{AGENT_B}",
        ]
    ]
