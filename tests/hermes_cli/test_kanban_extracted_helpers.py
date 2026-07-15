from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from hermes_cli import kanban_db
from hermes_cli import kanban_context
from hermes_cli import kanban_dispatch_policy
from hermes_cli import kanban_worker_runtime


@dataclass
class _TaskLike:
    continuation_count: int = 0
    assignee: str = "coder"
    kind: str = "task"


@dataclass
class _CommentLike:
    body: str
    author: str = "worker"
    created_at: int = 0
    kind: str = "comment"


def test_context_profile_selects_retry_for_continuations():
    task = _TaskLike(continuation_count=1, assignee="coder")

    assert kanban_context.context_profile_for_task(task, "full") == "retry"
    assert kanban_context.context_profile_for_task(task, "worker_slim") == "retry"
    assert kanban_context.context_profile_for_task(task, "reviewer_review") == "reviewer_review"


def test_context_profile_selects_reviewer_body_cap_for_initial_review():
    task = _TaskLike(assignee="reviewer", kind="review")

    profile = kanban_context.context_profile_for_task(task, "full")

    assert profile == "reviewer_review"
    assert kanban_context.context_caps(profile)["body_bytes"] == 32 * 1024


def test_render_comment_thread_keeps_directives_first_and_caps_regular_comments():
    comments = [
        _CommentLike("regular 1", author="coder", created_at=60),
        _CommentLike("operator says x", author="operator", created_at=0, kind="directive"),
        _CommentLike("regular 2", author="coder", created_at=120),
    ]

    rendered = kanban_context.render_comment_thread(
        comments,
        max_comments=1,
        comment_bytes=6,
    )

    assert rendered[0].startswith("## ⚠️ OPERATOR DIRECTIVE")
    assert "operator says x" not in rendered
    assert "operat… [truncated" in "\n".join(rendered)
    assert "showing most recent 1" in "\n".join(rendered)
    assert "regular 1" not in "\n".join(rendered)
    assert "regula… [truncated" in "\n".join(rendered)


def test_dispatch_positive_guards_reject_bools_and_non_positive_values():
    assert kanban_dispatch_policy.positive_int(True) is None
    assert kanban_dispatch_policy.positive_int(0) is None
    assert kanban_dispatch_policy.positive_int(3) == 3
    assert kanban_dispatch_policy.positive_number(False) is None
    assert kanban_dispatch_policy.positive_number(-1.0) is None
    assert kanban_dispatch_policy.positive_number(2.5) == 2.5


def test_worker_env_allowlist_can_be_injected_for_compatibility():
    parent = {
        "PATH": "/usr/bin",
        "HERMES_HOME": "/tmp/hermes",
        "CUSTOM_ALLOWED": "yes",
        "OPENROUTER_API_KEY": "lane",
        "SECRET": "drop",
    }

    env = kanban_worker_runtime.build_worker_env(
        parent,
        passthrough=frozenset({"PATH", "CUSTOM_ALLOWED"}),
        lane_provider_keys=frozenset(),
        prefixes=(),
    )

    assert env == {"PATH": "/usr/bin", "CUSTOM_ALLOWED": "yes"}


def test_claude_verdict_read_only_profiles_are_injected():
    lane = {"worker_runtime": "claude-cli"}

    assert kanban_worker_runtime.is_claude_verdict_read_only_lane(
        "audit", lane, read_only_profiles={"audit"},
    )
    assert not kanban_worker_runtime.is_claude_verdict_read_only_lane(
        "reviewer", lane, read_only_profiles={"audit"},
    )


def test_worker_process_launch_has_one_lifecycle_owner():
    """Hermes and Claude policies may build argv, but only one owner spawns."""
    tree = ast.parse(Path(kanban_db.__file__).read_text(encoding="utf-8"))
    owners: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "subprocess"
                and child.func.attr == "Popen"
            ):
                owners.append(node.name)

    assert owners == ["_launch_worker_process"]
