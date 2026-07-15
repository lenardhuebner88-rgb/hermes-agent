"""Regression tests for auto-decompose LLM error classification."""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb


@pytest.mark.parametrize(
    "reason",
    [
        "LLM error: BadRequestError",
        "LLM error: AuthenticationError",
        "LLM error: PermissionDeniedError",
        "LLM error: NotFoundError",
        "LLM error: UnprocessableEntityError",
    ],
)
def test_deterministic_llm_client_error_is_not_transient(reason):
    assert kb._decompose_failure_is_transient(reason) is False


@pytest.mark.parametrize(
    "reason",
    [
        "LLM error: APITimeoutError",
        "LLM error: RateLimitError",
        "LLM error: APIConnectionError",
        "LLM error: InternalServerError",
        "auxiliary client unavailable",
        "no auxiliary client configured",
        "llm returned malformed json",
        "db error",
    ],
)
def test_decompose_infrastructure_error_remains_transient(reason):
    assert kb._decompose_failure_is_transient(reason) is True


@pytest.mark.parametrize("reason", [None, "", "decomposer rejected task spec"])
def test_missing_or_unknown_decompose_error_is_not_transient(reason):
    assert kb._decompose_failure_is_transient(reason) is False
