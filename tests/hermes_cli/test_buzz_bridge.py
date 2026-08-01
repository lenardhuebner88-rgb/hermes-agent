from __future__ import annotations

import logging
from collections.abc import Callable

import pytest

from hermes_cli.buzz_bridge import (
    DEFAULT_APPROVERS,
    BridgeOutcome,
    BuzzReleaseGateBridge,
    CommandResult,
    ReleaseGateConfig,
    load_release_gate_config,
)

APPROVER = "a" * 64
OUTSIDER = "b" * 64
EVENT_ID = "c" * 64

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
