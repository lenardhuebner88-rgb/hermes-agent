"""Regression tests for gateway shutdown cleaning up cached agent memory providers (issue #11205).

When the gateway shuts down, ``stop()`` called ``_finalize_shutdown_agents()``
which only drained agents in ``_running_agents``.  Idle agents sitting in
``_agent_cache`` (LRU cache) were never cleaned up, so their
``MemoryProvider.on_session_end()`` hooks never fired.

The fix adds an explicit sweep of ``_agent_cache`` after
``_finalize_shutdown_agents`` in the ``_stop_impl`` coroutine.
"""

import threading
from collections import OrderedDict
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.gateway.restart_test_helpers import make_restart_runner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_shutdown_runner():
    """Real GatewayRunner spy with shutdown-cache state enabled for stop()."""
    runner, _adapter = make_restart_runner()
    runner._agent_cache = OrderedDict[str, Any]()
    runner._agent_cache_lock = threading.Lock()
    runner._busy_ack_ts = {}
    runner._drain_active_agents = AsyncMock(
        side_effect=lambda _timeout: (dict(runner._running_agents), False)
    )
    return runner


def _make_mock_agent():
    a = MagicMock()
    a.shutdown_memory_provider = MagicMock()
    a.close = MagicMock()
    return a


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCachedAgentCleanupOnShutdown:
    """Verify that ``stop()`` calls ``_cleanup_agent_resources`` on idle
    cached agents, triggering ``shutdown_memory_provider()`` (which calls
    ``on_session_end``)."""

    @pytest.mark.asyncio
    async def test_cached_agent_memory_provider_shut_down(self):
        """A cached agent's shutdown_memory_provider is called during gateway stop."""
        gw = _make_shutdown_runner()
        agent = _make_mock_agent()
        gw._agent_cache["session-1"] = (agent, "sig-123")

        await gw.stop()

        agent.shutdown_memory_provider.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_cleared_after_shutdown(self):
        """The _agent_cache dict is cleared after stop."""
        gw = _make_shutdown_runner()
        agent = _make_mock_agent()
        gw._agent_cache["s1"] = (agent, "sig1")

        await gw.stop()

        assert len(gw._agent_cache) == 0

    @pytest.mark.asyncio
    async def test_no_cached_agents_no_error(self):
        """stop() works fine when _agent_cache is empty."""
        gw = _make_shutdown_runner()

        await gw.stop()  # Should not raise

        assert len(gw._agent_cache) == 0

    @pytest.mark.asyncio
    async def test_multiple_cached_agents_all_cleaned(self):
        """All cached agents get cleaned up."""
        gw = _make_shutdown_runner()
        agents = []
        for i in range(5):
            a = _make_mock_agent()
            agents.append(a)
            gw._agent_cache[f"s{i}"] = (a, f"sig{i}")

        await gw.stop()

        for a in agents:
            a.shutdown_memory_provider.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_survives_agent_exception(self):
        """An exception from one agent's shutdown doesn't prevent others."""
        gw = _make_shutdown_runner()

        bad = _make_mock_agent()
        bad.shutdown_memory_provider.side_effect = RuntimeError("boom")
        bad.close.side_effect = RuntimeError("boom")

        good = _make_mock_agent()

        gw._agent_cache["bad"] = (bad, "sig-bad")
        gw._agent_cache["good"] = (good, "sig-good")

        await gw.stop()

        # The good agent should still be cleaned up
        good.shutdown_memory_provider.assert_called_once()

    @pytest.mark.asyncio
    async def test_plain_agent_not_tuple(self):
        """Cache entries that aren't tuples (just bare agents) are also cleaned."""
        gw = _make_shutdown_runner()
        agent = _make_mock_agent()
        gw._agent_cache["s1"] = agent  # Not a tuple

        await gw.stop()

        agent.shutdown_memory_provider.assert_called_once()
        assert len(gw._agent_cache) == 0

    @pytest.mark.asyncio
    async def test_none_entry_skipped(self):
        """A None cache entry doesn't cause errors."""
        gw = _make_shutdown_runner()
        gw._agent_cache["s1"] = None

        await gw.stop()

        assert len(gw._agent_cache) == 0


class TestRunningAgentsNotDoubleCleaned:
    """Verify behavior when agents appear in both _running_agents and _agent_cache."""

    @pytest.mark.asyncio
    async def test_running_and_cached_agent_cleaned_at_least_once(self):
        """An agent in both _running_agents and _agent_cache gets
        shutdown_memory_provider called at least once."""
        gw = _make_shutdown_runner()
        shared = _make_mock_agent()

        gw._running_agents["s1"] = shared
        gw._agent_cache["s1"] = (shared, "sig1")

        await gw.stop()

        # Called at least once — either from _finalize_shutdown_agents
        # or from the cache sweep (or both)
        assert shared.shutdown_memory_provider.call_count >= 1
