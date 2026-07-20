"""Anti-placebo tests for the CLI ``/reload-mcp`` connect-before-disconnect path.

The interactive CLI reload used to be *disconnect-first*: it called
``shutdown_mcp_servers()`` (tearing every live MCP session down) and only then
``discover_mcp_tools()`` to reconnect. That opened a tool-less downtime window
and — if reconnect failed — permanently dropped the tools with no rollback.

``HermesCLI._reload_mcp`` now routes through the same transactional
(shadow-topology) helper the gateway uses,
``reload_mcp_tools_transactionally``: it stages + validates a candidate
topology and only publishes atomically. On ``MCPReloadError`` the live servers
survive untouched, so the CLI must NOT refresh the agent or inject a change
note — the old tools stay callable.

These tests pin both invariants:
  * the disconnect-first primitives are never called (regression guard), and
  * a failed reload leaves the session state (agent + history) untouched.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import tools.mcp_tool as mcp_tool


def _make_cli(*, agent=None):
    """Minimal HermesCLI with just the attributes ``_reload_mcp`` touches."""
    import cli as cli_mod

    obj = object.__new__(cli_mod.HermesCLI)
    obj._command_running = False
    obj.agent = agent
    obj.conversation_history = []
    obj.enabled_toolsets = ["all"]
    return obj


def test_reload_uses_transactional_and_not_disconnect_first():
    """Success path: the CLI publishes via ``reload_mcp_tools_transactionally``
    and never calls the disconnect-first primitives — no downtime window."""
    cli = _make_cli(agent=None)

    published = {"vault": MagicMock(_registered_tool_names=["mcp__vault__search"])}

    with patch.object(
        mcp_tool, "reload_mcp_tools_transactionally",
        return_value=["mcp__vault__search"],
    ) as m_txn, \
        patch.object(mcp_tool, "shutdown_mcp_servers") as m_shutdown, \
        patch.object(mcp_tool, "discover_mcp_tools") as m_discover, \
        patch.dict(mcp_tool._servers, published, clear=True):
        cli._reload_mcp()

    # Connect-before-disconnect: the transactional publish ran exactly once...
    m_txn.assert_called_once_with()
    # ...and the disconnect-first primitives were eliminated.
    m_shutdown.assert_not_called()
    m_discover.assert_not_called()

    # The model is told the toolset changed (appended after existing history).
    assert cli.conversation_history, "a reload note should be appended on success"
    note = cli.conversation_history[-1]
    assert note["role"] == "user"
    assert "MCP servers have been reloaded" in note["content"]


def test_reload_failure_preserves_live_tools_and_session():
    """When the candidate topology fails validation the transactional helper
    raises ``MCPReloadError``; the live servers are untouched, so the CLI must
    NOT refresh the agent and must NOT inject a change note — the old tools
    stay callable."""
    agent = MagicMock()
    agent.tools = ["mcp__vault__search"]
    cli = _make_cli(agent=agent)

    err = mcp_tool.MCPReloadError(RuntimeError("candidate auth failed"))
    live = {"vault": MagicMock(_registered_tool_names=["mcp__vault__search"])}

    with patch.object(
        mcp_tool, "reload_mcp_tools_transactionally", side_effect=err,
    ), \
        patch.object(mcp_tool, "shutdown_mcp_servers") as m_shutdown, \
        patch.object(mcp_tool, "refresh_agent_mcp_tools") as m_refresh, \
        patch.dict(mcp_tool._servers, live, clear=True):
        # Must not raise: the CLI swallows MCPReloadError into a printed message.
        cli._reload_mcp()

    # No teardown was attempted (fail-closed connect-before-disconnect)...
    m_shutdown.assert_not_called()
    # ...the agent toolset was NOT rebuilt (nothing changed)...
    m_refresh.assert_not_called()
    agent._persist_session.assert_not_called()
    # ...and no phantom "tools reloaded" note poisons the transcript.
    assert cli.conversation_history == []
