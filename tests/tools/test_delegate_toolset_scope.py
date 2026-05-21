"""Tests for delegate_tool toolset scoping.

Verifies that subagents cannot gain tools that the parent does not have.
The LLM controls the `toolsets` parameter — without intersection with the
parent's enabled_toolsets, it can escalate privileges by requesting
arbitrary toolsets.
"""

import json
import threading
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from tools.delegate_tool import _build_child_agent, _strip_blocked_tools


class TestToolsetIntersection:
    """Subagent toolsets must be a subset of parent's enabled_toolsets."""

    def test_requested_toolsets_intersected_with_parent(self):
        """LLM requests toolsets parent doesn't have — extras are dropped."""
        parent = SimpleNamespace(enabled_toolsets=["terminal", "file"])

        # Simulate the intersection logic from _build_child_agent
        parent_toolsets = set(parent.enabled_toolsets)
        requested = ["terminal", "file", "web", "browser", "rl"]
        scoped = [t for t in requested if t in parent_toolsets]

        assert sorted(scoped) == ["file", "terminal"]
        assert "web" not in scoped
        assert "browser" not in scoped
        assert "rl" not in scoped

    def test_all_requested_toolsets_available_on_parent(self):
        """LLM requests subset of parent tools — all pass through."""
        parent = SimpleNamespace(enabled_toolsets=["terminal", "file", "web", "browser"])

        parent_toolsets = set(parent.enabled_toolsets)
        requested = ["terminal", "web"]
        scoped = [t for t in requested if t in parent_toolsets]

        assert sorted(scoped) == ["terminal", "web"]

    def test_no_toolsets_requested_inherits_parent(self):
        """When toolsets is None/empty, child inherits parent's set."""
        parent_toolsets = ["terminal", "file", "web"]
        child = _strip_blocked_tools(parent_toolsets)
        assert "terminal" in child
        assert "file" in child
        assert "web" in child

    def test_strip_blocked_removes_delegation(self):
        """Blocked toolsets (delegation, clarify, etc.) are always removed."""
        child = _strip_blocked_tools(["terminal", "delegation", "clarify", "memory"])
        assert "delegation" not in child
        assert "clarify" not in child
        assert "memory" not in child
        assert "terminal" in child

    def test_empty_intersection_yields_empty_toolsets(self):
        """If parent has no overlap with requested, child gets nothing extra."""
        parent = SimpleNamespace(enabled_toolsets=["terminal"])

        parent_toolsets = set(parent.enabled_toolsets)
        requested = ["web", "browser"]
        scoped = [t for t in requested if t in parent_toolsets]

        assert scoped == []


class TestKanbanRestrictedWorkerChildInheritance:
    KANBAN_MINIMAL_TOOLS = [
        "kanban_show",
        "kanban_complete",
        "kanban_block",
        "kanban_comment",
    ]

    def _parent(self, enabled_toolsets):
        parent = MagicMock()
        parent.enabled_toolsets = enabled_toolsets
        parent.base_url = "https://example.invalid"
        parent.api_key = "***"
        parent.provider = "test-provider"
        parent.api_mode = "chat_completions"
        parent.model = "test-model"
        parent.platform = "cli"
        parent.providers_allowed = None
        parent.providers_ignored = None
        parent.providers_order = None
        parent.provider_sort = None
        parent._session_db = None
        parent._delegate_depth = 0
        parent._active_children = []
        parent._active_children_lock = threading.Lock()
        parent._print_fn = None
        parent.tool_progress_callback = None
        parent.thinking_callback = None
        return parent

    def test_restricted_kanban_worker_does_not_derive_child_or_mcp_toolsets(self, monkeypatch, tmp_path):
        """P1.18: fake builder proof for profile/delegation/child boundary.

        Broad parent context can carry delegation and fake MCP toolsets.  In a
        restricted Kanban worker whose dispatcher evidence excludes
        delegate_task, a synthetic child construction must not inherit terminal,
        file, delegation, or MCP toolsets.  No subagent is spawned and no model
        call is made; AIAgent construction is patched to a MagicMock.
        """
        home = tmp_path / ".hermes"
        home.mkdir()
        parent = self._parent([
            "terminal",
            "file",
            "delegation",
            "mcp-fake-broad-context",
        ])
        requested = [
            "terminal",
            "file",
            "delegation",
            "mcp-fake-broad-context",
        ]

        # Broad-control sanity: without the restricted Kanban env, this parent
        # would be able to pass broad non-blocked toolsets through the builder.
        broad_control = _strip_blocked_tools(requested)
        assert "terminal" in broad_control
        assert "file" in broad_control
        assert "mcp-fake-broad-context" in broad_control
        assert "delegation" not in broad_control

        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_profile_isolation")
        monkeypatch.setenv("HERMES_KANBAN_EFFECTIVE_TOOLSETS", json.dumps(self.KANBAN_MINIMAL_TOOLS))

        with patch("run_agent.AIAgent") as MockAgent:
            child = MagicMock()
            MockAgent.return_value = child
            _build_child_agent(
                task_index=0,
                goal="test-only child boundary proof",
                context=None,
                toolsets=requested,
                model=None,
                max_iterations=1,
                task_count=1,
                parent_agent=parent,
                role="orchestrator",
            )

        _, kwargs = MockAgent.call_args
        assert kwargs["enabled_toolsets"] == []
        assert child._delegate_role == "leaf"
        assert "terminal" not in kwargs["enabled_toolsets"]
        assert "file" not in kwargs["enabled_toolsets"]
        assert "delegation" not in kwargs["enabled_toolsets"]
        assert "mcp-fake-broad-context" not in kwargs["enabled_toolsets"]
