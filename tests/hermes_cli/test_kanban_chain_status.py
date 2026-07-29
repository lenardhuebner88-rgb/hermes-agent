"""Regression tests for source-preserving integration-park classification."""

from __future__ import annotations

from hermes_cli.kanban_chain_status import blocked_event_kind, is_integration_park


def test_typed_nonintegration_blocked_event_overrides_legacy_reason_prefix():
    assert not is_integration_park(
        reason="integration parked: stale legacy reason",
        block_kind="review_revision",
    )


def test_untyped_blocked_event_uses_legacy_reason_even_if_task_row_is_capacity():
    # The stall sweep may update tasks.block_kind without writing another blocked
    # event.  That row therefore must not override an untyped block episode.
    event_kind = blocked_event_kind({})

    assert event_kind is None
    assert is_integration_park(
        reason="integration parked: merge conflict/failure (aborted): foo.py",
        block_kind=event_kind,
    )


def test_legacy_transient_event_uses_its_integration_reason():
    event_kind = blocked_event_kind({"kind": "transient"})

    assert event_kind is None
    assert is_integration_park(
        reason="integration parked: merge conflict/failure (aborted): foo.py",
        block_kind=event_kind,
    )


def test_typed_integration_event_stays_integration_when_task_row_is_capacity():
    event_kind = blocked_event_kind({"kind": "integration"})

    assert event_kind == "integration"
    assert is_integration_park(
        reason="integration parked: merge conflict/failure (aborted): foo.py",
        block_kind=event_kind,
    )
