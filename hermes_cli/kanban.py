"""CLI for the Hermes Kanban board — ``hermes kanban …`` subcommand.

Exposes the full Kanban command surface documented in the design spec
(``docs/hermes-kanban-v1-spec.pdf``).  All DB work is delegated to
``kanban_db``.  This module owns:

  * Argparse subcommand construction (``build_parser``).
  * Argument dispatch (``kanban_command`` + ``_KANBAN_ACTION_HANDLERS``).
  * Output formatting (plain text + ``--json``).
  * Slash-style entry (``run_slash``) used by interactive CLI and gateway.

Module layout (top → bottom; section banners mark each region):

  1. Small formatting / shared helpers (``_fmt_*``, ``_emit_json``, …)
  2. Parser registration helpers (``_register_*_parsers``) + ``build_parser``
  3. Command dispatch (``kanban_command``)
  4. Handlers — boards, then task CRUD / lifecycle / dispatch / notify /
     triage / epic / release / gc (``_cmd_*``, ``_dispatch_*``)
  5. Slash-command entry (``run_slash``) + module-level handler table
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli.goals import check_goal_mode_completion
from hermes_cli.kanban_decompose import _VALID_TASK_KINDS
from hermes_cli import kanban_swarm as ks
from hermes_cli.profiles import get_active_profile_name
from hermes_cli.scoped_auto_commit import create_scoped_local_commit

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    "todo":     "◻",
    "ready":    "▶",
    "running":  "●",
    "scheduled":"⏱",
    "blocked":  "⊘",
    "done":     "✓",
    "archived": "—",
}

_SCHEDULE_DUE_DURATION_RE = re.compile(r"\+(\d+)([smh])$")


def _parse_schedule_due(value: str) -> int:
    """Parse schedule deadlines into Unix epoch seconds."""
    if value.isdigit():
        return int(value)

    duration = _SCHEDULE_DUE_DURATION_RE.fullmatch(value)
    if duration:
        amount = int(duration.group(1))
        multiplier = {"s": 1, "m": 60, "h": 3600}[duration.group(2)]
        return int(time.time()) + amount * multiplier

    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is not None and parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return int(parsed.timestamp())

    raise argparse.ArgumentTypeError(
        "--due must be Unix epoch seconds, an ISO-8601 timestamp with offset, "
        "or a relative duration such as +2h, +45m, or +90s"
    )


def _parse_wait_for(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("--wait-for must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("--wait-for must be a JSON object")
    return parsed

def _coerce_config_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return default

def _fmt_ts(ts: Optional[int]) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

def _fmt_task_line(t: kb.Task) -> str:
    icon = _STATUS_ICONS.get(t.status, "?")
    assignee = t.assignee or "(unassigned)"
    tenant = f" [{t.tenant}]" if t.tenant else ""
    return f"{icon} {t.id}  {t.status:8s}  {assignee:20s}{tenant}  {t.title}"

def _task_to_dict(t: kb.Task) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title,
        "body": t.body,
        "assignee": t.assignee,
        "status": t.status,
        "priority": t.priority,
        "tenant": t.tenant,
        "workspace_kind": t.workspace_kind,
        "workspace_path": t.workspace_path,
        "branch_name": t.branch_name,
        "project_id": t.project_id,
        "created_by": t.created_by,
        "created_at": t.created_at,
        "due_at": t.due_at,
        "wait_for": t.wait_for,
        "started_at": t.started_at,
        "completed_at": t.completed_at,
        "result": t.result,
        "skills": list(t.skills) if t.skills else [],
        "max_retries": t.max_retries,
        "max_iterations": t.max_iterations,
        "continuation_count": t.continuation_count,
        "max_continuations": t.max_continuations,
        "last_continuation_reason": t.last_continuation_reason,
        "session_id": t.session_id,
        "workflow_template_id": t.workflow_template_id,
        "current_step_key": t.current_step_key,
        "epic_id": t.epic_id,
        "kind": t.kind,
        **kb.task_detail_projection(t),
    }


def _emit_json(obj: Any) -> None:
    """Pretty-print ``obj`` as JSON on stdout (indent=2, ensure_ascii=False)."""
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _kanban_err(msg: Any) -> None:
    """Print a ``kanban: …`` error line on stderr."""
    print(f"kanban: {msg}", file=sys.stderr)


def _run_state_kwargs(args: argparse.Namespace) -> Optional[dict[str, str]]:
    st = getattr(args, "state_type", None)
    sn = getattr(args, "state_name", None)
    if (st is None) != (sn is None):
        return None
    if st is None:
        return {}
    return {"state_type": st, "state_name": sn}

def _parse_workspace_flag(value: str) -> tuple[str, Optional[str]]:
    """Parse ``--workspace`` into ``(kind, path|None)``.

    Accepts: ``scratch``, ``worktree``, ``worktree:<path>``, ``dir:<path>``.
    """
    if not value:
        return ("scratch", None)
    v = value.strip()
    if v in {"scratch", "worktree"}:
        return (v, None)
    for prefix, kind in (("dir:", "dir"), ("worktree:", "worktree")):
        if not v.startswith(prefix):
            continue
        path = v[len(prefix):].strip()
        if not path:
            raise argparse.ArgumentTypeError(
                f"--workspace {prefix} requires a path after the colon"
            )
        return (kind, os.path.expanduser(path))
    raise argparse.ArgumentTypeError(
        f"unknown --workspace value {value!r}: use scratch, worktree, "
        "worktree:<path>, or dir:<path>"
    )

def _parse_branch_flag(value: Optional[str]) -> Optional[str]:
    """Normalize an optional branch name from ``kanban create --branch``."""
    if value is None:
        return None
    branch = value.strip()
    if not branch:
        raise argparse.ArgumentTypeError("--branch requires a non-empty name")
    if branch.startswith("-"):
        raise argparse.ArgumentTypeError("--branch must not start with '-'")
    if any(ch.isspace() for ch in branch):
        raise argparse.ArgumentTypeError("--branch must not contain whitespace")
    return branch

def _check_dispatcher_presence() -> tuple[bool, str]:
    """Return ``(running, message)``.

    - ``running=True``: a gateway is alive for this HERMES_HOME and its
      config has ``kanban.dispatch_in_gateway`` on (default). Message
      is a short status line.
    - ``running=False``: either no gateway is running, or the gateway
      is running but the config flag is off. Message is human guidance
      explaining the next step.

    Used by ``hermes kanban create`` (and callers) to warn when a task
    will sit in ``ready`` because nothing is there to pick it up.
    Defensive against import failures and config-read errors — if the
    probe itself errors, we return ``(True, "")`` so we don't spam
    false warnings (better to miss a warning than to cry wolf).
    """
    try:
        from gateway.status import get_running_pid  # type: ignore
    except Exception:
        return (True, "")  # can't probe — silent
    try:
        pid = get_running_pid()
    except Exception:
        return (True, "")  # probe errored — silent

    # Even if the gateway is up, dispatch_in_gateway may be off.
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        dispatch_on = _coerce_config_bool(
            cfg.get("kanban", {}).get("dispatch_in_gateway", True), default=True
        )
    except Exception:
        dispatch_on = True  # can't tell — assume default

    if pid and dispatch_on:
        return (True, f"gateway pid={pid}, dispatch enabled")
    if pid and not dispatch_on:
        return (
            False,
            "Gateway is running but kanban.dispatch_in_gateway=false in "
            "config.yaml — the task will sit in 'ready' until you flip it "
            "back on and restart the gateway, OR run the legacy "
            "standalone daemon (`hermes kanban daemon --force`)."
        )
    return (
        False,
        "No gateway is running — the task will sit in 'ready' until you "
        "start it. Run:\n"
        "    hermes gateway start\n"
        "The gateway hosts an embedded dispatcher (tick interval 60s by "
        "default); your task will be picked up on the next tick after "
        "the gateway comes up."
    )

# ---------------------------------------------------------------------------
# Argparse builder
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Parser registration helpers (extracted from build_parser for readability).
# Registration order is load-bearing for help-text grouping; do not reorder
# calls in build_parser without an intentional UX change.
# ---------------------------------------------------------------------------

def _register_init_parsers(sub: argparse._SubParsersAction) -> None:
    """Register the ``init`` subcommand."""
    # --- init ---
    sub.add_parser("init", help="Create kanban.db if missing (idempotent)")

def _register_boards_parsers(sub: argparse._SubParsersAction) -> None:
    """Register ``boards`` and its nested subcommands."""
    # --- boards (new in v2: multi-project support) ---
    p_boards = sub.add_parser(
        "boards",
        help="Manage kanban boards (one board per project / workstream)",
        description=(
            "Boards let you separate unrelated streams of work "
            "(projects, repos, domains) into isolated queues. Each "
            "board has its own DB, workspaces directory, and dispatcher "
            "loop — tasks on one board cannot collide with tasks on "
            "another. The first board is 'default' and always exists."
        ),
    )
    boards_sub = p_boards.add_subparsers(dest="boards_action")

    b_list = boards_sub.add_parser(
        "list", aliases=["ls"],
        help="List all boards with task counts",
    )
    b_list.add_argument("--json", action="store_true")
    b_list.add_argument("--all", action="store_true",
                        help="Include archived boards too")

    b_create = boards_sub.add_parser(
        "create", aliases=["new"],
        help="Create a new board",
    )
    b_create.add_argument("slug",
                          help="Board slug (kebab-case, e.g. atm10-server)")
    b_create.add_argument("--name", default=None,
                          help="Human-readable display name (defaults to Title Case of slug)")
    b_create.add_argument("--description", default=None,
                          help="Optional description")
    b_create.add_argument("--icon", default=None,
                          help="Optional emoji or single-character icon for the dashboard")
    b_create.add_argument("--color", default=None,
                          help="Optional hex color (e.g. '#8b5cf6') for the dashboard")
    b_create.add_argument("--switch", action="store_true",
                          help="Switch to the new board after creating it")
    b_create.add_argument("--default-workdir", default=None,
                          help="Default workspace path for tasks created on this board")

    b_rm = boards_sub.add_parser(
        "rm", aliases=["remove", "delete"],
        help="Archive (default) or delete a board",
    )
    b_rm.add_argument("slug")
    b_rm.add_argument("--delete", action="store_true",
                      help="Hard-delete the board directory instead of archiving it. "
                           "Default is to move it to boards/_archived/ so it's recoverable.")

    b_switch = boards_sub.add_parser(
        "switch", aliases=["use"],
        help="Set the active board for subsequent CLI calls",
    )
    b_switch.add_argument("slug")

    boards_sub.add_parser(
        "show", aliases=["current"],
        help="Print the currently-active board slug",
    )

    b_rename = boards_sub.add_parser(
        "rename",
        help="Change a board's human-readable display name (slug is immutable)",
    )
    b_rename.add_argument("slug")
    b_rename.add_argument("name", help="New display name")

    b_set_wd = boards_sub.add_parser(
        "set-default-workdir",
        help="Set the default workspace path for tasks on a board",
    )
    b_set_wd.add_argument("slug")
    b_set_wd.add_argument("path", nargs="?", default=None,
                          help="Absolute path to use as default workdir. Omit to clear.")

def _register_create_parser(sub: argparse._SubParsersAction) -> None:
    """Register the ``create`` subcommand."""
    # --- create ---
    p_create = sub.add_parser("create", help="Create a new task")
    p_create.add_argument("title", help="Task title")
    p_create.add_argument("--body", default=None, help="Optional opening post")
    p_create.add_argument("--assignee", default=None, help="Profile name to assign")
    p_create.add_argument("--parent", action="append", default=[],
                          help="Parent task id (repeatable)")
    p_create.add_argument("--workspace", default="scratch",
                          help="scratch | worktree | worktree:<path> | dir:<path> "
                               "(default: scratch)")
    p_create.add_argument("--branch", default=None,
                          help="Branch name for worktree tasks, e.g. wt/t6-wire")
    p_create.add_argument("--project", default=None,
                          help="Link to a project (id or slug). Anchors the task's "
                               "worktree under the project's primary repo with a "
                               "deterministic branch. See `hermes project list`.")
    p_create.add_argument("--tenant", default=None, help="Tenant namespace")
    p_create.add_argument("--priority", type=int, default=0, help="Priority tiebreaker")
    p_create.add_argument("--triage", action="store_true",
                          help="Park in triage — a specifier will flesh out the spec and promote to todo")
    p_create.add_argument("--idempotency-key", default=None,
                          help="Dedup key. If a non-archived task with this key exists, "
                               "its id is returned instead of creating a duplicate.")
    p_create.add_argument("--max-runtime", default=None,
                          help="Per-task runtime cap. Accepts seconds (300) or "
                               "durations (90s, 30m, 2h, 1d). When exceeded, "
                               "the dispatcher SIGTERMs (then SIGKILLs) the worker "
                               "and re-queues the task.")
    p_create.add_argument("--created-by", default="user",
                          help="Author name recorded on the task (default: user)")
    p_create.add_argument("--skill", action="append", default=[], dest="skills",
                          help="Skill to force-load into the worker "
                               "(repeatable). The kanban lifecycle is already "
                               "injected automatically. Example: "
                               "--skill translation --skill github-code-review")
    p_create.add_argument("--max-retries", type=int, default=None,
                          metavar="N",
                          help="Per-task override for the consecutive-failure "
                               "circuit breaker. Trip on the Nth failure — "
                               "e.g. --max-retries 1 blocks on the first "
                               "failure (no retries), --max-retries 3 allows "
                               "two retries. Omit to use the dispatcher's "
                               "kanban.failure_limit config "
                               f"(default {kb.DEFAULT_FAILURE_LIMIT}).")
    p_create.add_argument("--max-iterations", type=int, default=None,
                          metavar="N",
                          help="Per-task override for the worker's tool-"
                               "calling iteration budget. The dispatcher "
                               "injects HERMES_MAX_ITERATIONS=N into the "
                               "worker env so the LLM agent loop allows up "
                               "to N tool-calling rounds before the "
                               "iteration-budget guard fires. Omit to use "
                               "the profile's agent.max_turns default. "
                               "Useful for audit-class tasks that need more "
                               "headroom than the profile default.")
    p_create.add_argument("--max-continuations", type=int, default=None,
                          metavar="N",
                          help="Per-task cap for auto-continuation after a "
                               "worker explicitly reports iteration-budget "
                               "exhaustion. 0 disables auto-continuation for "
                               "this task; omit to use the code default.")
    p_create.add_argument("--goal", action="store_true", dest="goal_mode",
                          help="Run the worker in a goal loop: after each "
                               "turn a judge checks the response against the "
                               "card title/body and, if not done, the worker "
                               "keeps going in the same session until the "
                               "judge agrees it's complete (or the turn "
                               "budget runs out, which blocks the card for "
                               "review). Best for open-ended cards one shot "
                               "rarely finishes.")
    p_create.add_argument("--goal-max-turns", type=int, default=None,
                          metavar="N", dest="goal_max_turns",
                          help="Turn budget for --goal workers (default 20). "
                               "Ignored without --goal.")
    p_create.add_argument("--epic", default=None, dest="epic_id", metavar="EPIC_ID",
                          help="Attach the task to a durable epic (see "
                               "'hermes kanban epic'). On a --triage root the "
                               "epic propagates to every decompose child.")
    p_create.add_argument("--kind", default=None, dest="kind", metavar="KIND",
                          choices=sorted(_VALID_TASK_KINDS),
                          help="Optional task kind: code, research, review, ops, "
                               "text, or analysis (read-only counter-class to "
                               "code; verifier treats it task-class-aware).")
    p_create.add_argument("--initial-status",
                          choices=sorted(kb.VALID_INITIAL_STATUSES),
                          default="running",
                          help="Initial card status. Use 'blocked' for cards "
                               "that require immediate human ops (R3 gate) "
                               "to skip the brief running-to-blocked transition.")
    p_create.add_argument("--no-auto-scout", action="store_true",
                          help="Do not auto-inject a scout predecessor for this "
                               "new task even when review-gate heuristics classify "
                               "it as critical. Use for smoke/no-file/non-code "
                               "cards where a code recon scout would add noise.")
    p_create.add_argument("--no-notify-home", action="store_true",
                          help="Do not subscribe the new task to the configured "
                               "home channels. By default a CLI-created task is "
                               "subscribed so its terminal state (and its "
                               "decompose children's, via inheritance) reaches "
                               "the home channel without a manual notify-subscribe.")
    p_create.add_argument("--ui-impact", default=None, dest="ui_impact",
                          metavar="IMPACT", choices=sorted(kb._UI_IMPACT_VALUES),
                          help="UI-impact classifier (PlanSpec): none | minor | "
                               "redesign. NULL/none/minor → autonom-faehig; "
                               "redesign marks the task operator-gated (UI "
                               "rework needs human eyes). Read back via "
                               "'hermes kanban show' or effective_ui_impact().")
    p_create.add_argument("--json", action="store_true", help="Emit JSON output")

def _register_swarm_parser(sub: argparse._SubParsersAction) -> None:
    """Register the ``swarm`` subcommand."""
    # --- swarm ---
    p_swarm = sub.add_parser(
        "swarm",
        help="Create a Kanban Swarm v1 graph (parallel workers → verifier → synthesizer)",
    )
    p_swarm.add_argument("goal", help="Swarm goal / final outcome")
    p_swarm.add_argument(
        "--worker",
        action="append",
        default=[],
        metavar="PROFILE:TITLE[:SKILL,SKILL]",
        help="Parallel worker card (repeatable)",
    )
    p_swarm.add_argument("--verifier", required=True, help="Verifier profile")
    p_swarm.add_argument("--synthesizer", required=True, help="Synthesizer/writer profile")
    p_swarm.add_argument("--tenant", default=None, help="Tenant namespace")
    p_swarm.add_argument("--priority", type=int, default=0, help="Priority tiebreaker")
    p_swarm.add_argument("--created-by", default=None, help="Creator/anchor profile")
    p_swarm.add_argument("--idempotency-key", default=None, help="Dedup key for the root card")
    p_swarm.add_argument("--json", action="store_true", help="Emit JSON output")

def _register_task_create_parsers(sub: argparse._SubParsersAction) -> None:
    """Register ``create`` and ``swarm`` task-creation subcommands."""
    _register_create_parser(sub)
    _register_swarm_parser(sub)

def _register_task_query_parsers(sub: argparse._SubParsersAction) -> None:
    """Register list/show/assign/reclaim/reassign subcommands."""
    # --- list ---
    p_list = sub.add_parser("list", aliases=["ls"], help="List tasks")
    p_list.add_argument("--mine", action="store_true",
                        help="Filter by $HERMES_PROFILE as assignee")
    p_list.add_argument("--assignee", default=None,
                        help="Filter by assignee/lane (exact match)")
    p_list.add_argument("--created-by", default=None, dest="created_by",
                        metavar="AUTHOR",
                        help="Filter by the task's created_by author (exact match)")
    p_list.add_argument("--status", default=None,
                        choices=sorted(kb.VALID_STATUSES))
    p_list.add_argument("--tenant", default=None)
    p_list.add_argument("--session", default=None,
                        help="Filter by originating chat/agent session id "
                             "(set on tasks created from inside an ACP loop)")
    p_list.add_argument("--archived", action="store_true",
                        help="Include archived tasks")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument(
        "--sort",
        default=None,
        choices=sorted(kb.VALID_SORT_ORDERS.keys()),
        help="Sort order for listed tasks (default: priority)",
    )
    p_list.add_argument(
        "--workflow-template-id",
        default=None,
        metavar="ID",
        help="Restrict to tasks with this workflow_template_id",
    )
    p_list.add_argument(
        "--step-key",
        default=None,
        dest="current_step_key",
        metavar="KEY",
        help="Restrict to tasks with this current_step_key",
    )

    # --- show ---
    p_show = sub.add_parser("show", help="Show a task with comments + events")
    p_show.add_argument("task_id")
    p_show.add_argument("--json", action="store_true")
    p_show.add_argument(
        "--state-type",
        choices=("status", "outcome"),
        default=None,
        help="With --state-name: filter listed runs by task_runs column",
    )
    p_show.add_argument(
        "--state-name",
        default=None,
        metavar="VALUE",
        help="With --state-type: keep runs whose column equals this value",
    )

    # --- assign ---
    p_assign = sub.add_parser("assign", help="Assign or reassign a task")
    p_assign.add_argument("task_id")
    p_assign.add_argument("profile", help="Profile name (or 'none' to unassign)")

    # --- reclaim / reassign (recovery) ---
    p_reclaim = sub.add_parser(
        "reclaim",
        help="Release an active worker claim on a running task",
    )
    p_reclaim.add_argument("task_id")
    p_reclaim.add_argument(
        "--reason", default=None,
        help="Human-readable reason (recorded on the reclaimed event)",
    )

    p_reassign = sub.add_parser(
        "reassign",
        help="Reassign a task to a different profile, optionally reclaiming first",
    )
    p_reassign.add_argument("task_id")
    p_reassign.add_argument(
        "profile",
        help="New profile name (or 'none' to unassign)",
    )
    p_reassign.add_argument(
        "--reclaim", action="store_true",
        help="Release any active claim before reassigning (required if task is running)",
    )
    p_reassign.add_argument(
        "--reason", default=None,
        help="Human-readable reason (recorded on the reclaimed event)",
    )

def _register_diagnostics_parsers(sub: argparse._SubParsersAction) -> None:
    """Register the ``diagnostics`` (``diag``) subcommand."""
    # --- diagnostics (board-wide health) ---
    p_diag = sub.add_parser(
        "diagnostics",
        aliases=["diag"],
        help="List active diagnostics on the current board",
    )
    p_diag.add_argument(
        "--severity",
        choices=["warning", "error", "critical"],
        default=None,
        help="Only show diagnostics at or above this severity",
    )
    p_diag.add_argument(
        "--task",
        default=None,
        help="Only show diagnostics for one task id",
    )
    p_diag.add_argument(
        "--json", action="store_true",
        help="Emit JSON (structured) instead of the default human table",
    )

def _register_link_claim_comment_parsers(sub: argparse._SubParsersAction) -> None:
    """Register link, unlink, claim, and comment subcommands."""
    # --- link / unlink ---
    p_link = sub.add_parser("link", help="Add a parent->child dependency")
    p_link.add_argument("parent_id")
    p_link.add_argument("child_id")
    p_unlink = sub.add_parser("unlink", help="Remove a parent->child dependency")
    p_unlink.add_argument("parent_id")
    p_unlink.add_argument("child_id")

    # --- claim ---
    p_claim = sub.add_parser(
        "claim",
        help="Atomically claim a ready task (prints resolved workspace path)",
    )
    p_claim.add_argument("task_id")
    p_claim.add_argument("--ttl", type=int, default=kb.DEFAULT_CLAIM_TTL_SECONDS,
                         help="Claim TTL in seconds (default: 900)")

    # --- comment / complete / block / unblock / archive ---
    p_comment = sub.add_parser("comment", help="Append a comment")
    p_comment.add_argument("task_id")
    p_comment.add_argument("text", nargs="+", help="Comment body")
    p_comment.add_argument("--author", default=None,
                           help="Author name (default: $HERMES_PROFILE or 'user')")
    p_comment.add_argument("--max-len", type=int, default=None,
                           help="Trim the stored comment body to this many characters")
    p_comment.add_argument("--directive", action="store_true",
                           help="Mark as an OPERATOR DIRECTIVE (kind=directive) that "
                                "supersedes the task body in worker context. Operator-"
                                "only: rejected inside a spawned worker (HERMES_KANBAN_TASK set).")

def _register_complete_review_parsers(sub: argparse._SubParsersAction) -> None:
    """Register complete and review-commit subcommands."""
    p_complete = sub.add_parser("complete", help="Mark one or more tasks done")
    p_complete.add_argument("task_ids", nargs="+",
                            help="One or more task ids (only --result applies to all of them)")
    p_complete.add_argument("--result", default=None, help="Result summary")
    p_complete.add_argument("--summary", default=None,
                            help="Structured handoff summary for downstream tasks. "
                                 "Falls back to --result if omitted.")
    p_complete.add_argument("--metadata", default=None,
                            help='JSON dict of structured facts (e.g. \'{"changed_files": [...], '
                                 '"tests_run": 12}\'). Stored on the closing run.')

    p_review_commit = sub.add_parser(
        "review-commit",
        help="Opt-in local scoped Git commit after Coder handoff + Reviewer-B APPROVED",
    )
    p_review_commit.add_argument(
        "coder_task_id",
        help="Completed Coder task whose latest run metadata contains review_required=true",
    )
    p_review_commit.add_argument(
        "--reviewer-task",
        required=True,
        help="Completed Reviewer-B task whose latest run metadata contains verdict=APPROVED",
    )
    p_review_commit.add_argument("--repo", required=True, help="Git repo path")
    p_review_commit.add_argument(
        "--scoped-path",
        action="append",
        required=True,
        help="Repo-relative approved path to include (repeatable)",
    )
    p_review_commit.add_argument("--message", required=True, help="Local commit message")
    p_review_commit.add_argument("--json", action="store_true")

def _register_edit_status_parsers(sub: argparse._SubParsersAction) -> None:
    """Register edit/respec/block/set-workflow/schedule/unblock/promote."""
    p_edit = sub.add_parser(
        "edit",
        help="Edit recovery fields on an already-completed task",
    )
    p_edit.add_argument("task_id")
    p_edit.add_argument(
        "--result",
        required=True,
        help="Backfilled task result text for a done task",
    )
    p_edit.add_argument(
        "--summary",
        default=None,
        help="Structured handoff summary. Falls back to --result if omitted.",
    )
    p_edit.add_argument(
        "--metadata",
        default=None,
        help="JSON dict of structured facts to store on the latest completed run.",
    )

    p_respec = sub.add_parser(
        "respec",
        help="Archive a non-running task and create a replacement with a new body.",
    )
    p_respec.add_argument("task_id")
    body_group = p_respec.add_mutually_exclusive_group()
    body_group.add_argument(
        "--body",
        default=None,
        help="New task body (markdown). Stored on the replacement task.",
    )
    body_group.add_argument(
        "--body-file",
        default=None,
        help="Path to a markdown file containing the replacement task body.",
    )
    p_respec.add_argument(
        "--title",
        default=None,
        help="Replacement task title (defaults to the old task title).",
    )
    p_respec.add_argument(
        "--ac",
        dest="ac",
        default=None,
        help="Optional replacement acceptance criteria as 'AC-<id>: <statement>' bullets.",
    )
    p_respec.add_argument(
        "--author",
        default=None,
        help="Author recorded on the audit comment "
             "(default: $HERMES_PROFILE or 'user').",
    )

    p_block = sub.add_parser("block", help="Mark one or more tasks blocked")
    p_block.add_argument("task_id")
    p_block.add_argument("reason", nargs="*", help="Reason (also appended as a comment)")
    p_block.add_argument("--ids", nargs="+", default=None,
                         help="Additional task ids to block with the same reason (bulk mode)")
    p_block.add_argument(
        "--kind", default=None, choices=sorted(kb.VALID_BLOCK_KINDS),
        help=(
            "Typed block reason. 'dependency' waits in todo (auto-promoted "
            "when parents finish, no human); 'needs_input'/'capability' go to "
            "blocked for a human; 'review_revision' is review rework; "
            "'capacity' is token/iteration budget; 'integration' is merge/gate "
            "park; 'transient' marks a maybe-flaky failure. Repeated same-kind "
            "re-blocks after unblock route the task to triage to break "
            "unblock loops. Omit for a generic block."
        ),
    )
    p_block.add_argument(
        "--wait-for",
        type=_parse_wait_for,
        default=None,
        metavar="JSON",
        help=(
            "Required with --kind dependency. Closed JSON union: "
            "parents_all_done, not_before, or event_seen."
        ),
    )

    p_set_wf = sub.add_parser("set-workflow", help="Assign a workflow template to a task (seeds the first step)")
    p_set_wf.add_argument("task_id")
    p_set_wf.add_argument("template_id")

    p_schedule = sub.add_parser("schedule", help="Park one or more tasks in Scheduled (waiting on time, not human input)")
    p_schedule.add_argument("task_id")
    p_schedule.add_argument("reason", nargs="*", help="Reason/timing note (also appended as a comment)")
    p_schedule.add_argument("--ids", nargs="+", default=None,
                            help="Additional task ids to schedule with the same reason (bulk mode)")
    p_schedule.add_argument(
        "--due",
        type=_parse_schedule_due,
        metavar="WHEN",
        help="Wake at ISO-8601 offset time, Unix epoch seconds, or +2h/+45m/+90s",
    )

    p_unblock = sub.add_parser("unblock", help="Return one or more blocked/scheduled tasks to ready")
    p_unblock.add_argument(
        "--reason",
        default=None,
        help="Optional reason/note — recorded as a comment before unblocking. Quote multi-word reasons.",
    )
    p_unblock.add_argument(
        "--force",
        action="store_true",
        help="Unblock even when an exhausted per-task input-token budget would immediately re-park it",
    )
    p_unblock.add_argument(
        "--override-wait",
        action="store_true",
        help=(
            "Operator-only: discard an active worker wait and its typed parent "
            "edges. Requires --reason; the override is written to the audit trail."
        ),
    )
    p_unblock.add_argument("task_ids", nargs="+")

    p_promote = sub.add_parser(
        "promote",
        help="Manually move one or more todo/blocked tasks to ready (recovery path)",
    )
    p_promote.add_argument("task_id")
    p_promote.add_argument(
        "reason",
        nargs="*",
        help="Audit-trail reason (recorded on the task_events row)",
    )
    p_promote.add_argument(
        "--ids",
        nargs="+",
        default=None,
        help="Additional task ids to promote with the same reason (bulk mode)",
    )
    p_promote.add_argument(
        "--force",
        action="store_true",
        help="Promote even if parent dependencies are not yet done/archived",
    )
    p_promote.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the promotion without mutating state",
    )
    p_promote.add_argument(
        "--json",
        dest="json",
        action="store_true",
        help="Emit machine-readable JSON result",
    )

def _register_close_report_archive_parsers(sub: argparse._SubParsersAction) -> None:
    """Register close-sprint, report-discord, and archive."""
    p_close_sprint = sub.add_parser(
        "close-sprint",
        help=(
            "Aggregate kid receipts into a SPRINT CLOSURE comment on the "
            "parent, then complete the parent (recommended for any "
            "decomposed sprint with 3+ kids)"
        ),
    )
    p_close_sprint.add_argument("task_id", help="Parent task id to close")
    p_close_sprint.add_argument(
        "--auto-summary",
        action="store_true",
        help=(
            "Generate the 2–3 sentence overview via the auxiliary LLM. "
            "Falls back to a deterministic kid-count line when the aux "
            "client is unavailable."
        ),
    )
    p_close_sprint.add_argument(
        "--comment",
        dest="comment_file",
        default=None,
        help=(
            "Read the SPRINT CLOSURE comment body from FILE instead of "
            "auto-assembling it (or '-' for stdin). Mutually exclusive "
            "with --auto-summary; the override is used verbatim."
        ),
    )
    p_close_sprint.add_argument(
        "--result",
        default=None,
        help=(
            "Result text recorded on the parent task on completion "
            "(default: 'Sprint deliverable complete')"
        ),
    )
    p_close_sprint.add_argument(
        "--summary",
        default=None,
        help=(
            "Override the run-summary that lands on the parent's "
            "completion record (default: first non-empty line of the "
            "closure comment)"
        ),
    )
    p_close_sprint.add_argument(
        "--allow-open-kids",
        action="store_true",
        help=(
            "Skip the safety check that every kid must already be "
            "terminal (done/archived). Use ONLY when the closure is "
            "intentional with open kids — the comment will record their "
            "open status."
        ),
    )
    p_close_sprint.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the assembled closure-comment body to stdout but "
            "neither store it nor complete the parent"
        ),
    )
    p_close_sprint.add_argument(
        "--json",
        dest="json",
        action="store_true",
        help="Emit machine-readable JSON outcome",
    )

    # --- report-discord (combined human + structured report) ---
    p_report_discord = sub.add_parser(
        "report-discord",
        help="Render a Discord-ready Kanban report with kanban-report-v1 JSON",
    )
    p_report_discord.add_argument("--title", default=None, help="Human report title")
    p_report_discord.add_argument(
        "--summary",
        default=None,
        help="Override the generated one-sentence report summary",
    )
    p_report_discord.add_argument(
        "--scope-description",
        default=None,
        help="Scope label shown in the header and structured payload",
    )
    p_report_discord.add_argument(
        "--root-task",
        default=None,
        help="Limit report to one root task plus its direct parents/children",
    )
    p_report_discord.add_argument(
        "--status",
        action="append",
        default=None,
        help="Only include this task status (repeatable)",
    )
    p_report_discord.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived tasks in the report scope",
    )
    p_report_discord.add_argument(
        "--generated-at",
        default=None,
        help="Override generated_at timestamp (ISO-8601; tests/receipts only)",
    )
    p_report_discord.add_argument(
        "--json", action="store_true",
        help="Emit only the structured kanban-report-v1 JSON payload",
    )
    p_report_discord.add_argument(
        "--chunks",
        action="store_true",
        help="Emit JSON containing Discord message chunks instead of one Markdown report",
    )
    p_report_discord.add_argument(
        "--soft-cap",
        type=int,
        default=1800,
        help="Soft character cap per Discord chunk when --chunks is used (default: 1800)",
    )
    p_report_discord.add_argument(
        "--artifact-path",
        default=None,
        help="Full-report artifact path/URL to mention in split output",
    )
    p_report_discord.add_argument(
        "--output",
        default=None,
        help="Write the complete Markdown report to this path while printing the selected output",
    )

    p_archive = sub.add_parser("archive", help="Archive one or more tasks")
    p_archive.add_argument("task_ids", nargs="*",
                           help="Task ids to archive (default mode)")
    p_archive.add_argument(
        "--rm",
        dest="purge_ids",
        nargs="+",
        default=None,
        help="Permanently delete already-archived task ids from the board",
    )

def _register_task_lifecycle_parsers(sub: argparse._SubParsersAction) -> None:
    """Register link/claim/comment/complete/edit/block/promote/archive and related."""
    _register_link_claim_comment_parsers(sub)
    _register_complete_review_parsers(sub)
    _register_edit_status_parsers(sub)
    _register_close_report_archive_parsers(sub)

def _register_dispatch_daemon_parsers(sub: argparse._SubParsersAction) -> None:
    """Register tail/dispatch/holds/daemon/watch subcommands."""
    # --- tail ---
    p_tail = sub.add_parser("tail", help="Follow a task's event stream")
    p_tail.add_argument("task_id")
    p_tail.add_argument("--interval", type=float, default=1.0)

    # --- dispatch ---
    p_disp = sub.add_parser(
        "dispatch",
        help="One dispatcher pass: reclaim stale, promote ready, spawn workers",
    )
    p_disp.add_argument("--dry-run", action="store_true",
                        help="Don't actually spawn processes; just print what would happen")
    p_disp.add_argument("--max", type=int, default=None,
                        help="Cap number of spawns this pass")
    p_disp.add_argument("--failure-limit", type=int,
                        default=kb.DEFAULT_SPAWN_FAILURE_LIMIT,
                        help=f"Auto-block a task after this many consecutive non-success attempts "
                             f"(spawn_failed, timed_out, or crashed; default: {kb.DEFAULT_SPAWN_FAILURE_LIMIT})")
    p_disp.add_argument("--json", action="store_true")

    p_holds = sub.add_parser(
        "holds",
        help="List tasks the dispatcher is currently holding and why",
    )
    p_holds.add_argument("--json", action="store_true")

    # --- daemon (deprecated) ---
    p_daemon = sub.add_parser(
        "daemon",
        help="DEPRECATED — dispatcher now runs in the gateway. Use `hermes gateway start`.",
    )
    p_daemon.add_argument("--interval", type=float, default=60.0,
                          help="Seconds between dispatch ticks (default: 60)")
    p_daemon.add_argument("--max", type=int, default=None,
                          help="Cap number of spawns per tick")
    p_daemon.add_argument("--failure-limit", type=int,
                          default=kb.DEFAULT_SPAWN_FAILURE_LIMIT)
    p_daemon.add_argument("--pidfile", default=None,
                          help="Write the daemon's PID to this file on start")
    p_daemon.add_argument("--verbose", "-v", action="store_true",
                          help="Log each tick's outcome to stdout")
    # Undocumented escape hatch for users who truly cannot run the gateway.
    # Intentionally excluded from --help so nobody discovers it casually and
    # keeps the old double-dispatcher pattern alive.
    p_daemon.add_argument("--force", action="store_true",
                          help=argparse.SUPPRESS)

    # --- watch ---
    p_watch = sub.add_parser(
        "watch",
        help="Live-stream task_events to the terminal (Ctrl+C to exit)",
    )
    p_watch.add_argument("--assignee", default=None,
                         help="Only show events for tasks assigned to this profile")
    p_watch.add_argument("--tenant", default=None,
                         help="Only show events from tasks in this tenant")
    p_watch.add_argument("--kinds", default=None,
                         help="Comma-separated event kinds to include "
                              "(e.g. 'completed,blocked,gave_up,crashed,timed_out')")
    p_watch.add_argument("--interval", type=float, default=0.5,
                         help="Poll interval in seconds (default: 0.5)")

def _register_stats_notify_parsers(sub: argparse._SubParsersAction) -> None:
    """Register stats, backfill-costs, and notify-* subcommands."""
    # --- stats ---
    p_stats = sub.add_parser(
        "stats", help="Per-status + per-assignee counts + oldest-ready age",
    )
    p_stats.add_argument("--json", action="store_true")

    p_scores = sub.add_parser(
        "scores", help="Score aggregates and weekly approval rates",
    )
    p_scores.add_argument("--json", action="store_true")
    p_scores.add_argument(
        "--digest", action="store_true",
        help="Compact weekly digest for Discord (Markdown or --json)",
    )
    p_scores.add_argument(
        "--weeks", type=int, default=4,
        help="Number of weeks for --digest (default: 4)",
    )

    p_bfcost = sub.add_parser(
        "backfill-costs",
        help="Deferred profile-aware cost backfill for closed runs with NULL "
             "cost_usd (reads each worker's per-profile state.db)",
    )
    p_bfcost.add_argument("--limit", type=int, default=500,
                          help="Max closed runs to scan (newest first; default 500)")
    p_bfcost.add_argument("--since-hours", type=int, default=None,
                          help="Only runs ended within the last H hours (default: all)")
    p_bfcost.add_argument(
        "--dry-run",
        action="store_true",
        help="Report token-priced candidate count and total cost without writing",
    )
    p_bfcost.add_argument(
        "--repair-frozen-equivalent",
        action="store_true",
        help=(
            "Opt-in repair for closed non-claude-cli subscription runs frozen "
            "at cost_usd=0.0: set metadata.cost_usd_equivalent from "
            "input/output tokens and active lane model without changing cost_usd"
        ),
    )
    p_bfcost.add_argument(
        "--audited-claude-equivalent",
        action="store_true",
        help="S1b audited dry-run/classification for missing Claude cost_usd_equivalent stamps",
    )
    p_bfcost.add_argument(
        "--audited-non-claude-equivalent",
        action="store_true",
        help="S1c audited dry-run/classification for missing non-Claude cost_usd_equivalent stamps",
    )
    p_bfcost.add_argument(
        "--apply",
        action="store_true",
        help="Apply audited equivalent stamps; only use after explicit operator approval",
    )

    # --- backfill-metrics ---
    p_bfmetrics = sub.add_parser(
        "backfill-metrics",
        help="Idempotent backfill of run-metric scores (duration, tokens, "
             "cost, attempt index) from historical task_runs into scores",
    )

    # --- notify subscribe / list / remove ---
    p_nsub = sub.add_parser(
        "notify-subscribe",
        help="Subscribe a gateway source to a task's terminal events "
             "(used by /kanban subscribe in the gateway adapter)",
    )
    p_nsub.add_argument("task_id")
    p_nsub.add_argument("--platform", required=True)
    p_nsub.add_argument("--chat-id", required=True)
    p_nsub.add_argument("--thread-id", default=None)
    p_nsub.add_argument("--user-id", default=None)
    p_nsub.add_argument(
        "--notifier-profile", default=None,
        help="Profile gateway that owns/delivers this subscription (default: active profile)",
    )

    p_nlist = sub.add_parser(
        "notify-list",
        help="List notification subscriptions (optionally for a single task)",
    )
    p_nlist.add_argument("task_id", nargs="?", default=None)
    p_nlist.add_argument("--json", action="store_true")

    p_nrm = sub.add_parser(
        "notify-unsubscribe",
        help="Remove a gateway subscription from a task",
    )
    p_nrm.add_argument("task_id")
    p_nrm.add_argument("--platform", required=True)
    p_nrm.add_argument("--chat-id", required=True)
    p_nrm.add_argument("--thread-id", default=None)

def _register_worker_inspect_parsers(sub: argparse._SubParsersAction) -> None:
    """Register log/runs/heartbeat/assignees/context subcommands."""
    # --- log ---
    p_log = sub.add_parser(
        "log",
        help="Print the worker log for a task (from <kanban-root>/kanban/logs/)",
    )
    p_log.add_argument("task_id")
    p_log.add_argument("--tail", type=int, default=None,
                       help="Only print the last N bytes")

    # --- runs (per-attempt history for a task) ---
    p_runs = sub.add_parser(
        "runs",
        help="Show attempt history for a task (one row per run: profile, "
             "outcome, elapsed, summary)",
    )
    p_runs.add_argument("task_id")
    p_runs.add_argument("--json", action="store_true")
    p_runs.add_argument(
        "--state-type",
        choices=("status", "outcome"),
        default=None,
        help="With --state-name: filter runs by task_runs column",
    )
    p_runs.add_argument(
        "--state-name",
        default=None,
        metavar="VALUE",
        help="With --state-type: keep runs whose column equals this value",
    )

    # --- heartbeat (worker liveness signal) ---
    p_hb = sub.add_parser(
        "heartbeat",
        help="Emit a heartbeat event for a running task (worker liveness signal)",
    )
    p_hb.add_argument("task_id")
    p_hb.add_argument("--note", default=None,
                      help="Optional short note attached to the heartbeat event")

    # --- assignees ---
    p_asg = sub.add_parser(
        "assignees",
        help="List known profiles + per-profile task counts "
             "(union of ~/.hermes/profiles/ and current assignees on the board)",
    )
    p_asg.add_argument("--json", action="store_true")

    # --- context --- (for spawned workers)
    p_ctx = sub.add_parser(
        "context",
        help="Print the full context a worker sees for a task "
             "(title + body + parent results + comments).",
    )
    p_ctx.add_argument("task_id")

def _register_triage_parsers(sub: argparse._SubParsersAction) -> None:
    """Register specify/decompose/gc subcommands."""
    # --- specify --- (triage → todo via auxiliary LLM)
    p_specify = sub.add_parser(
        "specify",
        help="Flesh out a triage-column task into a concrete spec "
             "(title + body) and promote it to todo. Uses the auxiliary "
             "LLM configured under auxiliary.triage_specifier.",
    )
    p_specify.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="Task id to specify (required unless --all is given)",
    )
    p_specify.add_argument(
        "--all",
        dest="all_triage",
        action="store_true",
        help="Specify every task currently in the triage column",
    )
    p_specify.add_argument(
        "--tenant",
        default=None,
        help="When used with --all, restrict the sweep to this tenant",
    )
    p_specify.add_argument(
        "--author",
        default=None,
        help="Author name recorded on the audit comment "
             "(default: $HERMES_PROFILE or 'specifier')",
    )
    p_specify.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object per task on stdout",
    )

    # --- decompose --- (triage → fan-out via auxiliary LLM + orchestrator)
    p_decompose = sub.add_parser(
        "decompose",
        help="Decompose a triage-column task into a graph of child tasks "
             "routed to specialist profiles by description. Falls back to "
             "specify-style single-task promotion when the task doesn't "
             "benefit from fan-out. Uses auxiliary.kanban_decomposer.",
    )
    p_decompose.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="Task id to decompose (required unless --all is given)",
    )
    p_decompose.add_argument(
        "--all",
        dest="all_triage",
        action="store_true",
        help="Decompose every task currently in the triage column",
    )
    p_decompose.add_argument(
        "--tenant",
        default=None,
        help="When used with --all, restrict the sweep to this tenant",
    )
    p_decompose.add_argument(
        "--author",
        default=None,
        help="Author name recorded on the audit comment "
             "(default: $HERMES_PROFILE or 'decomposer')",
    )
    p_decompose.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object per task on stdout",
    )

    # --- gc ---
    p_gc = sub.add_parser(
        "gc", help="Garbage-collect archived-task workspaces, old events, and old logs",
    )
    p_gc.add_argument("--event-retention-days", type=int, default=30,
                      help="Delete task_events older than N days for terminal tasks (default: 30)")
    p_gc.add_argument("--log-retention-days", type=int, default=30,
                      help="Delete worker log files older than N days (default: 30)")

def _register_epic_parsers(sub: argparse._SubParsersAction) -> None:
    """Register the ``epic`` nested subcommand tree."""
    # --- epic (N-E3): durable goals spanning multiple task trees ---
    p_epic = sub.add_parser(
        "epic",
        help="Manage durable epics (create | list | show | close)",
    )
    epic_sub = p_epic.add_subparsers(dest="epic_action")
    e_create = epic_sub.add_parser("create", help="Create a new epic")
    e_create.add_argument("title", help="Epic title")
    e_create.add_argument("--body", default=None, help="Optional description")
    e_create.add_argument("--json", action="store_true", help="Emit JSON output")
    e_list = epic_sub.add_parser("list", aliases=["ls"], help="List epics")
    e_list.add_argument("--open", action="store_true", dest="open_only",
                        help="Only open epics")
    e_list.add_argument("--json", action="store_true", help="Emit JSON output")
    e_show = epic_sub.add_parser("show", help="Show an epic with its tasks + rollup")
    e_show.add_argument("epic_id", help="Epic id")
    e_show.add_argument("--json", action="store_true", help="Emit JSON output")
    e_close = epic_sub.add_parser("close", help="Mark an epic closed")
    e_close.add_argument("epic_id", help="Epic id")

def _register_closeout_parser(sub: argparse._SubParsersAction) -> None:
    """Register the ``closeout`` subcommand."""
    p_closeout = sub.add_parser(
        "closeout",
        help="Process a durable task closeout in a detached transient unit",
    )
    p_closeout.add_argument("task_id", help="Done task with closeout_pending")
    p_closeout.add_argument(
        "--inline",
        action="store_true",
        default=False,
        help="Claim and process in this process (used by the detached unit)",
    )
    p_closeout.add_argument("--json", action="store_true", help="Emit JSON result")

def _register_release_gate_parser(sub: argparse._SubParsersAction) -> None:
    """Register the ``release-gate`` subcommand."""
    p_release_gate = sub.add_parser(
        "release-gate",
        help="Execute a parked release-gate child: run the dashboard gate in "
             "the live checkout, spawn a bounded coder-claude fixer (worktree-"
             "only) on red, escalate to the operator on persistent red",
    )
    p_release_gate.add_argument("task_id", help="Release-gate child task id")
    p_release_gate.add_argument(
        "--max-retries",
        type=int,
        default=None,
        dest="max_retries",
        help="Bounded fixer retry budget (default: "
             "kanban.release_gate_fixer_max_retries, fallback 2)",
    )
    p_release_gate.add_argument(
        "--json", action="store_true", help="Emit JSON result"
    )
    p_release_gate.add_argument(
        "--inline",
        action="store_true",
        default=False,
        help="Run the gate synchronously in THIS process (legacy path). "
             "By default the gate is launched as a detached systemd transient "
             "unit via spawn_release_gate_activation — so it survives the "
             "dashboard restart the gate itself triggers. --inline keeps the "
             "old synchronous behaviour for tests and never spawns a unit.",
    )

def _register_release_operator_parsers(sub: argparse._SubParsersAction) -> None:
    """Register release-uireal, release-freigabe, complete-freigabe."""
    p_release_uireal = sub.add_parser(
        "release-uireal",
        help="Explicit operator GO: release a ui-real PlanSpec chain root held "
             "in 'scheduled' (records a uireal_released event with operator "
             "identity and flips the root scheduled->todo so its children "
             "dispatch). Idempotent; smoke/contract chains never need this.",
    )
    p_release_uireal.add_argument("task_id", help="ui-real PlanSpec root task id")
    p_release_uireal.add_argument(
        "--author",
        default=None,
        help="Operator identity recorded on the release event "
             "(default: active profile name)",
    )
    p_release_uireal.add_argument(
        "--json", action="store_true", help="Emit JSON result"
    )

    p_release_freigabe = sub.add_parser(
        "release-freigabe",
        help="Explicit operator GO: release a freigabe:operator PlanSpec chain "
             "root held in 'scheduled' (records a freigabe_released event with "
             "operator identity, flips the root scheduled->todo and promotes its "
             "held children so they dispatch). Idempotent; non-operator chains "
             "never need this.",
    )
    p_release_freigabe.add_argument(
        "task_id", help="freigabe:operator PlanSpec root task id"
    )
    p_release_freigabe.add_argument(
        "--author",
        default=None,
        help="Operator identity recorded on the release event "
             "(default: active profile name)",
    )
    p_release_freigabe.add_argument(
        "--json", action="store_true", help="Emit JSON result"
    )

    p_complete_freigabe = sub.add_parser(
        "complete-freigabe",
        help="Close a freigabe:operator PlanSpec chain root held in 'scheduled' "
             "as done elsewhere (records freigabe_completed, archives the held "
             "root/children, and stores the required rationale comment).",
    )
    p_complete_freigabe.add_argument(
        "task_id", help="freigabe:operator PlanSpec root task id"
    )
    p_complete_freigabe.add_argument(
        "--note",
        required=True,
        help="Mandatory rationale recorded as the freigabe_completed comment",
    )
    p_complete_freigabe.add_argument(
        "--author",
        default=None,
        help="Operator identity recorded on the completion event "
             "(default: active profile name)",
    )
    p_complete_freigabe.add_argument(
        "--json", action="store_true", help="Emit JSON result"
    )

    p_veto_freigabe = sub.add_parser(
        "veto-freigabe",
        help="Explicit operator VETO: archive a freigabe:operator PlanSpec chain "
             "root held in 'scheduled' (records a freigabe_vetoed event, archives "
             "the held root and its held children — nothing builds). Idempotent-"
             "safe: returns non-zero if the id is not a held operator root. This "
             "is the CLI sibling of the dashboard veto route, so a vetoed lever is "
             "correctly suppressed and counted by strategist reflect.",
    )
    p_veto_freigabe.add_argument(
        "task_id", help="freigabe:operator PlanSpec root task id"
    )
    p_veto_freigabe.add_argument(
        "--author",
        default=None,
        help="Operator identity recorded on the veto event "
             "(default: active profile name)",
    )
    p_veto_freigabe.add_argument(
        "--json", action="store_true", help="Emit JSON result"
    )

def _register_release_parsers(sub: argparse._SubParsersAction) -> None:
    """Register closeout and release-*/complete-freigabe subcommands."""
    _register_closeout_parser(sub)
    _register_release_gate_parser(sub)
    _register_release_operator_parsers(sub)

def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Attach the ``kanban`` subcommand tree under an existing subparsers.

    Returns the top-level ``kanban`` parser so caller can ``set_defaults``.
    """
    kanban_parser = parent_subparsers.add_parser(
        "kanban",
        help="Multi-profile collaboration board (tasks, links, comments)",
        description=(
            "Durable SQLite-backed task board shared across Hermes profiles. "
            "Tasks are claimed atomically, can depend on other tasks, and "
            "are executed by a named profile in an isolated workspace. "
            "See https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban "
            "or docs/hermes-kanban-v1-spec.pdf for the full design."
        ),
    )
    # --- global --board flag ---
    # Applies to every subcommand below. When set, scopes all reads and
    # writes to that board's DB. When omitted, resolves via the
    # HERMES_KANBAN_BOARD env var, then the persisted current-board
    # file, then "default". See kanban_db.get_current_board().
    kanban_parser.add_argument(
        "--board",
        default=None,
        metavar="<slug>",
        help=(
            "Board slug to operate on. Defaults to the current board "
            "(set via `hermes kanban boards switch <slug>` or the "
            "HERMES_KANBAN_BOARD env var). Use `hermes kanban boards list` "
            "to see all boards."
        ),
    )
    sub = kanban_parser.add_subparsers(dest="kanban_action")

    _register_init_parsers(sub)
    _register_boards_parsers(sub)
    _register_task_create_parsers(sub)
    _register_task_query_parsers(sub)
    _register_diagnostics_parsers(sub)
    _register_task_lifecycle_parsers(sub)
    _register_dispatch_daemon_parsers(sub)
    _register_stats_notify_parsers(sub)
    _register_worker_inspect_parsers(sub)
    _register_triage_parsers(sub)
    _register_epic_parsers(sub)
    _register_release_parsers(sub)

    kanban_parser.set_defaults(_kanban_parser=kanban_parser)
    return kanban_parser

# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

def kanban_command(args: argparse.Namespace) -> int:
    """Entry point from ``hermes kanban …`` argparse dispatch.

    Returns a shell-style exit code (0 on success, non-zero on error).
    """
    action = getattr(args, "kanban_action", None)
    if not action:
        # No subaction given: print help via the stored parser reference.
        parser = getattr(args, "_kanban_parser", None)
        if parser is not None:
            parser.print_help()
        else:
            print(
                "usage: hermes kanban <action> [options]\n"
                "Run 'hermes kanban --help' for the full list of actions.",
                file=sys.stderr,
            )
        return 0

    # Board-management commands operate on board metadata and the persisted
    # current-board pointer itself. They must ignore the shared `--board`
    # task-routing override; otherwise `/kanban --board beta boards show`
    # reports beta as the current board even when the on-disk pointer is
    # alpha.
    if action == "boards":
        return _dispatch_boards(args)

    # `--board <slug>` applies to every subcommand below by way of an
    # env-var pin for the duration of this call. Using HERMES_KANBAN_BOARD
    # (rather than threading `board=` through 50+ kb.connect() sites)
    # keeps the patch small and inherits the exact same resolution the
    # dispatcher uses for workers — consistency is a feature here.
    board_override = getattr(args, "board", None)
    board_scope = contextlib.nullcontext()
    if board_override:
        try:
            normed = kb._normalize_board_slug(board_override)
        except ValueError as exc:
            _kanban_err(exc)
            return 2
        if not normed:
            print("kanban: --board requires a slug", file=sys.stderr)
            return 2
        # Boards other than 'default' must already exist — typoed slugs
        # would otherwise silently create an empty board.
        if normed != kb.DEFAULT_BOARD and not kb.board_exists(normed):
            print(
                f"kanban: board {normed!r} does not exist. "
                f"Create it with `hermes kanban boards create {normed}`.",
                file=sys.stderr,
            )
            return 1
        board_scope = kb.scoped_current_board(normed)

    # Auto-initialize the DB before dispatching any subcommand. init_db
    # is idempotent, so running it every invocation is cheap (one
    # SELECT against sqlite_master when tables already exist) and
    # prevents "no such table: tasks" on first use from a fresh
    # HERMES_HOME. Previously only `init` and `daemon` triggered
    # schema creation; `create` / `list` / every other command would
    # error out on a fresh install.
    with board_scope:
        try:
            kb.init_db()
        except Exception as exc:
            print(f"kanban: could not initialize database: {exc}", file=sys.stderr)
            return 1

        handler = _KANBAN_ACTION_HANDLERS.get(action)
        if not handler:
            print(f"kanban: unknown action {action!r}", file=sys.stderr)
            return 2
        try:
            return int(handler(args) or 0)
        except (ValueError, RuntimeError) as exc:
            _kanban_err(exc)
            return 1

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _profile_author() -> str:
    """Best-effort author name for an interactive CLI call."""
    for env in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        v = os.environ.get(env)
        if v:
            return v
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "user"
    except Exception:
        return "user"

# ---------------------------------------------------------------------------
# Boards management (hermes kanban boards …)
# ---------------------------------------------------------------------------

def _dispatch_boards(args: argparse.Namespace) -> int:
    """Handle ``hermes kanban boards <action>``.

    Boards management is deliberately separate from the task-level
    commands: it operates on the filesystem (board directories,
    ``current`` pointer, ``board.json``), not on the per-board SQLite
    DB, so a fresh HERMES_HOME that has never called ``kanban init``
    can still run ``boards create`` / ``boards list``.
    """
    sub = getattr(args, "boards_action", None) or "list"
    if sub in {"list", "ls"}:
        return _cmd_boards_list(args)
    if sub in {"create", "new"}:
        return _cmd_boards_create(args)
    if sub in {"rm", "remove", "delete"}:
        return _cmd_boards_rm(args)
    if sub in {"switch", "use"}:
        return _cmd_boards_switch(args)
    if sub in {"show", "current"}:
        return _cmd_boards_show(args)
    if sub == "rename":
        return _cmd_boards_rename(args)
    if sub == "set-default-workdir":
        return _cmd_boards_set_default_workdir(args)
    print(f"kanban boards: unknown action {sub!r}", file=sys.stderr)
    return 2

def _board_task_counts(slug: str) -> dict[str, int]:
    """Return ``{status: count}`` for a board. Safe to call on an empty DB."""
    try:
        path = kb.kanban_db_path(board=slug)
        if not path.exists():
            return {}
        with kb.connect_closing(board=slug) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}
    except Exception:
        return {}

def _cmd_boards_list(args: argparse.Namespace) -> int:
    include_archived = bool(getattr(args, "all", False))
    boards = kb.list_boards(include_archived=include_archived)
    # Enrich each entry with task counts + whether it's the current board.
    current = kb.get_current_board()
    for b in boards:
        b["is_current"] = (b["slug"] == current)
        b["counts"] = _board_task_counts(b["slug"])
        b["total"] = sum(b["counts"].values())
    if getattr(args, "json", False):
        _emit_json(boards)
        return 0
    # Human table: marker (•) for current, slug, display name, counts.
    if not boards:
        print("(no boards — create one with `hermes kanban boards create <slug>`)")
        return 0
    print(f"{'':2s}  {'SLUG':24s}  {'NAME':28s}  COUNTS")
    for b in boards:
        marker = "●" if b["is_current"] else " "
        counts = b["counts"] or {}
        counts_str = (
            ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            or "(empty)"
        )
        name = b.get("name") or ""
        if b.get("archived"):
            name += " [archived]"
        print(f"{marker:2s}  {b['slug']:24s}  {name:28s}  {counts_str}")
    print()
    print(f"Current board: {current}")
    if len(boards) > 1:
        print("Switch boards with `hermes kanban boards switch <slug>`.")
    return 0

def _cmd_boards_create(args: argparse.Namespace) -> int:
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        print(f"kanban boards create: {exc}", file=sys.stderr)
        return 2
    if not normed:
        print("kanban boards create: slug is required", file=sys.stderr)
        return 2
    already = kb.board_exists(normed) and normed != kb.DEFAULT_BOARD
    meta = kb.create_board(
        normed,
        name=args.name,
        description=args.description,
        icon=args.icon,
        color=args.color,
        default_workdir=args.default_workdir,
    )
    verb = "already exists" if already else "created"
    print(f"Board {meta['slug']!r} {verb}.")
    print(f"  Display name: {meta.get('name', '')}")
    print(f"  DB path:      {meta['db_path']}")
    if getattr(args, "switch", False):
        kb.set_current_board(meta["slug"])
        print(f"  Switched to {meta['slug']!r}.")
    else:
        print(f"  Use `hermes kanban boards switch {meta['slug']}` to make it current.")
    return 0

def _cmd_boards_rm(args: argparse.Namespace) -> int:
    # When the user runs `hermes kanban boards delete <slug>` (alias), the
    # boards_action is 'delete' but args.delete is never set to True because
    # the --delete flag belongs to the 'rm' subparser only.  Detect the alias
    # and treat it identically to `boards rm --delete` (fixes #23139).
    force_delete = getattr(args, "delete", False) or getattr(args, "boards_action", "") == "delete"
    try:
        res = kb.remove_board(args.slug, archive=not force_delete)
    except ValueError as exc:
        print(f"kanban boards rm: {exc}", file=sys.stderr)
        return 1
    if res["action"] == "archived":
        print(f"Board {res['slug']!r} archived → {res['new_path']}")
        print("Recover by moving the directory back to "
              "<root>/kanban/boards/<slug>/.")
    else:
        print(f"Board {res['slug']!r} deleted.")
    return 0

def _cmd_boards_switch(args: argparse.Namespace) -> int:
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        print(f"kanban boards switch: {exc}", file=sys.stderr)
        return 2
    if not normed:
        print("kanban boards switch: slug is required", file=sys.stderr)
        return 2
    if not kb.board_exists(normed):
        print(
            f"kanban boards switch: board {normed!r} does not exist. "
            f"Create it with `hermes kanban boards create {normed}`.",
            file=sys.stderr,
        )
        return 1
    kb.set_current_board(normed)
    print(f"Active board is now {normed!r}.")
    return 0

def _cmd_boards_show(args: argparse.Namespace) -> int:
    current = kb.get_current_board()
    meta = kb.read_board_metadata(current)
    counts = _board_task_counts(current)
    total = sum(counts.values())
    print(f"Current board: {current}")
    print(f"  Display name: {meta.get('name', '')}")
    if meta.get("description"):
        print(f"  Description:  {meta['description']}")
    print(f"  DB path:      {meta['db_path']}")
    print(f"  Tasks:        {total} total"
          + (f" ({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))})"
             if counts else ""))
    return 0

def _cmd_boards_rename(args: argparse.Namespace) -> int:
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        print(f"kanban boards rename: {exc}", file=sys.stderr)
        return 2
    if not normed or not kb.board_exists(normed):
        print(f"kanban boards rename: board {args.slug!r} does not exist",
              file=sys.stderr)
        return 1
    meta = kb.write_board_metadata(normed, name=args.name)
    print(f"Board {normed!r} renamed to {meta['name']!r}.")
    return 0

def _cmd_boards_set_default_workdir(args: argparse.Namespace) -> int:
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        print(f"kanban boards set-default-workdir: {exc}", file=sys.stderr)
        return 2
    if not normed or not kb.board_exists(normed):
        print(f"kanban boards set-default-workdir: board {args.slug!r} does not exist",
              file=sys.stderr)
        return 1
    meta = kb.write_board_metadata(normed, default_workdir=args.path)
    new_val = meta.get("default_workdir")
    if new_val:
        print(f"Board {normed!r} default workdir set to {new_val!r}.")
    else:
        print(f"Board {normed!r} default workdir cleared.")
    return 0

# ---------------------------------------------------------------------------
# Shared task-handler utilities + command handlers
# ---------------------------------------------------------------------------

def _parse_duration(val) -> Optional[int]:
    """Parse ``30s`` / ``5m`` / ``2h`` / ``1d`` or a raw integer → seconds.

    Returns None for empty input. Raises ValueError on malformed input so
    the CLI can surface a usage error cleanly.
    """
    if val is None or val == "":
        return None
    s = str(val).strip().lower()
    # Bare integer → seconds.
    try:
        return int(s)
    except ValueError:
        pass
    # Suffixed form.
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in units:
        try:
            n = float(s[:-1])
        except ValueError as exc:
            raise ValueError(f"malformed duration {val!r}") from exc
        return int(n * units[s[-1]])
    raise ValueError(f"malformed duration {val!r} (expected 30s, 5m, 2h, 1d, or a number)")

def _cmd_init(args: argparse.Namespace) -> int:
    path = kb.init_db()
    print(f"Kanban DB initialized at {path}")

    print()
    # Enumerate profiles on disk so the user knows what assignees are
    # already addressable. Multica does this auto-detection on its
    # daemon start; we do it here at init time instead because our
    # dispatcher doesn't need to enumerate — we just pass the name
    # through to `hermes -p <name>`.
    try:
        profiles = kb.list_profiles_on_disk()
    except Exception:
        profiles = []
    if profiles:
        print(f"Discovered {len(profiles)} profile(s) on disk; any of these can "
              f"be an --assignee:")
        for name in profiles:
            print(f"  {name}")
    else:
        print("No profiles found under ~/.hermes/profiles/.")
        print("Create one with `hermes -p <name> setup` before assigning tasks.")
    print()
    print("Next step: start the gateway so ready tasks actually get picked up.")
    print("  hermes gateway start")
    print()
    print(
        "The gateway hosts an embedded dispatcher that ticks every 60 seconds\n"
        "by default (config: kanban.dispatch_interval_seconds). Without a\n"
        "running gateway, tasks stay in 'ready' forever."
    )
    return 0

def _cmd_heartbeat(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        ok = kb.heartbeat_worker(
            conn,
            args.task_id,
            note=getattr(args, "note", None),
            expected_run_id=_worker_run_id_for(args.task_id),
        )
    if not ok:
        print(f"cannot heartbeat {args.task_id} (not running?)", file=sys.stderr)
        return 1
    print(f"Heartbeat recorded for {args.task_id}")
    return 0

def _cmd_closeout(args: argparse.Namespace) -> int:
    """Launch closeout detached, or run the single-task unit core inline.

    Exit 0 means delivered or detached unit accepted, 2 is a durable safety
    hold/ambiguous release, and 1 is a launch, claim, or processing failure.
    """

    from hermes_cli import kanban_closeout as closeout

    inline = bool(getattr(args, "inline", False))
    board = getattr(args, "board", None)
    if not inline:
        payload = closeout.spawn_closeout_unit(args.task_id, board=board)
        if getattr(args, "json", False):
            print(json.dumps(payload))
        else:
            state = "started" if payload.get("ok") else "FAILED"
            print(
                f"closeout {args.task_id}: detached unit {state} "
                f"(unit={payload.get('unit')}); {payload.get('detail', '')}"
            )
        return 0 if payload.get("ok") else 1

    try:
        with kb.connect_closing(board=board) as conn:
            result = closeout.process_closeout(conn, args.task_id)
        payload = {
            "task_id": result.task_id,
            "state": result.state,
            "release_state": result.release_state,
            "receipt_path": result.receipt_path,
            "delivered": result.delivered,
            "error": result.error,
        }
    except Exception as exc:
        payload = {
            "task_id": args.task_id,
            "state": "failed",
            "delivered": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if getattr(args, "json", False):
        print(json.dumps(payload))
    elif payload["state"] == "failed":
        print(f"closeout {args.task_id}: {payload['error']}", file=sys.stderr)
    else:
        print(f"closeout {args.task_id}: {payload['state']}")
    if payload.get("delivered"):
        return 0
    if payload.get("state") in {"pending", "not_claimed"}:
        return 0
    if payload.get("state") in {"held", "ambiguous"}:
        return 2
    return 1

def _cmd_release_gate(args: argparse.Namespace) -> int:
    """Run the release gate on a parked release-gate child.

    By default the gate is launched as a DETACHED systemd transient unit via
    :func:`spawn_release_gate_activation` (the same path the dashboard execute
    endpoint takes). Running the gate inline in this process used to be the only
    option, but the gate restarts the dashboard backend — a ``systemctl restart``
    that kills the very process that must write the child's terminal result
    BEFORE it returns (the self-termination trap). The detached unit sits in its
    own cgroup and survives the restart.

    ``--inline`` keeps the old synchronous behaviour for tests: it runs
    :func:`execute_release_gate` directly in this process and never spawns a
    unit. The detached path needs no pre-deploy backup (the gate core takes care
    of that); the inline path still does it best-effort.

    Exit codes (default / detached):
        0  activation unit started (watch the task for green/escalation)
        1  the activation unit could not be started (precondition/spawn error)
    Exit codes (--inline):
        0  green
        2  escalated (operator action needed)
        1  precondition error / failure
    """
    from hermes_cli import kanban_worktrees as kwt

    inline = bool(getattr(args, "inline", False))

    # --- Detached (default): hand off to a transient systemd unit ------------
    if not inline:
        board = getattr(args, "board", None)
        launched = kwt.spawn_release_gate_activation(
            args.task_id, board=board,
        )
        if getattr(args, "json", False):
            print(json.dumps(launched))
        else:
            tag = "started" if launched.get("ok") else "FAILED"
            print(
                f"release-gate {args.task_id}: detached activation {tag} "
                f"(unit={launched.get('unit')}); {launched.get('detail', '')}"
            )
        return 0 if launched.get("ok") else 1

    # --- Inline (legacy / tests): run the gate core in this process ----------
    # Best-effort pre-deploy snapshot — never blocks the gate on failure.
    try:
        from hermes_cli.backup import create_pre_deploy_backup
        create_pre_deploy_backup()
    except Exception as exc:  # pragma: no cover
        print(f"Warning: pre-deploy backup failed ({exc}); continuing.", file=sys.stderr)

    with kb.connect_closing(board=getattr(args, "board", None)) as conn:
        try:
            result = kwt.execute_release_gate(
                conn,
                args.task_id,
                max_retries=getattr(args, "max_retries", None),
            )
        except kwt.ReleaseGateError as exc:
            print(f"release-gate: {exc}", file=sys.stderr)
            return 1
    if getattr(args, "json", False):
        print(json.dumps(result))
    else:
        print(
            f"Release-gate {args.task_id}: {result.get('status')} "
            f"(fixer attempts: {result.get('fixer_attempts', 0)})"
        )
    return 0 if result.get("status") == "green" else 2

def _cmd_release_uireal(args: argparse.Namespace) -> int:
    """Release a ui-real PlanSpec chain root held in 'scheduled' for an explicit
    operator GO. Records a ``uireal_released`` event with operator identity and
    flips the root scheduled->todo so its children become dispatchable. Idempotent
    (a root already released stays released, re-stamping the event).

    Exit codes: 0 released (incl. idempotent re-release); 1 the task is not a
    releasable ui-real root (not ui-real, not held in scheduled/todo, or unknown)."""
    author = getattr(args, "author", None) or _profile_author()
    with kb.connect_closing() as conn:
        released = kb.release_uireal_root(conn, args.task_id, author=author)
    if getattr(args, "json", False):
        print(json.dumps({
            "task_id": args.task_id, "released": released, "author": author,
        }))
    elif released:
        print(
            f"ui-real root {args.task_id} released by {author} "
            "(children now dispatchable)."
        )
    else:
        print(
            f"release-uireal: {args.task_id} is not a held ui-real root "
            "(not ui-real, not held in scheduled, or unknown) — nothing released.",
            file=sys.stderr,
        )
    return 0 if released else 1

def _cmd_release_freigabe(args: argparse.Namespace) -> int:
    """Release a freigabe:operator PlanSpec chain root held in 'scheduled' for an
    explicit operator GO. Records a ``freigabe_released`` event with operator
    identity, flips the root scheduled->todo and promotes its held children so
    the chain dispatches. Idempotent (a root already released stays released,
    re-stamping the event and re-releasing any still-held child).

    Exit codes: 0 released (incl. idempotent re-release); 1 the task is not a
    releasable freigabe:operator root (not operator, not held in scheduled/todo,
    or unknown)."""
    author = getattr(args, "author", None) or _profile_author()
    with kb.connect_closing() as conn:
        released = kb.release_freigabe_hold(conn, args.task_id, author=author)
    if getattr(args, "json", False):
        print(json.dumps({
            "task_id": args.task_id, "released": released, "author": author,
        }))
    elif released:
        print(
            f"freigabe:operator root {args.task_id} released by {author} "
            "(children now dispatchable)."
        )
    else:
        print(
            f"release-freigabe: {args.task_id} is not a held freigabe:operator root "
            "(not operator, not held in scheduled, or unknown) — nothing released.",
            file=sys.stderr,
        )
    return 0 if released else 1

def _cmd_complete_freigabe(args: argparse.Namespace) -> int:
    """Close a held freigabe:operator PlanSpec root as done elsewhere.

    This is the CLI sibling of the dashboard complete route: unlike release, it
    never dispatches the children. It archives the still-held chain and records
    the operator rationale as a comment for auditability.
    """
    author = getattr(args, "author", None) or _profile_author()
    note = str(getattr(args, "note", "") or "").strip()
    with kb.connect_closing() as conn:
        completed = kb.complete_freigabe_hold(
            conn,
            args.task_id,
            author=author,
            note=note,
        )
    if getattr(args, "json", False):
        print(json.dumps({
            "task_id": args.task_id,
            "completed": completed,
            "author": author,
        }))
    elif completed:
        print(
            f"freigabe:operator root {args.task_id} closed by {author} "
            "(done elsewhere; held children archived)."
        )
    else:
        print(
            f"complete-freigabe: {args.task_id} is not a held freigabe:operator root "
            "(not operator, not held in scheduled, or unknown) — nothing closed.",
            file=sys.stderr,
        )
    return 0 if completed else 1

def _cmd_veto_freigabe(args: argparse.Namespace) -> int:
    """Veto a held freigabe:operator PlanSpec chain root — the CLI sibling of the
    dashboard veto route (:func:`kb.dismiss_freigabe_hold`). Records a
    ``freigabe_vetoed`` event and archives the held root plus its held children so
    nothing builds. Unlike ``kanban archive``, this writes the ``freigabe_vetoed``
    learning event, so the lever is suppressed and counted by strategist reflect.

    Exit codes: 0 vetoed; 1 the task is not a vetoable freigabe:operator root (not
    operator, not held in scheduled, or unknown — touching nothing)."""
    author = getattr(args, "author", None) or _profile_author()
    with kb.connect_closing() as conn:
        vetoed = kb.dismiss_freigabe_hold(conn, args.task_id, author=author)
    if getattr(args, "json", False):
        print(json.dumps({
            "task_id": args.task_id, "vetoed": vetoed, "author": author,
        }))
    elif vetoed:
        print(
            f"freigabe:operator root {args.task_id} vetoed by {author} "
            "(held root and children archived; nothing builds)."
        )
    else:
        print(
            f"veto-freigabe: {args.task_id} is not a held freigabe:operator root "
            "(not operator, not held in scheduled, or unknown) — nothing vetoed.",
            file=sys.stderr,
        )
    return 0 if vetoed else 1

def _cmd_assignees(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        data = kb.known_assignees(conn)
    if getattr(args, "json", False):
        _emit_json(data)
        return 0
    if not data:
        print("(no assignees — create a profile with `hermes -p <name> setup`)")
        return 0
    # Header
    print(f"{'NAME':20s}  {'ON DISK':8s}  COUNTS")
    for entry in data:
        on_disk = "yes" if entry["on_disk"] else "no"
        counts = entry["counts"] or {}
        count_str = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "(idle)"
        print(f"{entry['name']:20s}  {on_disk:8s}  {count_str}")
    return 0

# ---------------------------------------------------------------------------
# Task create / swarm / list / show
# ---------------------------------------------------------------------------

def _create_validate_args(args: argparse.Namespace) -> tuple[Any, ...] | int:
    """Validate create flags.

    Returns ``(ws_kind, ws_path, branch_name, max_runtime, max_retries,
    max_iterations, max_continuations)`` or an int exit code on error.
    """
    try:
        ws_kind, ws_path = _parse_workspace_flag(args.workspace)
        branch_name = _parse_branch_flag(getattr(args, "branch", None))
    except argparse.ArgumentTypeError as exc:
        _kanban_err(exc)
        return 2
    if branch_name and ws_kind != "worktree":
        print("kanban: --branch is only valid with --workspace worktree", file=sys.stderr)
        return 2
    if getattr(args, "assignee", None):
        try:
            args.assignee = kb.validate_spawnable_assignee(args.assignee)
        except ValueError as exc:
            _kanban_err(exc)
            return 2
    try:
        max_runtime = _parse_duration(getattr(args, "max_runtime", None))
    except ValueError as exc:
        print(f"kanban: --max-runtime: {exc}", file=sys.stderr)
        return 2
    max_retries = getattr(args, "max_retries", None)
    if max_retries is not None and max_retries < 1:
        print(
            f"kanban: --max-retries must be >= 1 (got {max_retries}); "
            "use 1 to trip on the first failure.",
            file=sys.stderr,
        )
        return 2
    max_iterations = getattr(args, "max_iterations", None)
    if max_iterations is not None and max_iterations < 1:
        print(
            f"kanban: --max-iterations must be >= 1 (got {max_iterations})",
            file=sys.stderr,
        )
        return 2
    max_continuations = getattr(args, "max_continuations", None)
    if max_continuations is not None and max_continuations < 0:
        print(
            f"kanban: --max-continuations must be >= 0 (got {max_continuations})",
            file=sys.stderr,
        )
        return 2
    return (ws_kind, ws_path, branch_name, max_runtime, max_retries, max_iterations, max_continuations)


def _create_subscribe_home_channels(conn: Any, task_id: str) -> None:
    """Subscribe a newly created task to configured home channels (idempotent)."""
    try:
        from gateway.config import configured_home_channels
        homes = configured_home_channels()
    except Exception:
        homes = []
    for home in homes:
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform=home["platform"],
            chat_id=home["chat_id"],
            thread_id=home["thread_id"] or None,
            notifier_profile=_profile_author(),
        )


def _cmd_create(args: argparse.Namespace) -> int:
    validated = _create_validate_args(args)
    if isinstance(validated, int):
        return validated
    ws_kind, ws_path, branch_name, max_runtime, max_retries, max_iterations, max_continuations = validated
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title=args.title,
            body=args.body,
            assignee=args.assignee,
            created_by=args.created_by or _profile_author(),
            workspace_kind=ws_kind,
            workspace_path=ws_path,
            branch_name=branch_name,
            project_id=getattr(args, "project", None),
            tenant=args.tenant,
            priority=args.priority,
            parents=tuple(args.parent or ()),
            triage=bool(getattr(args, "triage", False)),
            idempotency_key=getattr(args, "idempotency_key", None),
            max_runtime_seconds=max_runtime,
            skills=getattr(args, "skills", None) or None,
            max_retries=max_retries,
            max_iterations=max_iterations,
            max_continuations=max_continuations,
            goal_mode=bool(getattr(args, "goal_mode", False)),
            goal_max_turns=getattr(args, "goal_max_turns", None),
            initial_status=getattr(args, "initial_status", "running"),
            epic_id=getattr(args, "epic_id", None),
            kind=getattr(args, "kind", None),
            ui_impact=getattr(args, "ui_impact", None),
            # P1-S3: a standalone `kanban create` couples a scout to a resolved-critical
            # task (flag/tier-gated, idempotent; held/triage statuses defer inside).
            # Live smoke tasks and other no-file/non-code cards can opt out so they
            # exercise the intended worker lane directly instead of a code-recon scout.
            auto_scout=not bool(getattr(args, "no_auto_scout", False)),
        )
        # Subscribe-on-create: route this task's terminal-state notifications
        # to every configured home channel (same target as the dashboard
        # subscribe_home endpoint), so a CLI-created root — and its decompose
        # children, via inheritance — reaches the home channel without a manual
        # notify-subscribe. Idempotent; no home channels -> no-op; opt out with
        # --no-notify-home.
        if not getattr(args, "no_notify_home", False):
            _create_subscribe_home_channels(conn, task_id)
        task = kb.get_task(conn, task_id)
    if getattr(args, "json", False):
        _emit_json(_task_to_dict(task))
    else:
        print(f"Created {task_id}  ({task.status}, assignee={task.assignee or '-'})")

        # Warn when the task would sit in `ready` because no dispatcher is
        # present. Only warn on ready+assigned tasks — triage/todo are
        # expected to sit idle until promoted, and unassigned tasks
        # can't be dispatched. Skipped in --json mode so the stdout
        # stream stays strictly machine-parseable for callers (the JSON
        # response itself carries enough info for them to decide if
        # they want to check dispatcher presence separately).
        if task.status == "ready" and task.assignee:
            running, message = _check_dispatcher_presence()
            if not running and message:
                print(f"\n⚠  {message}", file=sys.stderr)
    return 0


def _dispatch_epic(args: argparse.Namespace) -> int:
    """Handle ``hermes kanban epic <create|list|show|close>`` (N-E3)."""
    sub = getattr(args, "epic_action", None) or "list"
    if sub == "create":
        return _cmd_epic_create(args)
    if sub in {"list", "ls"}:
        return _cmd_epic_list(args)
    if sub == "show":
        return _cmd_epic_show(args)
    if sub == "close":
        return _cmd_epic_close(args)
    print(f"kanban epic: unknown action {sub!r}", file=sys.stderr)
    return 2

def _cmd_epic_create(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        eid = kb.create_epic(conn, title=args.title, body=getattr(args, "body", None))
        epic = kb.get_epic(conn, eid)
    if getattr(args, "json", False):
        _emit_json(epic)
    else:
        print(f"Created epic {eid}  ({epic['title']})")
    return 0

def _cmd_epic_list(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        epics = kb.list_epics(conn, include_closed=not getattr(args, "open_only", False))
    if getattr(args, "json", False):
        _emit_json({"epics": epics, "count": len(epics)})
        return 0
    if not epics:
        print("No epics.")
        return 0
    for e in epics:
        cost = f"${e['cost_usd']:.2f}" if e.get("cost_usd") is not None else "—"
        print(
            f"{e['id']}  [{e['status']}]  {e['title']}  "
            f"({e['done_tasks']}/{e['task_count']} done, {cost})"
        )
    return 0

def _cmd_epic_show(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        epic = kb.get_epic(conn, args.epic_id)
    if epic is None:
        print(f"kanban epic: no such epic {args.epic_id!r}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        _emit_json(epic)
        return 0
    cost = f"${epic['cost_usd']:.2f}" if epic.get("cost_usd") is not None else "—"
    print(f"{epic['id']}  [{epic['status']}]  {epic['title']}")
    if epic.get("body"):
        print(f"  {epic['body']}")
    print(f"  tasks: {epic['done_tasks']}/{epic['task_count']} done  ·  cost: {cost}")
    for t in epic.get("tasks", []):
        print(f"    {t['id']}  [{t['status']}]  {t['title']}")
    return 0

def _cmd_epic_close(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        ok = kb.close_epic(conn, args.epic_id)
    if not ok:
        print(f"kanban epic: no such epic {args.epic_id!r}", file=sys.stderr)
        return 1
    print(f"Closed epic {args.epic_id}")
    return 0

def _cmd_swarm(args: argparse.Namespace) -> int:
    try:
        workers = [ks.parse_worker_arg(raw) for raw in (args.worker or [])]
    except ValueError as exc:
        print(f"kanban swarm: {exc}", file=sys.stderr)
        return 2
    if not workers:
        print("kanban swarm: at least one --worker is required", file=sys.stderr)
        return 2
    with kb.connect_closing() as conn:
        created = ks.create_swarm(
            conn,
            goal=args.goal,
            workers=workers,
            verifier_assignee=args.verifier,
            synthesizer_assignee=args.synthesizer,
            tenant=args.tenant,
            created_by=args.created_by or _profile_author(),
            priority=args.priority,
            idempotency_key=getattr(args, "idempotency_key", None),
        )
    if getattr(args, "json", False):
        _emit_json(created.as_dict())
    else:
        print(f"Swarm root: {created.root_id}")
        print("Workers: " + ", ".join(created.worker_ids))
        print(f"Verifier: {created.verifier_id}")
        print(f"Synthesizer: {created.synthesizer_id}")
    return 0

def _cmd_list(args: argparse.Namespace) -> int:
    assignee = args.assignee
    if args.mine and not assignee:
        assignee = _profile_author()
    with kb.connect_closing() as conn:
        # Cheap "mini-dispatch": recompute ready so list output reflects
        # dependencies that may have cleared since the last dispatcher tick.
        kb.recompute_ready(conn)
        tasks = kb.list_tasks(
            conn,
            assignee=assignee,
            created_by=args.created_by,
            status=args.status,
            tenant=args.tenant,
            session_id=args.session,
            include_archived=args.archived,
            order_by=getattr(args, "sort", None),
            workflow_template_id=args.workflow_template_id,
            current_step_key=args.current_step_key,
        )
    if getattr(args, "json", False):
        _emit_json([_task_to_dict(t) for t in tasks])
        return 0
    # Passive discoverability: when the user has multiple boards, surface
    # which one they're looking at in the list header. Single-board users
    # never see this — the feature stays invisible until you opt in.
    try:
        all_boards = kb.list_boards(include_archived=False)
    except Exception:
        all_boards = []
    if len(all_boards) > 1:
        current = kb.get_current_board()
        other_count = len(all_boards) - 1
        print(
            f"Board: {current} "
            f"({other_count} other board{'s' if other_count != 1 else ''} — "
            f"`hermes kanban boards list`)\n"
        )
    if not tasks:
        print("(no matching tasks)")
        return 0
    for t in tasks:
        print(_fmt_task_line(t))
    return 0

def _show_build_json_payload(
    task: Any,
    comments: Any,
    events: Any,
    parents: Any,
    children: Any,
    runs: Any,
    latest_summary: Any,
) -> dict[str, Any]:
    """Build the ``kanban show --json`` payload (pure; no I/O)."""
    return {
        "task": _task_to_dict(task),
        "ui_impact": task.ui_impact,
        "effective_ui_impact": kb.effective_ui_impact(task),
        "latest_summary": latest_summary,
        "parents": parents,
        "children": children,
        "comments": [
            {"author": c.author, "body": c.body, "created_at": c.created_at}
            for c in comments
        ],
        "events": [
            {
                "kind": e.kind,
                "payload": e.payload,
                "created_at": e.created_at,
                "run_id": e.run_id,
            }
            for e in events
        ],
        "runs": [
            {
                "id": r.id,
                "profile": r.profile,
                "step_key": r.step_key,
                "status": r.status,
                "outcome": r.outcome,
                "summary": r.summary,
                "error": r.error,
                "metadata": r.metadata,
                "worker_pid": r.worker_pid,
                "started_at": r.started_at,
                "ended_at": r.ended_at,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost_usd": r.cost_usd,
            }
            for r in runs
        ],
    }


def _show_print_header(task: Any) -> None:
    """Print task identity / status / workspace / max-retries header fields."""
    print(f"Task {task.id}: {task.title}")
    print(f"  status:    {task.status}")
    print(f"  assignee:  {task.assignee or '-'}")
    if task.tenant:
        print(f"  tenant:    {task.tenant}")
    print(f"  workspace: {task.workspace_kind}" +
          (f" @ {task.workspace_path}" if task.workspace_path else ""))
    if task.branch_name:
        print(f"  branch:    {task.branch_name}")
    if task.skills:
        print(f"  skills:    {', '.join(task.skills)}")
    if task.model_override:
        print(f"  model:     {task.model_override}")
    if task.ui_impact:
        print(f"  ui-impact: {task.ui_impact} → {kb.effective_ui_impact(task)}")
    # Effective retry threshold. Show the per-task override if set,
    # otherwise the dispatcher's resolved value from config (or the
    # default if config doesn't set it either). Helps operators see
    # why a task auto-blocked earlier/later than they expected.
    if task.max_retries is not None:
        print(f"  max-retries: {task.max_retries} (task)")
    else:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            cfg_val = (cfg.get("kanban", {}) or {}).get("failure_limit")
        except Exception:
            cfg_val = None
        if cfg_val is not None and int(cfg_val) != kb.DEFAULT_FAILURE_LIMIT:
            print(f"  max-retries: {int(cfg_val)} (config kanban.failure_limit)")
        else:
            print(f"  max-retries: {kb.DEFAULT_FAILURE_LIMIT} (default)")
    print(f"  created:   {_fmt_ts(task.created_at)} by {task.created_by or '-'}")
    if task.due_at is not None:
        print(f"  due:       {_fmt_ts(task.due_at)}")


def _show_print_diagnostics(task: Any, events: Any, runs: Any) -> None:
    """Print the diagnostics block used by human-readable ``show`` output."""
    # Diagnostics section — surface active distress signals at the top
    # of show output so CLI users see them before scrolling through
    # comments / runs.
    from hermes_cli import kanban_diagnostics as kd
    diags = kd.compute_task_diagnostics(task, events, runs)
    if diags:
        sev_marker = {"warning": "⚠", "error": "!!", "critical": "!!!"}
        print(f"\n  Diagnostics ({len(diags)}):")
        for d in diags:
            print(f"    {sev_marker.get(d.severity, '?')} [{d.severity}] {d.title}")
            if d.data:
                bits = []
                for k, v in d.data.items():
                    if isinstance(v, list):
                        bits.append(f"{k}={','.join(str(x) for x in v)}")
                    else:
                        bits.append(f"{k}={v}")
                if bits:
                    print(f"       data: {' | '.join(bits)}")
            # Only show suggested actions in show output to keep it tight;
            # full list is available via `kanban diagnostics --task <id>`.
            for a in d.actions:
                if a.suggested:
                    print(f"       → {a.label}")


def _show_print_timeline(
    task: Any,
    parents: Any,
    children: Any,
    latest_summary: Any,
    comments: Any,
    events: Any,
    runs: Any,
) -> None:
    """Print started/completed, parents/children, body/result, comments/events/runs."""
    if task.started_at:
        print(f"  started:   {_fmt_ts(task.started_at)}")
    if task.completed_at:
        print(f"  completed: {_fmt_ts(task.completed_at)}")
    if parents:
        print(f"  parents:   {', '.join(parents)}")
    if children:
        print(f"  children:  {', '.join(children)}")
    if task.body:
        print()
        print("Body:")
        print(task.body)
    if task.result:
        print()
        print("Result:")
        print(task.result)
    elif latest_summary:
        # Worker handoff lives on the latest run, not on tasks.result.
        # Surface it at top-level so a glance at ``hermes kanban show <id>``
        # tells you what the worker did even if tasks.result is empty.
        print()
        print("Latest summary:")
        print(latest_summary)
    if comments:
        print()
        print(f"Comments ({len(comments)}):")
        for c in comments:
            print(f"  [{_fmt_ts(c.created_at)}] {c.author}: {c.body}")
    if events:
        print()
        print(f"Events ({len(events)}):")
        for e in events[-20:]:
            pl = f" {e.payload}" if e.payload else ""
            run_tag = f" [run {e.run_id}]" if e.run_id else ""
            print(f"  [{_fmt_ts(e.created_at)}]{run_tag} {e.kind}{pl}")
    if runs:
        print()
        print(f"Runs ({len(runs)}):")
        for r in runs:
            # Clamp to 0 so NTP backward-jumps don't print negative seconds.
            elapsed = (max(0, r.ended_at - r.started_at)
                       if r.ended_at else None)
            el = f"{elapsed}s" if elapsed is not None else "active"
            outcome = r.outcome or r.status or "active"
            print(f"  #{r.id:<3} {outcome:<12} @{r.profile or '-'}  {el}  "
                  f"{_fmt_ts(r.started_at)}")
            if r.summary:
                print(f"        → {r.summary.splitlines()[0][:160]}")
            if r.error:
                print(f"        ! {r.error.splitlines()[0][:160]}")


def _cmd_show(args: argparse.Namespace) -> int:
    rsk = _run_state_kwargs(args)
    if rsk is None:
        print(
            "kanban show: pass both --state-type and --state-name, or omit both",
            file=sys.stderr,
        )
        return 2
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, args.task_id)
        if not task:
            print(f"no such task: {args.task_id}", file=sys.stderr)
            return 1
        comments = kb.list_comments(conn, args.task_id)
        events = kb.list_events(conn, args.task_id)
        parents = kb.parent_ids(conn, args.task_id)
        children = kb.child_ids(conn, args.task_id)
        runs = kb.list_runs(conn, args.task_id, **rsk)
        # Workers hand off via ``task_runs.summary``; ``tasks.result`` is left NULL unless the caller explicitly passed
        # ``result=``. Surfacing the latest summary here keeps ``show`` from
        # looking like a no-op when the worker actually did real work.
        latest_summary = kb.latest_summary(conn, args.task_id)

    if getattr(args, "json", False):
        payload = _show_build_json_payload(
            task, comments, events, parents, children, runs, latest_summary,
        )
        _emit_json(payload)
        return 0

    _show_print_header(task)
    _show_print_diagnostics(task, events, runs)
    _show_print_timeline(
        task, parents, children, latest_summary, comments, events, runs,
    )
    return 0


def _cmd_assign(args: argparse.Namespace) -> int:
    profile = None if args.profile.lower() in {"none", "-", "null"} else args.profile
    if profile:
        try:
            profile = kb.validate_spawnable_assignee(profile)
        except ValueError as exc:
            _kanban_err(exc)
            return 2
    with kb.connect_closing() as conn:
        ok = kb.assign_task(conn, args.task_id, profile)
    if not ok:
        print(f"no such task: {args.task_id}", file=sys.stderr)
        return 1
    print(f"Assigned {args.task_id} to {profile or '(unassigned)'}")
    return 0

def _cmd_reclaim(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        ok = kb.reclaim_task(
            conn, args.task_id,
            reason=getattr(args, "reason", None),
        )
    if not ok:
        print(
            f"cannot reclaim {args.task_id} (not running or unknown id)",
            file=sys.stderr,
        )
        return 1
    print(f"Reclaimed {args.task_id}")
    return 0

def _cmd_reassign(args: argparse.Namespace) -> int:
    profile = None if args.profile.lower() in {"none", "-", "null"} else args.profile
    if profile:
        try:
            profile = kb.validate_spawnable_assignee(profile)
        except ValueError as exc:
            _kanban_err(exc)
            return 2
    with kb.connect_closing() as conn:
        ok = kb.reassign_task(
            conn, args.task_id, profile,
            reclaim_first=bool(getattr(args, "reclaim", False)),
            reason=getattr(args, "reason", None),
        )
    if not ok:
        print(
            f"cannot reassign {args.task_id} "
            f"(unknown id, or still running — pass --reclaim to release first)",
            file=sys.stderr,
        )
        return 1
    print(
        f"Reassigned {args.task_id} to "
        f"{profile or '(unassigned)'}"
        + (" (claim reclaimed)" if getattr(args, "reclaim", False) else "")
    )
    return 0

def _diagnostics_collect(
    args: argparse.Namespace,
    conn: Any,
    diag_config: Any,
    kd: Any,
) -> tuple[dict[str, Any], dict[str, dict]]:
    """Compute diagnostics_by_task + meta under an open connection.

    Pure extraction of the DB/collection phase of ``_cmd_diagnostics``.
    Missing-task early exit raises ``_DiagnosticsEarlyExit``.
    """
    # Either one-task mode or fleet mode.
    if getattr(args, "task", None):
        task = kb.get_task(conn, args.task)
        if task is None:
            raise _DiagnosticsEarlyExit(
                1, f"no such task: {args.task}",
            )
        diags_by_task = {
            args.task: kd.compute_task_diagnostics(
                task,
                kb.list_events(conn, args.task),
                kb.list_runs(conn, args.task),
                config=diag_config,
            )
        }
    else:
        # Fleet mode: pull all non-archived tasks + their events/runs.
        rows = list(conn.execute(
            "SELECT * FROM tasks WHERE status != 'archived'"
        ).fetchall())
        ids = [r["id"] for r in rows]
        if not ids:
            diags_by_task = {}
        else:
            placeholders = ",".join(["?"] * len(ids))
            ev_by = {i: [] for i in ids}
            for row in conn.execute(
                f"SELECT * FROM task_events WHERE task_id IN ({placeholders}) ORDER BY id",
                tuple(ids),
            ):
                ev_by.setdefault(row["task_id"], []).append(row)
            run_by = {i: [] for i in ids}
            for row in conn.execute(
                f"SELECT * FROM task_runs WHERE task_id IN ({placeholders}) ORDER BY id",
                tuple(ids),
            ):
                run_by.setdefault(row["task_id"], []).append(row)
            diags_by_task = {}
            for r in rows:
                tid = r["id"]
                dl = kd.compute_task_diagnostics(
                    r,
                    ev_by.get(tid, []),
                    run_by.get(tid, []),
                    config=diag_config,
                )
                if dl:
                    diags_by_task[tid] = dl

    # K4 cross-task pass: flag todo tasks stranded by a sticky-blocked
    # parent. The per-task engine above is graph-blind, so this runs as a
    # board-level pass with the open conn. Read-only and fail-soft — a
    # diagnostics hiccup must never break the command.
    try:
        cross = kd.find_descendants_blocked_by_stuck_parent(
            conn, config=diag_config,
        )
        root_branch_diags = kd.find_stranded_decompose_root_branches(conn)
        for tid, dl in root_branch_diags.items():
            cross.setdefault(tid, []).extend(dl)
        focus = getattr(args, "task", None)
        for tid, dl in cross.items():
            if focus and tid != focus:
                continue
            diags_by_task.setdefault(tid, []).extend(dl)
    except Exception:
        pass

    # Severity filter.
    sev = getattr(args, "severity", None)
    if sev:
        for tid in list(diags_by_task.keys()):
            kept = [d for d in diags_by_task[tid] if kd.SEVERITY_ORDER.index(d.severity) >= kd.SEVERITY_ORDER.index(sev)]
            if kept:
                diags_by_task[tid] = kept
            else:
                del diags_by_task[tid]

    # Map task_id → title/status/assignee for the table output.
    meta: dict[str, dict] = {}
    if diags_by_task:
        placeholders = ",".join(["?"] * len(diags_by_task))
        for r in conn.execute(
            f"SELECT id, title, status, assignee FROM tasks WHERE id IN ({placeholders})",
            tuple(diags_by_task.keys()),
        ):
            meta[r["id"]] = {
                "title": r["title"], "status": r["status"],
                "assignee": r["assignee"],
            }
    return diags_by_task, meta


class _DiagnosticsEarlyExit(Exception):
    """Internal control-flow for diagnostics collect early returns."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _diagnostics_print_human(
    diags_by_task: dict[str, Any],
    meta: dict[str, dict],
) -> int:
    """Render human-readable diagnostics table. Returns exit code."""
    if not diags_by_task:
        print("No active diagnostics on this board.")
        return 0

    # Human-readable summary: grouped by task, severity-marked, with
    # suggested actions inline.
    sev_marker = {"warning": "⚠", "error": "!!", "critical": "!!!"}
    total = sum(len(dl) for dl in diags_by_task.values())
    print(
        f"{total} active diagnostic(s) across "
        f"{len(diags_by_task)} task(s):\n"
    )
    for tid, dl in diags_by_task.items():
        m = meta.get(tid, {})
        title = m.get("title") or "(untitled)"
        status = m.get("status") or "?"
        assignee = m.get("assignee") or "(unassigned)"
        print(f"  {tid}  {status:8s}  @{assignee:18s}  {title}")
        for d in dl:
            print(f"    {sev_marker.get(d.severity, '?')} [{d.severity}] {d.kind}: {d.title}")
            if d.data:
                # Compact key:value pairs on one line.
                bits = []
                for k, v in d.data.items():
                    if isinstance(v, list):
                        bits.append(f"{k}={','.join(str(x) for x in v)}")
                    else:
                        bits.append(f"{k}={v}")
                if bits:
                    print(f"       data: {' | '.join(bits)}")
            # Suggested actions first.
            for a in d.actions:
                if a.suggested:
                    print(f"       → {a.label}")
        print()
    return 0


def _cmd_diagnostics(args: argparse.Namespace) -> int:
    """List active diagnostics on the board. Wraps the same rule engine
    the dashboard uses, so CLI output matches what the UI shows.
    """
    from hermes_cli import kanban_diagnostics as kd
    from hermes_cli.config import load_config

    diag_config = kd.config_from_runtime_config(load_config())

    with kb.connect_closing() as conn:
        try:
            diags_by_task, meta = _diagnostics_collect(args, conn, diag_config, kd)
        except _DiagnosticsEarlyExit as early:
            print(early.message, file=sys.stderr)
            return early.code

    if getattr(args, "json", False):
        out_json = [
            {
                "task_id": tid,
                **meta.get(tid, {}),
                "diagnostics": [d.to_dict() for d in dl],
            }
            for tid, dl in diags_by_task.items()
        ]
        _emit_json(out_json)
        return 0

    return _diagnostics_print_human(diags_by_task, meta)


def _cmd_link(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        kb.link_tasks(conn, args.parent_id, args.child_id)
    print(f"Linked {args.parent_id} -> {args.child_id}")
    return 0

def _cmd_unlink(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        ok = kb.unlink_tasks(conn, args.parent_id, args.child_id)
    if not ok:
        print(f"No such link: {args.parent_id} -> {args.child_id}", file=sys.stderr)
        return 1
    print(f"Unlinked {args.parent_id} -> {args.child_id}")
    return 0

def _cmd_claim(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        task = kb.claim_task(conn, args.task_id, ttl_seconds=args.ttl)
        if task is None:
            # Report why
            existing = kb.get_task(conn, args.task_id)
            if existing is None:
                print(f"no such task: {args.task_id}", file=sys.stderr)
                return 1
            print(
                f"cannot claim {args.task_id}: status={existing.status} "
                f"lock={existing.claim_lock or '(none)'}",
                file=sys.stderr,
            )
            return 1
        workspace = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, task.id, str(workspace))
    print(f"Claimed {task.id}")
    print(f"Workspace: {workspace}")
    return 0

def _cmd_comment(args: argparse.Namespace) -> int:
    kind = "comment"
    if getattr(args, "directive", False):
        # Cage model: a spawned worker (HERMES_KANBAN_TASK set in its env) must
        # not be able to grant itself an operator directive that overrides its
        # own task body. Only an interactive operator (no task env) may land
        # one. See build_worker_context / _render_comment_thread.
        if os.environ.get("HERMES_KANBAN_TASK"):
            print(
                "kanban: --directive is operator-only and cannot be set from "
                "inside a spawned worker (HERMES_KANBAN_TASK is set)",
                file=sys.stderr,
            )
            return 2
        kind = "directive"
    body = " ".join(args.text).strip()
    if args.max_len is not None:
        if args.max_len < 1:
            print("kanban: --max-len must be positive", file=sys.stderr)
            return 2
        if len(body) > args.max_len:
            suffix = f"\n\n[trimmed to {args.max_len} chars by --max-len]"
            body = body[: max(0, args.max_len - len(suffix))].rstrip() + suffix
    author = args.author or _profile_author()
    with kb.connect_closing() as conn:
        kb.add_comment(conn, args.task_id, author, body, kind=kind)
    label = "Directive" if kind == "directive" else "Comment"
    print(f"{label} added to {args.task_id}")
    return 0

def _worker_run_id_for(task_id: str) -> Optional[int]:
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None

def _latest_completed_metadata(
    conn,
    task_id: str,
    *,
    label: str,
) -> dict[str, Any]:
    task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"{label} task not found: {task_id}")
    run = kb.latest_run(conn, task_id)
    if run is None or run.outcome != "completed":
        raise ValueError(f"{label} task has no completed run: {task_id}")
    if not isinstance(run.metadata, dict):
        raise ValueError(f"{label} metadata object is required: {task_id}")
    return run.metadata

def _cmd_review_commit(args: argparse.Namespace) -> int:
    """Opt-in local scoped commit after Coder handoff and Reviewer-B verdict."""
    try:
        with kb.connect() as conn:
            coder_metadata = _latest_completed_metadata(
                conn,
                args.coder_task_id,
                label="coder",
            )
            reviewer_metadata = _latest_completed_metadata(
                conn,
                args.reviewer_task,
                label="reviewer",
            )

        acceptance_checks = (
            coder_metadata.get("acceptance_checks")
            or reviewer_metadata.get("acceptance_checks")
        )
        anti_scope = coder_metadata.get("anti_scope") or reviewer_metadata.get("anti_scope")
        expected_workflow_id = coder_metadata.get("workflow_id") or reviewer_metadata.get("workflow_id")
        receipt = create_scoped_local_commit(
            repo_path=args.repo,
            scoped_paths=args.scoped_path,
            message=args.message,
            coder_metadata=coder_metadata,
            reviewer_metadata=reviewer_metadata,
            acceptance_checks=acceptance_checks,
            anti_scope=anti_scope,
            expected_workflow_id=str(expected_workflow_id) if expected_workflow_id else None,
        )
    except (PermissionError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"review-commit blocked: {exc}", file=sys.stderr)
        return 1

    payload = {
        "commit_hash": receipt.commit_hash,
        "committed_paths": receipt.committed_paths,
        "preexisting_dirty_paths": receipt.preexisting_dirty_paths,
        "reviewer_verdict": receipt.reviewer_verdict,
        "pushed": False,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Review commit created {receipt.commit_hash}")
        print("Committed paths: " + ", ".join(receipt.committed_paths))
        if receipt.preexisting_dirty_paths:
            print("Pre-existing dirty left untouched: " + ", ".join(receipt.preexisting_dirty_paths))
        print("Push: not executed")
    return 0

def _complete_parse_metadata(raw_meta: Any) -> tuple[Any, int | None]:
    """Parse ``--metadata`` JSON. Returns ``(metadata, error_exit_or_None)``."""
    if not raw_meta:
        return None, None
    try:
        metadata = json.loads(raw_meta)
        if not isinstance(metadata, dict):
            raise ValueError("must be a JSON object")
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"kanban: --metadata: {exc}", file=sys.stderr)
        return None, 2
    return metadata, None


def _complete_one_task(
    conn: Any,
    tid: str,
    *,
    result: Any,
    summary: Any,
    metadata: Any,
) -> bool:
    """Complete a single task. Prints status lines. Returns True on success."""
    # Goal-mode pre-completion judge gate (Issue #38367), the CLI
    # side of the SAME gate the kanban_complete model tool enforces
    # (tools/kanban_tools.py:_handle_complete) — the documented
    # worker completion path invokes `hermes kanban complete` (or
    # the equivalent claude-CLI lifecycle bridge) directly against
    # kb.complete_task, bypassing the tool entirely, so a goal_mode
    # worker could self-certify without ever hitting the tool gate.
    # SHARED with the tool via hermes_cli.goals.check_goal_mode_completion
    # so the two enforcement points can't diverge.
    #
    # Scoped NARROWLY to a worker closing its OWN task
    # (HERMES_KANBAN_TASK == tid): an operator running `hermes kanban
    # complete` by hand (e.g. dispositioning a task as done-elsewhere)
    # has no worker-env marker and must stay ungated — the judge is a
    # guard against a worker self-certifying, not an operator override.
    if os.environ.get("HERMES_KANBAN_TASK") == tid:
        task_for_gate = kb.get_task(conn, tid)
        if task_for_gate and task_for_gate.goal_mode:
            rejection = check_goal_mode_completion(
                task_id=tid,
                task_title=task_for_gate.title,
                task_body=task_for_gate.body,
                handoff_text=(summary or result or ""),
            )
            if rejection:
                print(f"kanban: {rejection}", file=sys.stderr)
                return False
    # Worker-context completions (the claude-CLI lifecycle bridge
    # reports back via this verb) must hit the same review gate as the
    # in-process kanban_complete tool — otherwise a claude-cli worker
    # in a code-bearing role (e.g. premium) bypasses the verifier.
    # Operator CLI completions (no HERMES_KANBAN_TASK/RUN_ID env)
    # keep the direct 'done' path by design.
    worker_run_id = _worker_run_id_for(tid)
    try:
        completed = kb.complete_task(
            conn, tid,
            result=result,
            summary=summary,
            metadata=metadata,
            expected_run_id=worker_run_id,
            review_gate=worker_run_id is not None,
        )
    except kb.ReviewVerdictRequiredError as exc:
        _kanban_err(exc)
        return False
    if not completed:
        print(f"cannot complete {tid} (unknown id or terminal state)", file=sys.stderr)
        return False
    print(f"Completed {tid}")
    return True


def _cmd_complete(args: argparse.Namespace) -> int:
    """Mark one or more tasks done. Supports a single id or a list."""
    ids = list(args.task_ids or [])
    if not ids:
        print("at least one task_id is required", file=sys.stderr)
        return 1
    summary = getattr(args, "summary", None)
    raw_meta = getattr(args, "metadata", None)
    # Guard: structured handoff fields are per-run, so they'd be
    # copy-pasted identically across N runs — almost always a footgun.
    # Refuse instead of silently doing the wrong thing.
    if len(ids) > 1 and (summary or raw_meta):
        print(
            "kanban: --summary / --metadata are per-task and can't be used "
            "with multiple ids (would apply the same handoff to every task). "
            "Complete tasks one at a time, or drop the flags for the bulk close.",
            file=sys.stderr,
        )
        return 2
    metadata, meta_err = _complete_parse_metadata(raw_meta)
    if meta_err is not None:
        return meta_err
    # Versioned-bundle gate (PlanSpec C landing): mirror the in-process
    # kanban_complete tool so the claude-CLI worker lane (premium/coder-claude)
    # gets the same in-flight rejection. When the worker opts into the structured
    # bundle (metadata carries schema_version) it must be complete; otherwise we
    # echo the missing fields back and DO NOT complete the task (no state change),
    # so the worker re-runs `complete` with the fields instead of landing a
    # half-filled bundle that a downstream gate would auto-block.
    if metadata is not None:
        from hermes_cli.disposition import validate_completion_bundle

        bundle_missing = validate_completion_bundle(metadata)
        if bundle_missing:
            print(
                "kanban: completion bundle missing required field(s): "
                + ", ".join(bundle_missing)
                + ". Task is still in-flight (no state change). Add the field(s) "
                "to --metadata and run `complete` again. Required bundle shape: "
                '{"schema_version": 1, "gates": {"exit_code": 0}, '
                '"AC": "<how each AC is met>", "residual_risk": "<one line>"}.',
                file=sys.stderr,
            )
            return 2
    failed: list[str] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            ok = _complete_one_task(
                conn, tid,
                result=args.result,
                summary=summary,
                metadata=metadata,
            )
            if not ok:
                failed.append(tid)
    return 0 if not failed else 1


def _cmd_edit(args: argparse.Namespace) -> int:
    raw_meta = getattr(args, "metadata", None)
    metadata = None
    if raw_meta:
        try:
            metadata = json.loads(raw_meta)
            if not isinstance(metadata, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"kanban: --metadata: {exc}", file=sys.stderr)
            return 2
    with kb.connect_closing() as conn:
        if not kb.edit_completed_task_result(
            conn,
            args.task_id,
            result=args.result,
            summary=getattr(args, "summary", None),
            metadata=metadata,
        ):
            print(
                f"cannot edit {args.task_id} (unknown id or task is not done)",
                file=sys.stderr,
            )
            return 1
    print(f"Edited {args.task_id}")
    return 0

def _cmd_respec(args: argparse.Namespace) -> int:
    body = getattr(args, "body", None)
    body_file = getattr(args, "body_file", None)
    ac = getattr(args, "ac", None)
    if body_file:
        try:
            body = Path(body_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"kanban: --body-file: {exc}", file=sys.stderr)
            return 2
    if body is None and ac is None:
        print(
            "kanban: respec needs --body, --body-file, and/or --ac",
            file=sys.stderr,
        )
        return 2
    author = getattr(args, "author", None) or _profile_author()
    # A ValueError from a malformed --ac propagates to the dispatch wrapper,
    # which prints "kanban: <msg>" and returns 1 — a sprechende CLI-Meldung.
    with kb.connect_closing() as conn:
        new_id = kb.respec_task(
            conn,
            args.task_id,
            body=body,
            title=getattr(args, "title", None),
            acceptance_criteria=ac,
            author=author,
        )
        if not new_id:
            print(
                f"cannot respec {args.task_id} (unknown id, or task is "
                f"running/review/done/archived — only triage/todo/scheduled/"
                f"ready/blocked are replaceable)",
                file=sys.stderr,
            )
            return 1
    print(new_id)
    return 0

def _cmd_block(args: argparse.Namespace) -> int:
    reason = " ".join(args.reason).strip() if args.reason else None
    kind = getattr(args, "kind", None)
    author = _profile_author()
    ids = [args.task_id] + list(getattr(args, "ids", None) or [])
    failed: list[str] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            if reason:
                kb.add_comment(conn, tid, author, f"BLOCKED: {reason}")
            if not kb.block_task(
                conn,
                tid,
                reason=reason,
                kind=kind,
                wait_for=getattr(args, "wait_for", None),
                expected_run_id=_worker_run_id_for(tid),
            ):
                failed.append(tid)
                print(f"cannot block {tid}", file=sys.stderr)
            else:
                # Report where the task actually landed — a 'dependency'
                # kind routes to todo, and a tripped unblock-loop breaker
                # (see kb.block_task / BLOCK_RECURRENCE_LIMIT) routes to
                # triage — so 'Blocked' alone would misreport those cases.
                landed = kb.get_task(conn, tid)
                where = landed.status if landed else "blocked"
                suffix = f": {reason}" if reason else ""
                if where == "todo":
                    print(f"{tid} → todo (dependency wait){suffix}")
                elif where == "triage":
                    print(
                        f"{tid} → triage (unblock loop detected — needs a "
                        f"human decision){suffix}"
                    )
                else:
                    print(f"Blocked {tid}{suffix}")
    return 0 if not failed else 1

def _cmd_set_workflow(args: argparse.Namespace) -> int:
    from hermes_cli.kanban_workflows import load_workflow_template

    tmpl = load_workflow_template(args.template_id)
    if tmpl is None:
        print(f"unknown workflow template: {args.template_id}", file=sys.stderr)
        return 1
    first = tmpl.first_step_key()
    if not first:
        print(f"workflow template has no steps: {args.template_id}", file=sys.stderr)
        return 1
    with kb.connect_closing() as conn:
        ok = kb.set_task_workflow(
            conn,
            args.task_id,
            template_id=args.template_id,
            first_step_key=first,
        )
    if ok:
        print(f"set workflow {args.template_id} on {args.task_id} (step {first})")
        return 0
    print(f"cannot set workflow on {args.task_id}", file=sys.stderr)
    return 1

def _cmd_schedule(args: argparse.Namespace) -> int:
    reason = " ".join(args.reason).strip() if args.reason else None
    author = _profile_author()
    ids = [args.task_id] + list(getattr(args, "ids", None) or [])
    failed: list[str] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            if not kb.schedule_task(
                conn,
                tid,
                reason=reason,
                due_at=getattr(args, "due", None),
                expected_run_id=_worker_run_id_for(tid),
            ):
                failed.append(tid)
                print(f"cannot schedule {tid}", file=sys.stderr)
            else:
                if reason:
                    kb.add_comment(conn, tid, author, f"SCHEDULED: {reason}")
                print(f"Scheduled {tid}" + (f": {reason}" if reason else ""))
    return 0 if not failed else 1

def _cmd_unblock(args: argparse.Namespace) -> int:
    ids = list(args.task_ids or [])
    if not ids:
        print("at least one task_id is required", file=sys.stderr)
        return 1
    reason = getattr(args, "reason", None)
    override_wait = bool(getattr(args, "override_wait", False))
    if override_wait and os.environ.get("HERMES_KANBAN_TASK"):
        print("--override-wait is operator-only", file=sys.stderr)
        return 1
    if override_wait and (not reason or not str(reason).strip()):
        print("--override-wait requires --reason", file=sys.stderr)
        return 1
    if reason is not None:
        reason = reason.strip() or None
    author = _profile_author() if reason else None
    failed: list[str] = []
    try:
        from hermes_cli.config import load_config

        config = load_config()
        kanban_config = config.get("kanban", {}) if isinstance(config, dict) else {}
    except Exception:
        kanban_config = {}
    per_task_input_token_cap = kanban_config.get("per_task_input_token_cap")
    with kb.connect_closing() as conn:
        for tid in ids:
            refusal = None if args.force else kb.budget_runaway_unblock_refusal(
                conn,
                tid,
                per_task_input_token_cap=per_task_input_token_cap,
            )
            if refusal:
                failed.append(tid)
                print(
                    f"Refusing to unblock {tid}: exhausted budget-runaway park "
                    f"(input_token_sum={refusal['input_token_sum']}, cap={refusal['cap']}, "
                    "budget_extension_count="
                    f"{refusal['budget_extension_count']}/{refusal['budget_extension_limit']}). "
                    "Use --force to retain the previous unblock behavior, create a fresh bounded "
                    "task, respec this task, or raise kanban.per_task_input_token_cap in config.",
                    file=sys.stderr,
                )
                continue
            if not kb.unblock_task(
                conn,
                tid,
                override_wait=override_wait,
                actor=author,
                reason=reason,
            ):
                failed.append(tid)
                print(
                    f"cannot unblock {tid} (not blocked/scheduled, or active wait?)",
                    file=sys.stderr,
                )
            else:
                if reason:
                    kb.add_comment(conn, tid, author, f"UNBLOCK: {reason}")
                print(f"Unblocked {tid}" + (f": {reason}" if reason else ""))
    return 0 if not failed else 1

def _cmd_promote(args: argparse.Namespace) -> int:
    reason = " ".join(args.reason).strip() if args.reason else None
    author = _profile_author()
    as_json = getattr(args, "json", False)
    extra_ids = list(getattr(args, "ids", None) or [])
    # Dedupe while preserving order; positional task_id always first.
    ids: list[str] = []
    seen: set[str] = set()
    for tid in [args.task_id, *extra_ids]:
        if tid not in seen:
            ids.append(tid)
            seen.add(tid)

    results: list[dict[str, object]] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            ok, err = kb.promote_task(
                conn,
                tid,
                actor=author,
                reason=reason,
                force=bool(args.force),
                dry_run=bool(args.dry_run),
            )
            results.append({
                "task_id": tid,
                "promoted": ok,
                "dry_run": bool(args.dry_run),
                "forced": bool(args.force),
                "reason": reason,
                "error": err,
            })

    failed = [r for r in results if not r["promoted"]]
    if as_json:
        # Single-id stays a flat object for back-compat; bulk emits a list.
        payload: object = results[0] if len(results) == 1 else results
        _emit_json(payload)
        return 0 if not failed else 1

    tag = " (dry)" if args.dry_run else ""
    label = "Would promote" if args.dry_run else "Promoted"
    for r in results:
        if r["promoted"]:
            suffix = f": {reason}" if reason else ""
            print(f"{label} {r['task_id']} -> ready{tag}{suffix}")
        else:
            print(f"cannot promote {r['task_id']}: {r['error']}", file=sys.stderr)
    return 0 if not failed else 1

def _cmd_report_discord(args: argparse.Namespace) -> int:
    """Render a read-only Discord report with a kanban-report-v1 JSON block."""
    from hermes_cli import kanban_discord_report as kdr

    statuses: list[str] = []
    for raw in getattr(args, "status", None) or []:
        statuses.extend(part.strip() for part in str(raw).split(",") if part.strip())
    with kb.connect_closing() as conn:
        report = kdr.build_discord_report(
            conn,
            board=kb.get_current_board(),
            generated_at=getattr(args, "generated_at", None),
            report_title=getattr(args, "title", None),
            scope_description=getattr(args, "scope_description", None),
            root_task_id=getattr(args, "root_task", None),
            included_statuses=statuses or None,
            include_archived=bool(getattr(args, "include_archived", False)),
            summary=getattr(args, "summary", None),
        )

    full_markdown = kdr.render_discord_report_markdown(report)
    output_path_raw = getattr(args, "output", None)
    if output_path_raw:
        output_path = Path(output_path_raw).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(full_markdown, encoding="utf-8")

    if getattr(args, "json", False):
        _emit_json(report["structured"])
        return 0

    if getattr(args, "chunks", False):
        chunks = kdr.split_discord_report(
            report,
            soft_cap=getattr(args, "soft_cap", 1800),
            artifact_path=getattr(args, "artifact_path", None) or output_path_raw,
        )
        print(
            json.dumps(
                {
                    "contract_version": 1,
                    "soft_cap": int(getattr(args, "soft_cap", 1800)),
                    "chunk_count": len(chunks),
                    "chunks": chunks,
                    "artifact_path": getattr(args, "artifact_path", None) or output_path_raw,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    print(full_markdown, end="")
    return 0

def _cmd_close_sprint(args: argparse.Namespace) -> int:
    """Aggregate kid receipts into a SPRINT CLOSURE comment and complete
    the parent.  See ``hermes_cli/kanban_close_sprint.py`` for shape.
    """
    from hermes_cli import kanban_close_sprint as kcs

    if args.auto_summary and args.comment_file:
        print(
            "kanban close-sprint: --auto-summary and --comment are mutually "
            "exclusive",
            file=sys.stderr,
        )
        return 2

    comment_override: Optional[str] = None
    if args.comment_file:
        try:
            comment_override = kcs.read_comment_file(args.comment_file)
        except OSError as exc:
            print(f"kanban close-sprint: cannot read --comment file: {exc}",
                  file=sys.stderr)
            return 2

    audit_author = _profile_author()
    as_json = bool(getattr(args, "json", False))

    if args.dry_run:
        with kb.connect_closing() as conn:
            parent = kb.get_task(conn, args.task_id)
            if parent is None:
                print(f"kanban close-sprint: unknown task {args.task_id}",
                      file=sys.stderr)
                return 1
            if comment_override is not None:
                body = comment_override
                kids = [kcs._kid_receipt(conn, k)
                        for k in kb.child_ids(conn, args.task_id)]
            else:
                body, kids = kcs.build_closure_comment(
                    conn, args.task_id, auto_summary=args.auto_summary,
                )
        if as_json:
            print(json.dumps({
                "task_id": args.task_id,
                "dry_run": True,
                "comment_body": body,
                "kid_count": len(kids),
            }, indent=2, ensure_ascii=False))
        else:
            print(body)
        return 0

    with kb.connect_closing() as conn:
        outcome = kcs.close_sprint(
            conn,
            args.task_id,
            auto_summary=args.auto_summary,
            comment_override=comment_override,
            author=audit_author,
            result=args.result,
            summary_override=args.summary,
            require_kids_done=not args.allow_open_kids,
        )

    if as_json:
        payload = {
            "task_id": outcome.parent_id,
            "ok": outcome.ok,
            "reason": outcome.reason,
            "completed": outcome.completed,
            "comment_id": outcome.comment_id,
            "kid_count": len(outcome.kid_receipts or []),
            "comment_body": outcome.comment_body,
        }
        _emit_json(payload)
        return 0 if outcome.ok and outcome.completed else 1

    if not outcome.ok:
        print(f"kanban close-sprint: {outcome.reason}", file=sys.stderr)
        return 1

    status_tag = "completed" if outcome.completed else "comment-only"
    print(
        f"Closed sprint {outcome.parent_id} "
        f"(comment_id={outcome.comment_id}, {status_tag}, "
        f"kids={len(outcome.kid_receipts or [])})"
    )
    return 0 if outcome.completed else 1

def _cmd_archive(args: argparse.Namespace) -> int:
    ids = list(args.task_ids or [])
    purge_ids = list(getattr(args, "purge_ids", None) or [])
    if ids and purge_ids:
        print("choose either task_ids to archive or --rm archived task_ids", file=sys.stderr)
        return 1
    if not ids and not purge_ids:
        print("at least one task_id is required", file=sys.stderr)
        return 1
    failed: list[str] = []
    with kb.connect_closing() as conn:
        if purge_ids:
            for tid in purge_ids:
                if not kb.delete_archived_task(conn, tid):
                    failed.append(tid)
                    print(f"cannot delete {tid} (must already be archived)", file=sys.stderr)
                else:
                    print(f"Deleted {tid}")
            return 0 if not failed else 1
        for tid in ids:
            if not kb.archive_task(conn, tid):
                failed.append(tid)
                print(f"cannot archive {tid}", file=sys.stderr)
            else:
                print(f"Archived {tid}")
    return 0 if not failed else 1

def _cmd_tail(args: argparse.Namespace) -> int:
    last_id = 0
    print(f"Tailing events for {args.task_id}. Ctrl-C to stop.")
    try:
        while True:
            with kb.connect_closing() as conn:
                events = kb.list_events(conn, args.task_id)
            for e in events:
                if e.id > last_id:
                    pl = f" {e.payload}" if e.payload else ""
                    print(f"[{_fmt_ts(e.created_at)}] {e.kind}{pl}", flush=True)
                    last_id = e.id
            time.sleep(max(0.1, args.interval))
    except KeyboardInterrupt:
        print("\n(stopped)")
        return 0

# ---------------------------------------------------------------------------
# Dispatch / holds / daemon
# ---------------------------------------------------------------------------

def _dispatch_load_kwargs(args: argparse.Namespace) -> tuple[dict[str, Any], bool, int]:
    """Resolve dispatch_kwargs + auto-retry settings from config / CLI flags.

    Returns ``(dispatch_kwargs, auto_retry_blocked, auto_retry_blocked_backoff_seconds)``.
    """
    # Honour kanban.default_assignee as the fallback for unassigned ready
    # tasks (#27145), kanban.max_in_progress as the global concurrency cap
    # (#33488), kanban.max_in_progress_per_profile as the per-profile
    # cap (#21582), and kanban.max_spawn as the per-tick spawn limit
    # (#28805). Same semantics as the gateway dispatch path so behavior
    # matches whether the user runs the CLI directly or relies on the
    # gateway-embedded dispatcher.
    try:
        from hermes_cli.config import load_config
        _cfg = load_config()
        _kanban_cfg = _cfg.get("kanban", {}) if isinstance(_cfg, dict) else {}
        dispatch_kwargs = kb.dispatch_kwargs_from_config(_kanban_cfg)
        # CLI --max overrides config kanban.max_spawn when both are present;
        # CLI is the more explicit signal so it wins.
        cli_max = getattr(args, "max", None)
        if cli_max is not None:
            dispatch_kwargs["max_spawn"] = cli_max
        auto_retry_blocked = _coerce_config_bool(
            _kanban_cfg.get("auto_retry_blocked", False), default=False
        )
        try:
            auto_retry_blocked_backoff_seconds = int(
                _kanban_cfg.get(
                    "auto_retry_blocked_backoff_seconds",
                    kb.DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS,
                )
            )
        except (TypeError, ValueError):
            auto_retry_blocked_backoff_seconds = kb.DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS
        if auto_retry_blocked_backoff_seconds < 0:
            auto_retry_blocked_backoff_seconds = kb.DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS
    except Exception:
        dispatch_kwargs = kb.dispatch_kwargs_from_config({})
        cli_max = getattr(args, "max", None)
        if cli_max is not None:
            dispatch_kwargs["max_spawn"] = cli_max
        auto_retry_blocked = False
        auto_retry_blocked_backoff_seconds = kb.DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS
    return dispatch_kwargs, auto_retry_blocked, auto_retry_blocked_backoff_seconds


def _dispatch_print_result(
    res: Any,
    args: argparse.Namespace,
    dispatch_kwargs: dict[str, Any],
) -> int:
    """Print dispatch_once result as JSON or human text. Returns exit code."""
    if getattr(args, "json", False):
        print(json.dumps({
            "reclaimed": res.reclaimed,
            "crashed": res.crashed,
            "timed_out": res.timed_out,
            "stale": res.stale,
            "auto_blocked": res.auto_blocked,
            "auto_retried_blocked": [
                {"task_id": tid, "attempt": attempt}
                for (tid, attempt) in res.auto_retried_blocked
            ],
            "promoted": res.promoted,
            "spawned": [
                {"task_id": tid, "assignee": who, "workspace": ws}
                for (tid, who, ws) in res.spawned
            ],
            "skipped_unassigned": res.skipped_unassigned,
            "skipped_nonspawnable": res.skipped_nonspawnable,
            "skipped_per_profile_capped": [
                {"task_id": tid, "assignee": who, "current": current}
                for (tid, who, current) in res.skipped_per_profile_capped
            ],
            "auto_assigned_default": res.auto_assigned_default,
        }, indent=2))
        return 0
    print(f"Reclaimed:    {res.reclaimed}")
    print(f"Crashed:      {len(res.crashed)}")
    if res.crashed:
        print(f"  {', '.join(res.crashed)}")
    print(f"Timed out:    {len(res.timed_out)}")
    if res.timed_out:
        print(f"  {', '.join(res.timed_out)}")
    print(f"Stale:        {len(res.stale)}")
    if res.stale:
        print(f"  {', '.join(res.stale)}")
    print(f"Auto-blocked: {len(res.auto_blocked)}")
    if res.auto_blocked:
        print(f"  {', '.join(res.auto_blocked)}")
    print(f"Auto-retried blocked: {len(res.auto_retried_blocked)}")
    for tid, attempt in res.auto_retried_blocked:
        print(f"  - {tid}  attempt {attempt}/{kb.DEFAULT_AUTO_RETRY_BLOCKED_LIMIT}")
    print(f"Promoted:     {res.promoted}")
    print(f"Spawned:      {len(res.spawned)}")
    for tid, who, ws in res.spawned:
        tag = " (dry)" if args.dry_run else ""
        print(f"  - {tid}  ->  {who}  @ {ws or '-'}{tag}")
    if res.auto_assigned_default:
        default_assignee = dispatch_kwargs.get("default_assignee")
        print(
            f"Auto-assigned to kanban.default_assignee={default_assignee!r}: "
            f"{', '.join(res.auto_assigned_default)}"
        )
    if res.skipped_unassigned:
        print(f"Skipped (unassigned): {', '.join(res.skipped_unassigned)}")
    if res.skipped_per_profile_capped:
        for tid, who, current in res.skipped_per_profile_capped:
            print(
                f"Deferred ({who} at per-profile cap, {current} running): {tid}"
            )
    if res.skipped_nonspawnable:
        print(
            f"Skipped (non-spawnable assignee — terminal lane, OK): "
            f"{', '.join(res.skipped_nonspawnable)}"
        )
    return 0


def _cmd_dispatch(args: argparse.Namespace) -> int:
    dispatch_kwargs, auto_retry_blocked, auto_retry_blocked_backoff_seconds = (
        _dispatch_load_kwargs(args)
    )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn,
            dry_run=args.dry_run,
            failure_limit=getattr(args, "failure_limit", kb.DEFAULT_SPAWN_FAILURE_LIMIT),
            auto_retry_blocked=auto_retry_blocked,
            auto_retry_blocked_backoff_seconds=auto_retry_blocked_backoff_seconds,
            **dispatch_kwargs,
        )
        if not args.dry_run:
            try:
                from hermes_cli import kanban_closeout as _closeout

                _closeout.spawn_pending_closeouts(
                    conn,
                    board=kb.board_slug_for_conn(conn),
                    limit=10,
                )
            except Exception:
                _log.debug("kanban dispatch: closeout spawn sweep failed", exc_info=True)
    return _dispatch_print_result(res, args, dispatch_kwargs)


def _cmd_holds(args: argparse.Namespace) -> int:
    """Handle ``hermes kanban holds [--json]``."""
    try:
        from hermes_cli.config import load_config

        config = load_config()
        kanban_config = config.get("kanban", {}) if isinstance(config, dict) else {}
    except Exception:
        kanban_config = {}
    dispatch_kwargs = kb.dispatch_kwargs_from_config(kanban_config)
    with kb.connect_closing(board=getattr(args, "board", None)) as conn:
        report = kb.list_dispatch_holds(
            conn,
            board=getattr(args, "board", None),
            **dispatch_kwargs,
        )
    if getattr(args, "json", False):
        _emit_json(report)
        return 0
    if report["count"] == 0:
        print("No dispatch holds.")
        return 0
    print(f"Dispatch holds ({report['count']}):")
    for hold in report["holds"]:
        line = f"  - {hold['task_id']}  {hold['bucket']}"
        if hold["bucket"] == "repo_serialized":
            holder = hold.get("holder") or {}
            line += (
                f"  repo={hold.get('repo_root')}  "
                f"holder={holder.get('task_id')} ({holder.get('status')})"
            )
        elif hold["bucket"] == "per_profile_capped":
            line += f"  {hold.get('assignee')} at {hold.get('current')}/{hold.get('cap')}"
        elif hold.get("reason"):
            line += f"  reason={hold['reason']}"
        print(line)
    return 0

def _daemon_print_deprecation() -> None:
    """Print the deprecation / migration message for ``kanban daemon``."""
    print(
        "hermes kanban daemon: DEPRECATED — the dispatcher now runs\n"
        "inside the gateway. To use kanban:\n"
        "\n"
        "    hermes gateway start       # starts the gateway + embedded dispatcher\n"
        "\n"
        "Ready tasks will be picked up on the next dispatcher tick\n"
        "(default: every 60 seconds). Configure via config.yaml:\n"
        "\n"
        "    kanban:\n"
        "      dispatch_in_gateway: true      # default\n"
        "      dispatch_interval_seconds: 60\n"
        "      failure_limit: 2              # consecutive non-success attempts before auto-block\n"
        "\n"
        "Running both the gateway AND this standalone daemon will\n"
        "race for claims. If you truly need the old standalone\n"
        "daemon (no gateway available), rerun with --force.",
        file=sys.stderr,
    )


def _daemon_write_pidfile(pidfile: str) -> None:
    """Best-effort pidfile write used by the --force daemon path."""
    try:
        Path(pidfile).parent.mkdir(parents=True, exist_ok=True)
        Path(pidfile).write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:
        print(f"warning: could not write pidfile {pidfile}: {exc}", file=sys.stderr)


def _daemon_ready_queue_nonempty() -> bool:
    """Cheap probe — is there at least one ready+assigned+unclaimed
    task whose assignee maps to a real Hermes profile (i.e. one the
    dispatcher would actually try to spawn for)?

    Filters out tasks assigned to control-plane lanes
    (e.g. ``orion-cc``, ``orion-research``) that are pulled by
    terminals via ``claim_task`` directly — those are correctly idle
    from the dispatcher's perspective, not stuck.
    """
    try:
        with kb.connect_closing() as conn:
            return kb.has_spawnable_ready(conn)
    except Exception:
        return False


def _daemon_on_tick(res: Any, health_state: dict[str, int], verbose: bool) -> None:
    """Per-tick health telemetry + optional verbose progress for --force daemon."""
    HEALTH_WINDOW = 6  # ticks (default 30s at interval=5)
    ready_pending = bool(res.skipped_unassigned) or _daemon_ready_queue_nonempty()
    spawned_any = bool(res.spawned)
    if ready_pending and not spawned_any:
        total_held, hold_counts, _dominant = kb.summarize_dispatch_holds([res])
        now = int(time.time())
        # An expected hold (repo-serialized / respawn-guarded / budget /
        # role-fit / per-profile cap) is not a profile-health stuck. A task
        # with no assignee IS operator-actionable, so it stays on the stuck
        # path even when other tasks are merely held.
        if total_held > 0 and not res.skipped_unassigned:
            health_state["bad_ticks"] = 0
            rg_count = hold_counts.get("respawn_guarded", 0)
            if rg_count > 0:
                # Canary: track persistence whenever ANY task is respawn-
                # guarded (not only when it dominates) so a stuck guard can't
                # be masked forever by a larger unrelated hold bucket.
                if health_state["respawn_held_since"] == 0:
                    health_state["respawn_held_since"] = now
                elif (
                    now - health_state["respawn_held_since"]
                    >= kb._RESPAWN_GUARD_SUCCESS_WINDOW
                    and now - health_state["last_warn_at"] >= 300
                ):
                    print(
                        f"[{_fmt_ts(now)}] WARN dispatcher: {rg_count} "
                        f"ready task(s) respawn-guarded for "
                        f">{kb._RESPAWN_GUARD_SUCCESS_WINDOW}s and never "
                        f"cleared — possible stuck guard. holds={hold_counts}.",
                        file=sys.stderr, flush=True,
                    )
                    health_state["last_warn_at"] = now
            else:
                health_state["respawn_held_since"] = 0
        else:
            health_state["respawn_held_since"] = 0
            health_state["bad_ticks"] += 1
    else:
        health_state["bad_ticks"] = 0
        health_state["respawn_held_since"] = 0
    # Emit a warning once per HEALTH_WINDOW bad ticks (not every tick)
    # so log volume stays bounded while the problem persists.
    if health_state["bad_ticks"] >= HEALTH_WINDOW:
        now = int(time.time())
        # Rate-limit repeats: at most one warning per 5 minutes.
        if now - health_state["last_warn_at"] >= 300:
            print(
                f"[{_fmt_ts(now)}] WARN dispatcher stuck: "
                f"ready queue non-empty for {health_state['bad_ticks']} "
                f"consecutive ticks but 0 workers spawned successfully, and "
                f"no expected hold explains it. "
                f"Check profile health (venv, PATH, credentials) and "
                f"`hermes kanban list --status ready` / "
                f"`hermes kanban list --status blocked` for recent "
                f"spawn_failed tasks.",
                file=sys.stderr, flush=True,
            )
            health_state["last_warn_at"] = now
    if not verbose:
        return
    did_work = (
        res.reclaimed or res.crashed or res.timed_out or res.promoted
        or res.spawned or res.auto_blocked or res.stale
    )
    if did_work:
        print(
            f"[{_fmt_ts(int(time.time()))}] "
            f"reclaimed={res.reclaimed} crashed={len(res.crashed)} "
            f"timed_out={len(res.timed_out)} stale={len(res.stale)} "
            f"promoted={res.promoted} spawned={len(res.spawned)} "
            f"auto_blocked={len(res.auto_blocked)}",
            flush=True,
        )


def _cmd_daemon(args: argparse.Namespace) -> int:
    """Deprecated — the dispatcher now runs inside the gateway.

    Left in as a stub so users with the old command in scripts/systemd
    units get a clear migration message instead of a cryptic
    "no such command" error. A ``--force`` escape hatch keeps the old
    standalone daemon alive for the rare edge case where someone truly
    cannot run the gateway (e.g. running on a host that forbids
    long-lived background services), but the default path exits 2
    with guidance so nobody accidentally keeps running two dispatchers
    against the same kanban.db.
    """
    # --force lets power users keep the standalone loop for one more
    # release cycle. Undocumented in `--help` so nobody discovers it
    # casually — intentional.
    if not getattr(args, "force", False):
        _daemon_print_deprecation()
        return 2

    # Legacy path — same logic as before, kept behind --force.
    # Make sure the DB exists before printing "started" so the user sees the
    # correct DB path and any init error surfaces immediately.
    kb.init_db()

    pidfile = getattr(args, "pidfile", None)
    if pidfile:
        _daemon_write_pidfile(pidfile)

    verbose = bool(getattr(args, "verbose", False))
    print(
        f"Kanban dispatcher running STANDALONE via --force "
        f"(interval={args.interval}s, pid={os.getpid()}). "
        f"Ctrl-C to stop. NOTE: if a gateway is also running with "
        f"dispatch_in_gateway=true (default), you have two dispatchers "
        f"racing for claims.",
        file=sys.stderr,
    )

    # Health telemetry: warn when every tick finds ready work but fails to
    # spawn any worker. Catches broken profiles, PATH drift, missing venv,
    # credential loss — cases where the per-task circuit breaker auto-blocks
    # each task quietly but the operator has no signal that the dispatcher
    # itself is dysfunctional.
    health_state = {"bad_ticks": 0, "last_warn_at": 0, "respawn_held_since": 0}

    def _on_tick(res):
        _daemon_on_tick(res, health_state, verbose)

    try:
        kb.run_daemon(
            interval=args.interval,
            max_spawn=args.max,
            failure_limit=getattr(args, "failure_limit", kb.DEFAULT_SPAWN_FAILURE_LIMIT),
            on_tick=_on_tick,
        )
    finally:
        if pidfile:
            try:
                Path(pidfile).unlink()
            except OSError:
                pass
    print("(dispatcher stopped)")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Live-stream task_events to the terminal."""
    kinds = (
        {k.strip() for k in args.kinds.split(",") if k.strip()}
        if args.kinds else None
    )
    cursor = 0
    print("Watching kanban events. Ctrl-C to stop.", flush=True)
    # Seed cursor at the latest id so we don't replay history.
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM task_events"
        ).fetchone()
        cursor = int(row["m"])

    try:
        while True:
            with kb.connect_closing() as conn:
                rows = conn.execute(
                    "SELECT e.id, e.task_id, e.kind, e.payload, e.created_at, "
                    "       t.assignee, t.tenant "
                    "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
                    "WHERE e.id > ? ORDER BY e.id ASC LIMIT 200",
                    (cursor,),
                ).fetchall()
            for r in rows:
                cursor = max(cursor, int(r["id"]))
                if kinds and r["kind"] not in kinds:
                    continue
                if args.assignee and r["assignee"] != args.assignee:
                    continue
                if args.tenant and r["tenant"] != args.tenant:
                    continue
                try:
                    payload = json.loads(r["payload"]) if r["payload"] else None
                except Exception:
                    payload = None
                pl = f" {payload}" if payload else ""
                print(
                    f"[{_fmt_ts(r['created_at'])}] {r['task_id']:10s} "
                    f"{r['kind']:18s} (@{r['assignee'] or '-'}){pl}",
                    flush=True,
                )
            time.sleep(max(0.1, args.interval))
    except KeyboardInterrupt:
        print("\n(stopped)")
        return 0

def _cmd_stats(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        stats = kb.board_stats(conn)
    if getattr(args, "json", False):
        _emit_json(stats)
        return 0
    print("By status:")
    for k in ("triage", "todo", "scheduled", "ready", "running", "blocked", "done"):
        print(f"  {k:8s}  {stats['by_status'].get(k, 0)}")
    blocked_by_kind = stats.get("blocked_by_kind") or {}
    if blocked_by_kind:
        print("\nBlocked by kind:")
        for kind, count in sorted(blocked_by_kind.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {kind:20s}  {count}")
    if stats["by_assignee"]:
        print("\nBy assignee:")
        for who, counts in sorted(stats["by_assignee"].items()):
            parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            print(f"  {who:20s}  {parts}")
    age = stats["oldest_ready_age_seconds"]
    if age is not None:
        print(f"\nOldest ready task age: {int(age)}s")
    return 0


def _cmd_scores(args: argparse.Namespace) -> int:
    if getattr(args, "digest", False):
        return _cmd_scores_digest(args)
    with kb.connect_closing() as conn:
        report = kb.scores_report(conn)
    if getattr(args, "json", False):
        _emit_json(report)
        return 0

    print(f"Score rows: {report['rows_total']}")
    print("By name:")
    for name, count in report["by_name"].items():
        print(f"  {name:20s}  {count}")
    print("By source:")
    for source, count in report["by_source"].items():
        print(f"  {source:20s}  {count}")
    rate = report["approval_rate"]
    rate_text = f"{rate:.1%}" if rate is not None else "n/a"
    print(f"Approval rate: {rate_text} ({report['approved_rows']}/{report['rows_total']})")
    print("Weekly approval rate:")
    for week in report["weeks"]:
        weekly_rate = week["approval_rate"]
        weekly_text = f"{weekly_rate:.1%}" if weekly_rate is not None else "n/a"
        print(
            f"  {week['year']}-W{week['week']:02d}  {weekly_text:6s} "
            f"({week['approved_rows']}/{week['rows_total']})"
        )
    return 0


def _fmt_pct(value: Optional[float]) -> str:
    """Format a 0..1 float as percentage string, or 'n/a'."""
    return f"{value:.1%}" if value is not None else "n/a"


def _cmd_scores_digest(args: argparse.Namespace) -> int:
    """Handle ``hermes kanban scores --digest [--weeks N] [--json]``."""
    weeks = max(1, int(getattr(args, "weeks", 4) or 4))
    with kb.connect_closing() as conn:
        digest = kb.scores_digest(conn, weeks=weeks)

    # Always write JSON sidecar for agent grounding (AC-2)
    try:
        from hermes_constants import get_hermes_home

        reports_dir = get_hermes_home() / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        sidecar = reports_dir / "scores-digest-latest.json"
        sidecar.write_text(
            json.dumps(digest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 — best-effort sidecar, never fail the digest
        pass

    if getattr(args, "json", False):
        _emit_json(digest)
        return 0

    # Build compact Markdown for Discord (max ~1800 chars)
    lines: list[str] = []
    lines.append("**Kanban Score Digest**")

    # Overall approval rate + WoW delta
    rate = digest["approval_rate"]
    wow = digest["wow_delta"]
    wow_text = ""
    if wow is not None:
        sign = "+" if wow >= 0 else ""
        wow_text = f" ({sign}{wow:.1%} WoW)"
    lines.append(
        f"Approval: **{_fmt_pct(rate)}** "
        f"({digest['approved_rows']}/{digest['rows_total']}){wow_text}"
    )

    # Weekly trend (compact)
    weekly_parts: list[str] = []
    for w in digest["weekly"]:
        wr = w["approval_rate"]
        weekly_parts.append(
            f"W{w['week']:02d} {_fmt_pct(wr)}"
        )
    if weekly_parts:
        lines.append("Trend: " + " → ".join(weekly_parts))

    # Per-profile breakdown
    if digest["by_profile"]:
        prof_parts = [
            f"{name}: {_fmt_pct(v['approval_rate'])} ({v['approved']}/{v['total']})"
            for name, v in digest["by_profile"].items()
        ]
        lines.append("Profile: " + " | ".join(prof_parts))

    # Per-model breakdown
    if digest["by_model"]:
        model_parts = [
            f"{name}: {_fmt_pct(v['approval_rate'])} ({v['approved']}/{v['total']})"
            for name, v in digest["by_model"].items()
        ]
        lines.append("Model: " + " | ".join(model_parts))

    # Cost/duration per approved run
    if digest["has_metric_scores"] and digest["cost_duration_per_approved_run"]:
        cd = digest["cost_duration_per_approved_run"]
        costs = [r["cost_usd"] for r in cd if r["cost_usd"] is not None]
        durations = [r["duration_seconds"] for r in cd if r["duration_seconds"] is not None]
        if costs:
            lines.append(
                f"Cost/approved run: avg ${sum(costs)/len(costs):.3f} "
                f"(n={len(costs)})"
            )
        if durations:
            avg_dur = sum(durations) / len(durations)
            lines.append(f"Duration/approved run: avg {avg_dur:.0f}s (n={len(durations)})")
    elif not digest["has_metric_scores"]:
        lines.append("Metrik-Scores: keine vorhanden")

    # Top-3 retry hotspots
    if digest["retry_hotspots"]:
        hs_parts = [
            f"{h['task_id']} (×{h['max_attempt']})"
            for h in digest["retry_hotspots"]
        ]
        lines.append("Retry-Hotspots: " + ", ".join(hs_parts))

    # Reference lines
    lines.append("Scorecard: /control → Scorecard")
    lines.append("Langfuse: loopback-Langfuse → Scores")

    text = "\n".join(lines)
    # Truncate to ~1800 chars for Discord safety
    if len(text) > 1800:
        text = text[:1797] + "..."
    print(text)
    return 0


def _cmd_backfill_costs(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        since_seconds = args.since_hours * 3600 if args.since_hours else None
        if getattr(args, "audited_claude_equivalent", False):
            report = kb.audit_claude_cost_equivalent_backfill(
                conn,
                limit=args.limit,
                since_seconds=since_seconds,
                board=getattr(args, "board", None),
                apply=bool(getattr(args, "apply", False)),
            )
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        if getattr(args, "audited_non_claude_equivalent", False):
            report = kb.audit_non_claude_cost_equivalent_backfill(
                conn,
                limit=args.limit,
                board=getattr(args, "board", None),
                apply=bool(getattr(args, "apply", False)),
            )
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        if getattr(args, "apply", False):
            print("--apply is only valid with --audited-claude-equivalent or --audited-non-claude-equivalent", file=sys.stderr)
            return 2
        token_report = kb.backfill_run_costs_from_tokens(
            conn,
            dry_run=bool(getattr(args, "dry_run", False)),
            limit=args.limit,
            since_seconds=since_seconds,
        )
        if getattr(args, "dry_run", False):
            print(
                f"Dry run: {token_report['rows_affected']} run(s) would be updated; "
                f"total cost_usd ${token_report['total_cost_usd']:.6f}."
            )
            return 0
        n = kb.backfill_run_costs(
            conn,
            limit=args.limit,
            since_seconds=since_seconds,
        )
        n_sessions = kb.backfill_run_costs_from_sessions(
            conn,
            limit=args.limit,
            since_seconds=since_seconds,
            board=getattr(args, "board", None),
        )
        n_repair = 0
        if getattr(args, "repair_frozen_equivalent", False):
            n_repair = kb.repair_cost_equivalent_for_frozen_runs(
                conn,
                limit=args.limit,
                since_seconds=since_seconds,
                board=getattr(args, "board", None),
            )
        print(
            "Backfilled cost_usd for "
            f"{token_report['rows_affected'] + n + n_sessions} run(s)."
        )
        if n_repair:
            print(f"Repaired cost_usd_equivalent for {n_repair} frozen closed run(s).")
    return 0


def _cmd_backfill_metrics(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        n = kb.backfill_run_metric_scores(conn)
        print(f"Backfilled run-metric scores for {n} row(s).")
    return 0


def _cmd_notify_subscribe(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            print(f"no such task: {args.task_id}", file=sys.stderr)
            return 1
        kb.add_notify_sub(
            conn, task_id=args.task_id,
            platform=args.platform, chat_id=args.chat_id,
            thread_id=args.thread_id, user_id=args.user_id,
            notifier_profile=args.notifier_profile or _profile_author(),
        )
    print(f"Subscribed {args.platform}:{args.chat_id}"
          + (f":{args.thread_id}" if args.thread_id else "")
          + f" to {args.task_id}")
    return 0

def _cmd_notify_list(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        subs = kb.list_notify_subs(conn, args.task_id)
    if getattr(args, "json", False):
        _emit_json(subs)
        return 0
    if not subs:
        print("(no subscriptions)")
        return 0
    for s in subs:
        thr = f":{s['thread_id']}" if s.get("thread_id") else ""
        owner = f"  owner={s['notifier_profile']}" if s.get("notifier_profile") else ""
        print(f"  {s['task_id']:10s}  {s['platform']}:{s['chat_id']}{thr}"
              f"  (since event {s['last_event_id']}){owner}")
    return 0

def _cmd_notify_unsubscribe(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        ok = kb.remove_notify_sub(
            conn, task_id=args.task_id,
            platform=args.platform, chat_id=args.chat_id,
            thread_id=args.thread_id,
        )
    if not ok:
        print("(no such subscription)", file=sys.stderr)
        return 1
    print(f"Unsubscribed from {args.task_id}")
    return 0

def _cmd_log(args: argparse.Namespace) -> int:
    content = kb.read_worker_log(args.task_id, tail_bytes=args.tail)
    if content is None:
        print(f"(no log for {args.task_id} — task may not have spawned yet)",
              file=sys.stderr)
        return 1
    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    return 0

def _cmd_runs(args: argparse.Namespace) -> int:
    """Show attempt history for a task."""
    rsk = _run_state_kwargs(args)
    if rsk is None:
        print(
            "kanban runs: pass both --state-type and --state-name, or omit both",
            file=sys.stderr,
        )
        return 2
    with kb.connect_closing() as conn:
        runs = kb.list_runs(conn, args.task_id, **rsk)
    if getattr(args, "json", False):
        print(json.dumps([
            {
                "id": r.id, "profile": r.profile, "status": r.status,
                "outcome": r.outcome, "started_at": r.started_at,
                "ended_at": r.ended_at, "summary": r.summary,
                "error": r.error, "metadata": r.metadata,
                "worker_pid": r.worker_pid, "step_key": r.step_key,
                # K5a/K17: die gestempelten Kosten-Spalten gehören in die
                # Anzeige — sonst wirkt das CLI "älter" als die DB.
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost_usd": r.cost_usd,
            } for r in runs
        ], indent=2, ensure_ascii=False))
        return 0
    if not runs:
        print(f"(no runs yet for {args.task_id})")
        return 0
    print(f"{'#':3s}  {'OUTCOME':12s}  {'PROFILE':16s}  {'ELAPSED':>8s}  STARTED")
    for i, r in enumerate(runs, 1):
        end = r.ended_at or int(time.time())
        # Clamp to 0 so NTP backward-jumps don't print negative durations.
        elapsed = max(0, end - r.started_at)
        if elapsed < 60:
            el = f"{elapsed}s"
        elif elapsed < 3600:
            el = f"{elapsed // 60}m"
        else:
            el = f"{elapsed / 3600:.1f}h"
        outcome = r.outcome or ("(running)" if not r.ended_at else r.status)
        print(f"{i:3d}  {outcome:12s}  {(r.profile or '-'):16s}  {el:>8s}  {_fmt_ts(r.started_at)}")
        if r.summary:
            # Indent and truncate long summaries to keep the table readable.
            summary = r.summary.splitlines()[0][:100]
            print(f"     → {summary}")
        if r.error:
            print(f"     ✖ {r.error[:100]}")
    return 0

def _cmd_context(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        text = kb.build_worker_context(conn, args.task_id, audience="operator")
    print(text)
    return 0

# ---------------------------------------------------------------------------
# Triage (specify / decompose) + GC
# ---------------------------------------------------------------------------

def _cmd_specify(args: argparse.Namespace) -> int:
    """Flesh out a triage task (or all of them) via auxiliary LLM,
    then promote to todo. Thin wrapper over ``kanban_specify``."""
    from hermes_cli import kanban_specify as spec

    all_flag = bool(getattr(args, "all_triage", False))
    tenant = getattr(args, "tenant", None)
    author = getattr(args, "author", None) or _profile_author()
    want_json = bool(getattr(args, "json", False))

    if args.task_id and all_flag:
        print(
            "kanban: pass either a task id OR --all, not both",
            file=sys.stderr,
        )
        return 2

    if all_flag:
        ids = spec.list_triage_ids(tenant=tenant)
        if not ids:
            msg = (
                "No triage tasks"
                + (f" for tenant {tenant!r}" if tenant else "")
                + "."
            )
            if want_json:
                print(json.dumps({"specified": 0, "total": 0}))
            else:
                print(msg)
            return 0
    elif args.task_id:
        ids = [args.task_id]
    else:
        print(
            "kanban: specify requires a task id or --all",
            file=sys.stderr,
        )
        return 2

    ok_count = 0
    fail_count = 0
    for tid in ids:
        outcome = spec.specify_task(tid, author=author)
        if outcome.ok:
            ok_count += 1
        else:
            fail_count += 1
        if want_json:
            print(json.dumps({
                "task_id": outcome.task_id,
                "ok": outcome.ok,
                "reason": outcome.reason,
                "detail": outcome.detail,
                "new_title": outcome.new_title,
            }))
        elif outcome.ok:
            title_suffix = (
                f" — retitled: {outcome.new_title!r}"
                if outcome.new_title
                else ""
            )
            print(f"Specified {outcome.task_id} → todo{title_suffix}")
        else:
            detail_suffix = f" — {outcome.detail}" if outcome.detail else ""
            print(
                f"kanban: specify {outcome.task_id}: {outcome.reason}{detail_suffix}",
                file=sys.stderr,
            )
    if not all_flag:
        return 0 if ok_count == 1 else 1
    # --all: succeed if at least one promotion landed; exit 1 only when
    # every candidate failed (honest signal for scripts).
    return 0 if (ok_count > 0 or not ids) else 1

def _cmd_decompose(args: argparse.Namespace) -> int:
    """Fan a triage task (or all of them) out into a graph of child
    tasks via the auxiliary LLM, routed to specialist profiles by
    description. Thin wrapper over ``kanban_decompose``."""
    from hermes_cli import kanban_decompose as decomp

    all_flag = bool(getattr(args, "all_triage", False))
    tenant = getattr(args, "tenant", None)
    author = getattr(args, "author", None) or _profile_author()
    want_json = bool(getattr(args, "json", False))

    if args.task_id and all_flag:
        print(
            "kanban: pass either a task id OR --all, not both",
            file=sys.stderr,
        )
        return 2

    if all_flag:
        ids = decomp.list_triage_ids(tenant=tenant)
        if not ids:
            msg = (
                "No triage tasks"
                + (f" for tenant {tenant!r}" if tenant else "")
                + "."
            )
            if want_json:
                print(json.dumps({"decomposed": 0, "total": 0}))
            else:
                print(msg)
            return 0
    elif args.task_id:
        ids = [args.task_id]
    else:
        print(
            "kanban: decompose requires a task id or --all",
            file=sys.stderr,
        )
        return 2

    ok_count = 0
    for tid in ids:
        outcome = decomp.decompose_task(tid, author=author)
        if outcome.ok:
            ok_count += 1
        # Fail-soft decompose-failure bookkeeping. A counter bump must
        # never break the decomposition flow, so swallow any DB error.
        try:
            with kb.connect_closing() as _conn:
                if outcome.ok:
                    kb.reset_decompose_failed(_conn, outcome.task_id)
                else:
                    kb.record_decompose_failure(
                        _conn,
                        outcome.task_id,
                        reason=outcome.reason,
                        detail=outcome.detail,
                    )
        except Exception:  # pragma: no cover - defensive
            pass
        if want_json:
            print(json.dumps({
                "task_id": outcome.task_id,
                "ok": outcome.ok,
                "reason": outcome.reason,
                "detail": outcome.detail,
                "fanout": outcome.fanout,
                "child_ids": outcome.child_ids,
                "new_title": outcome.new_title,
            }))
        elif outcome.ok:
            if outcome.fanout and outcome.child_ids:
                child_summary = ", ".join(outcome.child_ids)
                print(
                    f"Decomposed {outcome.task_id} → {len(outcome.child_ids)} "
                    f"children ({child_summary}); root promoted to todo"
                )
            else:
                title_suffix = (
                    f" — retitled: {outcome.new_title!r}"
                    if outcome.new_title
                    else ""
                )
                print(
                    f"Specified {outcome.task_id} → todo "
                    f"(no fanout){title_suffix}"
                )
        else:
            detail_suffix = f" — {outcome.detail}" if outcome.detail else ""
            print(
                f"kanban: decompose {outcome.task_id}: {outcome.reason}{detail_suffix}",
                file=sys.stderr,
            )
    if not all_flag:
        return 0 if ok_count == 1 else 1
    return 0 if (ok_count > 0 or not ids) else 1

def _cmd_gc(args: argparse.Namespace) -> int:
    """Remove scratch workspaces of archived tasks, prune old events, and
    delete old worker logs."""
    import shutil
    scratch_root = kb.workspaces_root()
    removed_ws = 0
    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT id, workspace_kind, workspace_path FROM tasks WHERE status = 'archived'"
        ).fetchall()
    for row in rows:
        if row["workspace_kind"] != "scratch":
            continue
        path = Path(row["workspace_path"] or (scratch_root / row["id"]))
        try:
            path = path.resolve()
        except OSError:
            continue
        try:
            path.relative_to(scratch_root.resolve())
        except ValueError:
            # Safety: never delete outside the scratch root.
            continue
        if path.exists() and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed_ws += 1

    event_days = getattr(args, "event_retention_days", 30)
    log_days = getattr(args, "log_retention_days", 30)
    with kb.connect_closing() as conn:
        removed_events = kb.gc_events(
            conn, older_than_seconds=event_days * 24 * 3600,
        )
    removed_logs = kb.gc_worker_logs(
        older_than_seconds=log_days * 24 * 3600,
    )
    print(f"GC complete: {removed_ws} workspace(s), "
          f"{removed_events} event row(s), {removed_logs} log file(s) removed")
    return 0

# ---------------------------------------------------------------------------
# Slash-command entry point (used by /kanban from CLI and gateway)
# ---------------------------------------------------------------------------

_SLASH_KANBAN_HELP = """\
**/kanban** — manage the shared task board.

Common subcommands:
  `list` (alias `ls`)   List tasks on the current board
  `show <id>`           Task details + comments + events
  `stats`               Per-status / per-assignee counts
  `create <title>…`     Create a task (auto-subscribes you to events)
  `comment <id> <msg>`  Append a comment
  `complete <id>…`      Mark task(s) done
  `block <id> [reason]` Mark blocked; `schedule <id> [reason]` parks time-delay work; `unblock <id>` to revive
  `assign <id> <profile>`  Reassign
  `boards list`         Show all boards
  `assignees`           Known profiles + counts
  `context <id>`        Full worker-context dump
  `runs <id>`           Attempt history
  `log <id>`            Worker log

Run `/kanban <subcommand> -h` for arguments. \
Read-only commands are safe while an agent is running.\
"""

# ---------------------------------------------------------------------------
# Module-level dispatch table (command name → handler).
# Defined here so every ``_cmd_*`` / ``_dispatch_*`` is already bound.
# ---------------------------------------------------------------------------

_KANBAN_ACTION_HANDLERS: dict[str, Any] = {
    "init":     _cmd_init,
    "create":   _cmd_create,
    "swarm":    _cmd_swarm,
    "list":     _cmd_list,
    "ls":       _cmd_list,
    "show":     _cmd_show,
    "assign":   _cmd_assign,
    "reclaim":  _cmd_reclaim,
    "reassign": _cmd_reassign,
    "diagnostics": _cmd_diagnostics,
    "diag":     _cmd_diagnostics,
    "link":     _cmd_link,
    "unlink":   _cmd_unlink,
    "claim":    _cmd_claim,
    "comment":  _cmd_comment,
    "complete": _cmd_complete,
    "review-commit": _cmd_review_commit,
    "edit":     _cmd_edit,
    "respec":   _cmd_respec,
    "block":    _cmd_block,
    "set-workflow": _cmd_set_workflow,
    "schedule": _cmd_schedule,
    "unblock":  _cmd_unblock,
    "promote":  _cmd_promote,
    "close-sprint": _cmd_close_sprint,
    "report-discord": _cmd_report_discord,
    "archive":  _cmd_archive,
    "tail":     _cmd_tail,
    "dispatch": _cmd_dispatch,
    "holds":    _cmd_holds,
    "daemon":   _cmd_daemon,
    "watch":    _cmd_watch,
    "stats":    _cmd_stats,
    "scores":   _cmd_scores,
    "backfill-costs": _cmd_backfill_costs,
    "backfill-metrics": _cmd_backfill_metrics,
    "log":      _cmd_log,
    "runs":     _cmd_runs,
    "heartbeat": _cmd_heartbeat,
    "assignees": _cmd_assignees,
    "notify-subscribe":   _cmd_notify_subscribe,
    "notify-list":        _cmd_notify_list,
    "notify-unsubscribe": _cmd_notify_unsubscribe,
    "context":  _cmd_context,
    "specify":  _cmd_specify,
    "decompose":  _cmd_decompose,
    "gc":       _cmd_gc,
    "epic":     _dispatch_epic,
    "closeout": _cmd_closeout,
    "release-gate": _cmd_release_gate,
    "release-uireal": _cmd_release_uireal,
    "release-freigabe": _cmd_release_freigabe,
    "complete-freigabe": _cmd_complete_freigabe,
    "veto-freigabe": _cmd_veto_freigabe,
}

def run_slash(rest: str) -> str:
    """Execute a ``/kanban …`` string and return captured stdout/stderr.

    ``rest`` is everything after ``/kanban`` (may be empty).  Used from
    both the interactive CLI (``self._handle_kanban_command``) and the
    gateway (``_handle_kanban_command``) so formatting is identical.
    """
    import io
    import contextlib

    tokens = shlex.split(rest) if rest and rest.strip() else []

    # Bare ``/kanban`` or ``/kanban help`` / ``--help`` / ``-h`` / ``?``:
    # show the curated short-help block instead of dumping argparse's full
    # usage tree (which is enormous and reads as garbage in a chat
    # bubble).  Per-subcommand help still works via ``/kanban foo -h``.
    if not tokens or tokens[0] in {"help", "--help", "-h", "?"}:
        return _SLASH_KANBAN_HELP

    # Single argparse tree rooted at "/kanban".  build_parser() expects a
    # subparsers action to attach to, so build a throwaway one and pull
    # the kanban_parser back out — then drive it directly so usage/error
    # text reads as ``/kanban`` (not ``/kanban-wrap kanban``).
    _wrap = argparse.ArgumentParser(prog="/kanban-wrap", add_help=False)
    _wrap.exit_on_error = False  # type: ignore[attr-defined]
    _top_sub = _wrap.add_subparsers(dest="_top")
    kanban_parser = build_parser(_top_sub)
    kanban_parser.prog = "/kanban"
    kanban_parser.exit_on_error = False  # type: ignore[attr-defined]
    for _action in kanban_parser._actions:
        if isinstance(_action, argparse._SubParsersAction):
            for _name, _choice in _action.choices.items():
                _choice.prog = f"/kanban {_name}"
                _choice.exit_on_error = False  # type: ignore[attr-defined]

    def _usage_for_error() -> str:
        if tokens:
            for _action in kanban_parser._actions:
                if isinstance(_action, argparse._SubParsersAction):
                    subparser = _action.choices.get(tokens[0])
                    if subparser is not None:
                        return subparser.format_usage().rstrip()
        return kanban_parser.format_usage().rstrip()

    buf_out = io.StringIO()
    buf_err = io.StringIO()
    # ``-h`` / ``--help`` makes argparse print to stdout and SystemExit(0).
    # Capture both streams so neither the help text nor the error text
    # bypasses our buffer.
    try:
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            args = kanban_parser.parse_args(tokens)
    except SystemExit as exc:
        out = buf_out.getvalue().rstrip()
        err = buf_err.getvalue().rstrip()
        # Help dump (exit 0) → return the captured help text directly.
        if exc.code in {0, None} and out:
            return out
        body = err or out
        return f"⚠ /kanban usage error\n{body}" if body else "⚠ /kanban usage error"
    except argparse.ArgumentError as exc:
        return f"⚠ /kanban usage error\n{_usage_for_error()}\n{exc}"

    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        try:
            kanban_command(args)
        except SystemExit:
            pass
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)

    out = buf_out.getvalue().rstrip()
    err = buf_err.getvalue().rstrip()
    if err and out:
        return f"{out}\n{err}"
    return err if err else (out or "(no output)")
