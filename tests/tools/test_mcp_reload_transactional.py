"""Anti-placebo tests for the transactional (shadow) MCP reload.

``reload_mcp_tools_transactionally`` implements connect-before-disconnect:
it builds a candidate topology in an isolated temp namespace, validates each
candidate (initialize + discover + a NON-mutating smoke invoke), and only
then atomically publishes it under the registry lock. A candidate failure
must leave the CURRENT live servers, their registrations, and their sessions
completely untouched — the old tools stay callable, the old server task is
never shut down, and no partial swap escapes.

These tests fake the MCP loop / connect / registry so they exercise the real
transactional control flow (staging, cleanup, publish, rollback) rather than
re-asserting a reconnect (which would be a placebo).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import tools.mcp_tool as mcp_tool
from tools.mcp_tool import MCPReloadError, reload_mcp_tools_transactionally
from tools.registry import ToolRegistry


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

def _readonly_tool(name: str):
    """A read-only, zero-required-argument tool → eligible for smoke invoke."""
    return SimpleNamespace(
        name=name,
        description="",
        annotations=SimpleNamespace(readOnlyHint=True),
        inputSchema={},
    )


class _FakeSession:
    def __init__(self, *, invoke_error: BaseException | None = None):
        self._invoke_error = invoke_error
        self.ping_calls = 0
        self.invoke_calls = 0

    async def send_ping(self):
        self.ping_calls += 1
        return MagicMock()

    async def call_tool(self, name, arguments=None):
        self.invoke_calls += 1
        if self._invoke_error is not None:
            raise self._invoke_error
        return SimpleNamespace(isError=False, content=[])


class _FakeServer:
    """Duck-typed MCPServerTask stand-in for reload staging/publish."""

    def __init__(
        self,
        name: str,
        *,
        tools=None,
        session: _FakeSession | None = None,
        registered=None,
        shutdown_error: BaseException | None = None,
    ):
        self.name = name
        self._tools = list(tools or [])
        self.session = session if session is not None else _FakeSession()
        self.tool_timeout = 30.0
        self._registered_tool_names = list(registered or [])
        self._shutdown_error = shutdown_error
        self.shutdown_calls = 0
        self.deregister_calls = 0

    async def shutdown(self):
        self.shutdown_calls += 1
        if self._shutdown_error is not None:
            raise self._shutdown_error
        self.session = None

    def _deregister_tools(self):
        self.deregister_calls += 1
        self._registered_tool_names = []


def _sync_run_on_mcp_loop(coro_or_factory, timeout=None):
    """Run staged coroutines synchronously on a throwaway loop (no bg thread)."""
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _patchers(*, config, connect):
    """Common patch set: MCP available, identity config, sync loop, fake connect."""
    return [
        patch.object(mcp_tool, "_MCP_AVAILABLE", True),
        patch.object(mcp_tool, "_load_mcp_config", return_value=config),
        patch.object(mcp_tool, "_filter_suspicious_mcp_servers", side_effect=lambda s: s),
        patch.object(mcp_tool, "_ensure_mcp_loop", MagicMock()),
        patch.object(mcp_tool, "_run_on_mcp_loop", side_effect=_sync_run_on_mcp_loop),
        patch.object(mcp_tool, "_connect_server", side_effect=connect),
    ]


class _Patched:
    def __init__(self, patchers):
        self._patchers = patchers

    def __enter__(self):
        for p in self._patchers:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patchers):
            p.stop()
        return False


# --------------------------------------------------------------------------- #
# Candidate-failure invariants (the anti-placebo core)
# --------------------------------------------------------------------------- #

def test_candidate_invoke_failure_leaves_old_server_live():
    """Candidate discovers OK but its smoke INVOKE fails → reload aborts,
    the live server object is untouched (never shut down, registration kept),
    and the failed candidate is cleaned up."""
    old = _FakeServer("vault", registered=["mcp__vault__search"])
    cand_session = _FakeSession(invoke_error=RuntimeError("candidate auth failed"))
    candidate = _FakeServer(
        "vault",
        tools=[_readonly_tool("search")],
        session=cand_session,
    )

    async def _connect(shadow_name, cfg):
        return candidate

    config = {"vault": {"command": "x"}}

    with _Patched(_patchers(config=config, connect=_connect)):
        with patch.dict(mcp_tool._servers, {"vault": old}, clear=True):
            with pytest.raises(MCPReloadError) as ei:
                reload_mcp_tools_transactionally()

            # Old server object identity + state fully preserved.
            assert mcp_tool._servers["vault"] is old
            assert old.shutdown_calls == 0
            assert old.deregister_calls == 0
            assert old._registered_tool_names == ["mcp__vault__search"]

    # The failure is the invoke error, not a reconnect placebo.
    assert "candidate auth failed" in str(ei.value)
    assert cand_session.invoke_calls == 1
    # Candidate session was closed during cleanup.
    assert candidate.shutdown_calls == 1


def test_multi_server_one_failure_keeps_existing_usable():
    """Two configured servers; the second candidate fails. The reload is
    fail-closed: nothing is published, so the healthy existing server stays
    live, and every staged candidate is cleaned up."""
    old_vault = _FakeServer("vault", registered=["mcp__vault__search"])

    good = _FakeServer("vault", tools=[], session=_FakeSession())
    broken = _FakeServer(
        "broken",
        tools=[_readonly_tool("probe")],
        session=_FakeSession(invoke_error=RuntimeError("broken server down")),
    )

    async def _connect(shadow_name, cfg):
        return good if cfg["_id"] == "vault" else broken

    config = {
        "vault": {"command": "v", "_id": "vault"},
        "broken": {"command": "b", "_id": "broken"},
    }

    with _Patched(_patchers(config=config, connect=_connect)):
        with patch.dict(mcp_tool._servers, {"vault": old_vault}, clear=True):
            with pytest.raises(MCPReloadError) as ei:
                reload_mcp_tools_transactionally()

            # Existing healthy server untouched → still usable.
            assert mcp_tool._servers["vault"] is old_vault
            assert old_vault.shutdown_calls == 0
            assert old_vault._registered_tool_names == ["mcp__vault__search"]

    assert "broken server down" in str(ei.value)
    # Both staged candidates were cleaned up (the good one too — fail-closed).
    assert good.shutdown_calls == 1
    assert broken.shutdown_calls == 1


def test_candidate_cleanup_error_is_reported_not_swallowed():
    """When a candidate fails AND its cleanup also fails, the error surfaces
    with both the candidate error and a redacted cleanup error — never a
    false success."""
    candidate = _FakeServer(
        "vault",
        tools=[_readonly_tool("search")],
        session=_FakeSession(invoke_error=RuntimeError("primary auth failed")),
        shutdown_error=RuntimeError("could not close transport"),
    )

    async def _connect(shadow_name, cfg):
        return candidate

    config = {"vault": {"command": "x"}}

    with _Patched(_patchers(config=config, connect=_connect)):
        with patch.dict(mcp_tool._servers, {}, clear=True):
            with pytest.raises(MCPReloadError) as ei:
                reload_mcp_tools_transactionally()

    err = ei.value
    assert "primary auth failed" in err.candidate_error
    assert err.cleanup_errors, "cleanup failure must be recorded, not swallowed"
    assert any("could not close transport" in c for c in err.cleanup_errors)
    assert "candidate cleanup also failed" in str(err)


# --------------------------------------------------------------------------- #
# Success path: publish, then retire the old topology
# --------------------------------------------------------------------------- #

def test_success_publishes_candidate_and_retires_old_after():
    """On success the candidate replaces the live server, the old server is
    deregistered and shut down AFTER publication, and the new registered tool
    names are returned. Order matters: the old shutdown must not strip the
    freshly published handlers (its registered-name list is emptied first)."""
    old = _FakeServer("vault", registered=["mcp__vault__search"])
    candidate = _FakeServer("vault", tools=[], session=_FakeSession())

    async def _connect(shadow_name, cfg):
        return candidate

    config = {"vault": {"command": "x"}}

    def _fake_register(name, server, cfg):
        # Simulate the real registration side effect the old shutdown must
        # not undo: the returned names become the live tool set.
        return ["mcp__vault__search"]

    fresh_registry = ToolRegistry()

    patchers = _patchers(config=config, connect=_connect)
    patchers.append(patch.object(mcp_tool, "_register_server_tools", side_effect=_fake_register))

    with _Patched(patchers):
        with patch("tools.registry.registry", fresh_registry):
            with patch.dict(mcp_tool._servers, {"vault": old}, clear=True):
                result = reload_mcp_tools_transactionally()

                # Candidate is now live under the real name.
                assert mcp_tool._servers["vault"] is candidate
                assert candidate.name == "vault"
                assert candidate._registered_tool_names == ["mcp__vault__search"]

    assert result == ["mcp__vault__search"]
    # Old topology retired only after publication.
    assert old.deregister_calls == 1
    assert old.shutdown_calls == 1
    # Old deregister emptied its own name list, so its shutdown could not have
    # stripped the newly published handlers.
    assert old._registered_tool_names == []
