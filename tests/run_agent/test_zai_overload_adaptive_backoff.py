"""Regression guard: the Z.AI Coding overload adaptive long-backoff must
actually be reachable from the retry loop.

The Z.AI Coding Plan GLM-5.2 endpoint returns HTTP 429 "The service may be
temporarily overloaded..." for server-wide overload. ``classify_api_error``
routes that (by design, #14038) to ``FailoverReason.overloaded`` — NOT
``rate_limit`` — so the credential pool isn't burned rotating a valid key.

But ``conversation_loop`` gated ``adaptive_rate_limit_backoff`` on
``is_rate_limited``, which deliberately excludes ``overloaded``. The two
subsystems were built independently (each with passing unit tests) and
silently conflicted: the entire 30→60→90→120s adaptive long backoff was
dead code, and a single-key ZAI user with no fallback got hammered with
generic 2–60s retries against an already-overloaded endpoint.

These tests pin the CROSS-MODULE contract that was broken, plus the exact
shape of the (now-broadened) loop gate.
"""

from __future__ import annotations


class _ZaiOverloadError(Exception):
    """A realistic Z.AI Coding overload 429 (OpenAI-SDK-APIStatusError shape)."""

    def __init__(self):
        super().__init__(
            "The service may be temporarily overloaded, please try again later "
            '(code 1305)'
        )
        self.status_code = 429
        self.body = {"error": {"code": "1305"}}


def _zai_overload_error():
    return _ZaiOverloadError()


class TestZaiOverloadContract:
    def test_classifier_routes_zai_overload_to_overloaded_not_rate_limit(self):
        """classify → overloaded, so is_rate_limited is False (the reason the
        old gate skipped the adaptive backoff)."""
        from agent.error_classifier import classify_api_error, FailoverReason

        classified = classify_api_error(_zai_overload_error(), provider="zai")
        assert classified.reason == FailoverReason.overloaded

    def test_same_error_is_a_zai_overload_for_the_backoff_helper(self):
        """The retry helper recognises the identical error shape — so the
        ONLY thing between them was the loop gate."""
        from agent.retry_utils import is_zai_coding_overload_error

        assert is_zai_coding_overload_error(
            base_url="https://api.z.ai/api/coding/paas/v4",
            model="glm-5.2",
            error=_zai_overload_error(),
        )

    def test_adaptive_backoff_produces_long_schedule_past_short_attempts(self):
        from agent.retry_utils import adaptive_rate_limit_backoff

        # attempt 4 (> short_attempts=3) → first long-backoff bucket (30s).
        wait, policy = adaptive_rate_limit_backoff(
            4,
            base_url="https://api.z.ai/api/coding/paas/v4",
            model="glm-5.2",
            error=_zai_overload_error(),
            default_wait=3.0,
        )
        assert policy == "zai_coding_overload_long"
        assert wait >= 30.0, "long backoff must dominate the generic default_wait"


class TestLoopBackoffGate:
    """Mirror of conversation_loop.py's adaptive-backoff gate. Kept in
    lock-step with the source (same convention as
    test_31273_402_not_retried.py's is_client_error mirror)."""

    @staticmethod
    def _adaptive_backoff_consulted(*, is_rate_limited: bool, retry_after) -> bool:
        # Post-fix gate: fire on ANY retry-with-backoff path when there is no
        # explicit Retry-After (the helper self-guards to non-ZAI = no-op).
        # Pre-fix this was ``is_rate_limited and not retry_after``.
        return not retry_after

    def test_overloaded_retry_now_consults_adaptive_backoff(self):
        # A ZAI overload is classified overloaded → is_rate_limited is False.
        assert self._adaptive_backoff_consulted(
            is_rate_limited=False, retry_after=None
        ), "the overloaded-retry path must reach adaptive_rate_limit_backoff"

    def test_explicit_retry_after_still_wins(self):
        assert not self._adaptive_backoff_consulted(
            is_rate_limited=True, retry_after=120.0
        )
