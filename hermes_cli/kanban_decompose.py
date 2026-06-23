"""Kanban decomposer — fan a triage task out into a graph of child tasks.

Invoked by ``hermes kanban decompose [task_id | --all]`` and the
auto-decompose path in the gateway dispatcher loop. Reads the user's
profile roster (with descriptions) and asks the auxiliary LLM to
return a task graph in JSON. Then atomically creates the children,
links them under the root, and flips the root ``triage -> todo``.

The root task stays alive and becomes the parent of every leaf child,
so when the whole graph completes the root wakes back up — its
assignee (the orchestrator profile) gets a chance to judge completion
and add more tasks if the work isn't done yet.

Design notes
------------

* Mirrors the shape of ``hermes_cli/kanban_specify.py``: lazy aux
  client import inside the function, lenient response parse, never
  raises on expected failure modes.

* The system prompt sees the *configured* profile roster — names plus
  descriptions plus the default fallback. Profiles without a
  description are still listed (with a note) so the decomposer can
  match on name as a fallback, but the user has an obvious incentive
  to describe them.

* ``fanout=false`` collapses to the same effect as ``kanban specify``:
  we tighten the body and flip ``triage -> todo`` as a single task,
  no children created. This makes ``decompose`` a strict superset of
  ``specify`` from the user's perspective.

* If the LLM picks an assignee that doesn't exist as a profile, we
  rewrite it to the configured ``default_assignee`` (or the default
  profile if unset). A child task NEVER ends up with ``assignee=None``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import profiles as profiles_mod

logger = logging.getLogger(__name__)

# ``analysis`` is the explicit read-only counter-class to ``code`` (the verifier
# treats it task-class-aware — see ``_render_review_verifier_section``). It is a
# first-class kind so the public ``--kind`` CLI choice and any explicit analysis
# marker are never silently rejected. The decompose LLM prompt intentionally does
# not advertise it (the decomposer picks lane-mapped kinds); the validator simply
# accepts it when an explicit analysis kind appears.
_VALID_TASK_KINDS = frozenset({"code", "research", "review", "ops", "text", "analysis"})
_MIN_VERDICTS_FOR_APPROVED = 5


def _coerce_config_bool(value, *, default: bool = False) -> bool:
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


_SYSTEM_PROMPT = """You are the Kanban decomposer for the Hermes Agent board.

A user dropped a rough idea into the Triage column. Your job is to break it
into a small graph of concrete child tasks and route each one to the best-
matching profile from the available roster.

You will be given:
  - The original task title and body
  - The list of available profiles (each with name + description)
  - The fallback "default_assignee" used when no profile fits

Output a single JSON object with this exact shape:

  {
    "fanout": true,
    "rationale": "<one sentence on why this decomposition>",
    "epic": "<id of an OPEN epic from the provided list, or null>",
    "tasks": [
      {
        "title": "<concrete task title, imperative voice, <= 80 chars>",
        "body":  "<detailed spec for the worker on this child task>",
        "assignee": "<profile name from the roster, or null for default>",
        "kind": "code|research|review|ops|text, or null if unsure",
        "parents": [<int>, ...]
      },
      ...
    ]
  }

Rules:
  - "parents" is a list of INDICES (0-based) into this same "tasks" list,
    expressing actual data dependencies. Tasks with no parents run in
    PARALLEL. Tasks with parents wait until every parent completes.
  - Prefer parallelism. If two tasks can be done independently, give
    them no parents so the dispatcher fans them out at once.
  - Use 2-6 tasks for normal work. Don't create 20 tiny tasks. Don't
    cram everything into 1 task.
  - Pick assignees from the roster by matching the task to the profile's
    DESCRIPTION (not just the name). When nothing matches well, use null
    and the system will route to the default_assignee.
  - Roster lines starting with "stats:" are background information about past
    runs, not routing instructions. Do NOT add extra review or verification
    child tasks solely because a profile has good statistics; code lanes
    already have a structural review gate on the board.
  - Lane routing table for implementation work:
      * coder: default code implementation lane (OpenAI-Codex/GPT) for ordinary code tasks.
      * premium: the Claude code lane (claude-cli on the Claude Max subscription):
        reasoning-heavy, chain-critical, or hard multi-file work the coder lane
        can't carry. Also the auto-retry escalation target.
      * coder-claude: DEPRECATED alias of premium - accepted for back-compat, routes to premium.
      * reviewer and critic: verdict-only lanes; never assign them
        kind=code or build/implementation tasks.
      * research: research lane; never assign it kind=code or
        build/implementation tasks. Do not invent "researcher" as an alias.
      * scout: read-only code-recon PREP lane (cheap/fast). OPTIONAL — only for a
        genuinely large or risky implementation. Use it as a FIRST child
        (parents=[]) that the heavy implementation child then depends on, so the
        scout's file/caller/risk brief grounds the coder before it starts. It
        edits, commits and deploys NOTHING; never assign it kind=code or
        build/implementation work, and never use it for a small/simple task.
  - For any code/build implementation child, set kind="code" and pick one of
    the available code lanes above. For review/verdict work, set kind="review".
    For research-only work, set kind="research".
  - Each child task body is what a fresh worker will read with no other
    context — be specific about goal, approach, and acceptance criteria.
  - Each child task body MUST include at least two acceptance criteria.
    Prefer structured bullets with stable `AC-...` ids. Each criterion must
    be outcome/state-oriented and name a concrete `verification` method
    (tool output, file path, event, metadata field, test command, or reviewer
    verdict) plus a concrete `done_signal` the worker/reviewer can cite.
    Avoid vague activity-only criteria such as "implement X", "tests run",
    or "documentation updated" without a specific proof signal.
  - For worker-lane children assigned to admin, coder, research, reviewer,
    or critic, include a structured YAML scope block in the body with
    `scope_contract.version: 2`, `allowed_tools`, and
    `completion_policy.require_scope_attestation: true`. `allowed_tools` is a
    declarative kanban-lifecycle attestation: set it ONLY to a subset of
    exactly these four values — `kanban_show`, `kanban_complete`,
    `kanban_block`, `kanban_comment`. The worker's real work tools (file,
    terminal, web, etc.) come from its PROFILE config — NEVER list those
    (e.g. `file_read`, `shell`, `terminal`) in `allowed_tools`. The Python
    decomposer normalizes and validates this block, so this prompt rule is a
    secondary defense. Do NOT use broad allowed_tools values like `all`,
    `any`, `*`, `tools`, `mcp`, or `kanban`.

CRITICAL: Constraint preservation (non-negotiable).
  - Any line in the original task body starting with
    `CRITICAL:`, `MANDATORY:`, `MUST:`, or `NEVER:` MUST be
    reproduced VERBATIM in every kid task body whose scope touches
    that constraint. Do NOT paraphrase, do NOT summarize, do NOT
    soften the wording ("if implementation requires", "when
    appropriate", "ideally" are all FORBIDDEN softeners for these
    lines).
  - If only one kid touches the scope, the verbatim line goes in
    that one kid. If multiple kids touch the scope, the verbatim
    line goes in every one of them.
  - Any absolute filesystem path (anything starting with `/` or
    `~/`) mentioned in the parent body MUST appear verbatim in at
    least one kid body that uses that path. Do NOT abbreviate
    `~/.hermes/reports/<sprint>/` to "reports directory" — keep the
    full absolute path.
  - Any task id (`t_` followed by 8 hex chars, or a similar
    canonical id form mentioned in the body) referenced in the
    parent body MUST appear verbatim in at least one kid body when
    the work depends on or refers to that task.
  - These preservation rules are stricter than the general
    "rephrase for clarity" license you have on the rest of the
    body. Constraints LOSE their meaning when they are paraphrased.

Optional epic assignment (conservative, top-level "epic" field):
  - You may receive a list of OPEN epics (durable initiatives) with ids and
    titles. Set "epic" to the id of the ONE epic whose subject CLEARLY
    matches this task's content.
  - When in doubt, when nothing matches, or when no list is provided:
    "epic": null. A wrong grouping is worse than no grouping.
  - NEVER invent epic ids and NEVER create new epics — only ids verbatim
    from the provided list are valid.

When the task is genuinely a single unit of work (no useful decomposition),
return:

  {
    "fanout": false,
    "rationale": "<one sentence>",
    "epic": "<id of an OPEN epic from the provided list, or null>",
    "title": "<tightened title>",
    "body":  "<concrete spec for a single worker>",
    "assignee": "<profile name from the roster, or null for default>",
    "kind": "code|research|review|ops|text, or null if unsure"
  }

In that case the task stays as one work item, just with a tightened spec and
a concrete assignee. If no profile fits, use null and the system will route to
the default_assignee.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Available profiles (assignees you may pick from):
{roster}

Default assignee (used when no profile fits a task): {default_assignee}

Open epics (set "epic" ONLY on a clear content match, else null):
{epics}
"""


# The documented ("plan + spec") method asks the SAME decomposer for two
# extra, optional, NON-breaking fields on top of the base schema so the
# backend can render a human-readable Vault plan-spec (narrative on top,
# subtask table below) from the very same object it creates the kanban
# subtasks from — one source, no drift. The base shape is unchanged, so a
# model that ignores the addendum still yields a valid decomposition.
_DOCUMENTED_PROMPT_ADDENDUM = """

DOCUMENTED-PLAN MODE (this request only):
You are additionally writing a short, durable plan document a human will
read. On top of the base JSON shape, include:

  - A top-level "narrative" string: 2-5 sentences of plain prose
    explaining the plan — the goal, the chosen approach, and how the
    subtasks fit together / in what order. Markdown is allowed. This is
    the reasoning a reader sees BEFORE the subtask table. No code fences.
  - On EACH task object, a "summary" string: one terse line (<= 120
    chars) describing what that subtask delivers, for the table row.

These two fields are additive. Keep every base rule (fanout, parents,
assignees, constraint preservation, scope blocks) exactly as specified.
Still output only the single JSON object, no prose outside it."""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class DecomposeOutcome:
    """Result of decomposing a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    fanout: bool = False
    child_ids: list[str] | None = None
    new_title: Optional[str] = None
    # Set by the documented ("plan + spec") method only: the relative
    # filename of the Vault plan-spec written for this root, and whether
    # the children were held in ``scheduled`` (gate) vs auto-promoted.
    spec_relpath: Optional[str] = None
    gated: bool = False


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _extract_json_blob(raw: str) -> Optional[dict]:
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    candidate = stripped[first : last + 1]
    try:
        val = json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(val, dict):
        return None
    return val



def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _resolve_orchestrator_profile(cfg: dict) -> str:
    """Resolve which profile owns the root/orchestration task after fan-out.

    Falls back to the active default profile when ``kanban.orchestrator_profile``
    is unset, so a task is never stranded for lack of an orchestrator.
    """
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("orchestrator_profile") or "").strip()
    if explicit:
        try:
            if profiles_mod.profile_exists(explicit):
                return explicit
        except Exception:
            pass
    # Fall back to the active default profile.
    try:
        return profiles_mod.get_active_profile_name() or "default"
    except Exception:
        return "default"


def _resolve_default_assignee(cfg: dict) -> str:
    """Resolve which profile catches child tasks the orchestrator can't route."""
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("default_assignee") or "").strip()
    if explicit:
        try:
            if profiles_mod.profile_exists(explicit):
                return explicit
        except Exception:
            pass
    try:
        return profiles_mod.get_active_profile_name() or "default"
    except Exception:
        return "default"


def _build_roster() -> tuple[list[dict], set[str]]:
    """Return (roster_for_prompt, valid_assignee_names).

    Each roster entry is ``{name, description, has_description}``. The
    valid-set is used after the LLM responds to rewrite invalid
    assignees to the default fallback.
    """
    roster: list[dict] = []
    valid: set[str] = set()
    try:
        all_profiles = profiles_mod.list_profiles()
    except Exception as exc:
        logger.warning("decompose: failed to list profiles: %s", exc)
        return roster, valid
    for p in all_profiles:
        desc = (p.description or "").strip()
        roster.append({
            "name": p.name,
            "description": desc or f"(no description; profile named {p.name!r})",
            "has_description": bool(desc),
        })
        valid.add(p.name)
    return roster, valid


def _format_pct(value: float) -> str:
    return f"{int(round(value))}%"


def _format_compact_count(value: int) -> str:
    if abs(value) < 1000:
        return str(value)
    compact = value / 1000.0
    text = f"{compact:.1f}".rstrip("0").rstrip(".")
    return f"{text}k"


def _format_profile_outcome_stats(stats: dict) -> str:
    parts = [
        f"done {_format_pct(float(stats['done_pct']))}",
        f"blocked {_format_pct(float(stats['blocked_pct']))}",
        f"timeout {_format_pct(float(stats['timeout_pct']))}",
    ]
    avg_tokens = stats.get("avg_tokens")
    if avg_tokens is not None:
        parts.append(f"Ø {_format_compact_count(int(avg_tokens))} tok")
    avg_runtime = stats.get("avg_runtime_s")
    if avg_runtime is not None:
        parts.append(f"Ø {int(avg_runtime)}s")
    approved_pct = stats.get("approved_pct")
    verdict_n = int(stats.get("verdict_n") or 0)
    if approved_pct is not None and verdict_n >= _MIN_VERDICTS_FOR_APPROVED:
        parts.append(f"approved {_format_pct(float(approved_pct))} (n={verdict_n})")
    return " · ".join(parts)


def _enrich_roster_with_outcome_stats(conn, roster: list[dict]) -> None:
    if not roster:
        return
    try:
        stats_by_profile = kb.profile_outcome_stats(conn)
    except Exception as exc:
        logger.debug("decompose: profile outcome stats unavailable: %s", exc)
        return
    if not stats_by_profile:
        return
    for entry in roster:
        stats = stats_by_profile.get(entry["name"])
        if stats:
            entry["stats"] = stats


def _format_roster(roster: list[dict]) -> str:
    if not roster:
        return "  (no profiles installed — decomposer cannot route work)"
    lines = []
    for entry in roster:
        tag = "" if entry["has_description"] else " ⚠ undescribed"
        lines.append(f"  - {entry['name']}{tag}: {entry['description']}")
        stats = entry.get("stats")
        if stats:
            lines.append(f"    stats: {_format_profile_outcome_stats(stats)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Constraint-preservation validator
# ---------------------------------------------------------------------------
#
# The LLM is told (in _SYSTEM_PROMPT) to reproduce CRITICAL:/MANDATORY:/
# MUST:/NEVER: lines and absolute paths/task ids verbatim from the parent
# body in the kid bodies that touch that scope. Even with a strong
# prompt the model can drop or paraphrase them under pressure (see
# `feedback_hermes_kanban_mandatory_comment_pattern`). This module
# post-validates the LLM's output and logs a warning so the operator
# notices BEFORE the kids get claimed by the dispatcher.
#
# We don't auto-reject the decomposition — the workaround is for the
# operator to fix the kid via `kanban comment <kid_id> "MANDATORY: ..."`
# before the worker claims the kid (see AGENTS.md "Worker-Steering via
# Comments"). Warning-only is the right severity here.

_CONSTRAINT_PREFIXES: tuple[str, ...] = ("CRITICAL:", "MANDATORY:", "MUST:", "NEVER:")

# Absolute filesystem paths: starts with `/` or `~/`, not part of a URL.
# Match at least 4 chars after the leading marker so we don't grab "/" alone.
_ABS_PATH_RE = re.compile(r"(?<![:/\w])(?:~/|/)(?:[\w.\-/]+)")

# Canonical kanban task ids: `t_` + at least 6 hex chars.
_TASK_ID_RE = re.compile(r"\bt_[0-9a-f]{6,}\b")


def _collect_constraint_lines(body: str) -> list[str]:
    """Return CRITICAL/MANDATORY/MUST/NEVER lines from ``body``, verbatim."""
    if not body:
        return []
    found: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        # Match the prefix at line start (ignore leading bullets / numbering).
        # Allow `- CRITICAL: ...` and `1. CRITICAL: ...` as well as bare.
        bare = stripped.lstrip("-*•+0123456789. \t")
        for prefix in _CONSTRAINT_PREFIXES:
            if bare.startswith(prefix):
                found.append(stripped)
                break
    return found


def _collect_absolute_paths(body: str) -> list[str]:
    """Return absolute filesystem paths mentioned in ``body``."""
    if not body:
        return []
    seen: list[str] = []
    in_seen: set[str] = set()
    for match in _ABS_PATH_RE.findall(body):
        # Heuristic: ignore short hits ("/", "/a") and very-likely URLs
        # ("//example.com" was already excluded by the negative lookbehind).
        if len(match) < 4:
            continue
        if match not in in_seen:
            in_seen.add(match)
            seen.append(match)
    return seen


def _collect_task_ids(body: str) -> list[str]:
    """Return canonical kanban task ids mentioned in ``body``."""
    if not body:
        return []
    seen: list[str] = []
    in_seen: set[str] = set()
    for match in _TASK_ID_RE.findall(body):
        if match not in in_seen:
            in_seen.add(match)
            seen.append(match)
    return seen


@dataclass
class ConstraintPreservationReport:
    """What the validator found.

    Fields list the parent-body items that did NOT survive into any kid:

    - ``missing_constraints``: CRITICAL/MANDATORY/MUST/NEVER lines absent
      from every kid body.
    - ``missing_paths``: absolute filesystem paths absent from every kid.
    - ``missing_task_ids``: canonical ``t_…`` ids absent from every kid.

    ``ok`` is True when all three are empty.
    """

    missing_constraints: list[str]
    missing_paths: list[str]
    missing_task_ids: list[str]

    @property
    def ok(self) -> bool:
        return not (
            self.missing_constraints
            or self.missing_paths
            or self.missing_task_ids
        )


def validate_constraint_preservation(
    parent_body: str,
    kids: list[dict],
) -> ConstraintPreservationReport:
    """Check that critical lines and absolute paths/ids from ``parent_body``
    survived verbatim into at least one of the ``kids``' bodies.

    ``kids`` is a list of dicts shaped like the decomposer output:
    ``{"title": ..., "body": ..., ...}``.

    Pure-function, no side effects. Callers wire logging.
    """
    constraints = _collect_constraint_lines(parent_body)
    paths = _collect_absolute_paths(parent_body)
    task_ids = _collect_task_ids(parent_body)
    if not (constraints or paths or task_ids):
        return ConstraintPreservationReport([], [], [])

    kid_bodies: list[str] = []
    for kid in kids:
        body = kid.get("body") if isinstance(kid, dict) else None
        kid_bodies.append(body if isinstance(body, str) else "")

    def _missing_from_all_kids(needle: str) -> bool:
        if not needle:
            return False
        return all(needle not in body for body in kid_bodies)

    missing_constraints = [
        line for line in constraints if _missing_from_all_kids(line)
    ]
    missing_paths = [p for p in paths if _missing_from_all_kids(p)]
    missing_task_ids = [t for t in task_ids if _missing_from_all_kids(t)]

    return ConstraintPreservationReport(
        missing_constraints=missing_constraints,
        missing_paths=missing_paths,
        missing_task_ids=missing_task_ids,
    )


# ---------------------------------------------------------------------------
# Worker scope-contract injection + validation
# ---------------------------------------------------------------------------

_WORKER_SCOPE_LANES: frozenset[str] = frozenset({
    "admin",
    "coder",
    "coder-claude",
    "premium",
    "research",
    "reviewer",
    "critic",
    "verifier",
    # Slice c: scout is a real gateway-dispatched worker (read-only recon), so it
    # gets the same kanban-lifecycle scope-contract attestation as research.
    "scout",
})

_BASE_WORKER_ALLOWED_TOOLS: tuple[str, ...] = (
    "kanban_show",
    "kanban_complete",
    "kanban_block",
    "kanban_comment",
    "kanban_heartbeat",
    "kanban_create",
    # Workspace tools every worker lane needs to actually DO the task. Without
    # these a disciplined worker that reads allowed_tools as a binding allowlist
    # self-blocks on any artifact-producing task (it has file/terminal from its
    # profile, but the contract didn't list them). allowed_tools is advisory
    # attestation, not runtime enforcement — but workers honour it literally, so
    # the canonical set must include the tools their profile already grants.
    "read_file",
    "write_file",
    "patch",
    "search_files",
    "terminal",
)

_BROAD_ALLOWED_TOOL_MARKERS: frozenset[str] = frozenset({
    "all",
    "*",
    "any",
    "tools",
    "mcp",
    "kanban",
    "all_tools",
})

_FORBIDDEN_SCOPE_MARKERS: frozenset[str] = frozenset({
    "openclaw",
    "atlas",
    "mission-control",
    "mission_control",
    "telegram",
    "auth",
    "secrets",
    "config_write",
    "cron_write",
})

_DEFAULT_FORBIDDEN_SYSTEMS: tuple[str, ...] = (
    "OpenClaw",
    "Atlas",
    "Mission-Control",
    "Telegram",
    "secrets",
    "auth",
    "config_write",
    "cron_write",
)

_DEFAULT_FORBIDDEN_PATHS: tuple[str, ...] = (
    "/home/piet/.hermes/config.yaml",
    "/home/piet/.hermes/profiles/",
    "/home/piet/.hermes/kanban.db",
    "/home/piet/.openclaw/",
    "/home/piet/vault/_agents/OpenClaw/",
)


@dataclass
class WorkerScopeContractIssue:
    """A fail-closed worker scope-contract validation issue."""

    index: int
    assignee: str
    reason: str


@dataclass
class WorkerScopeContractReport:
    """Validation result for worker-lane child scope contracts."""

    issues: list[WorkerScopeContractIssue]

    @property
    def ok(self) -> bool:
        return not self.issues


def _is_worker_lane(assignee: str) -> bool:
    return assignee.strip() in _WORKER_SCOPE_LANES


def _body_has_scope_contract(body: str) -> bool:
    return "scope_contract:" in body and "allowed_tools:" in body


def _count_scope_contract_blocks(body: str) -> int:
    """Count ``scope_contract:`` block headers in ``body``.

    A worker child must carry EXACTLY ONE structured scope_contract block. A
    second (decoy) block is a spoofing vector: :func:`validate_worker_scope_contracts`
    extracts ``version`` / ``allowed_tools`` via FIRST-match, so it would attest
    against block #1 while a downstream reader could act on a broader block #2.
    Counting block headers lets the validator fail closed on any duplicate.
    A header is a line whose stripped form starts with ``scope_contract:`` — prose
    mentions like ``scope_contract/allowed paths`` do not match.
    """
    return sum(
        1 for line in body.splitlines()
        if line.strip().startswith("scope_contract:")
    )


def _default_worker_scope_contract(
    child: dict,
    *,
    parent_task: object | None = None,
) -> dict:
    """Build a deterministic minimal contract for a worker child body."""
    title = (child.get("title") or "worker task").strip()
    parent_id = getattr(parent_task, "id", "") if parent_task is not None else ""
    objective = title if not parent_id else f"[{parent_id}] {title}"
    return {
        "scope_contract": {
            "version": 2,
            "objective": objective[:280],
            "allowed_systems": ["hermes-agent", "hermes-kanban"],
            "allowed_paths": _collect_absolute_paths(
                getattr(parent_task, "body", "") or ""
            ) if parent_task is not None else [],
            "allowed_tools": list(_BASE_WORKER_ALLOWED_TOOLS),
            "forbidden_systems": list(_DEFAULT_FORBIDDEN_SYSTEMS),
            "forbidden_paths": list(_DEFAULT_FORBIDDEN_PATHS),
            "forbidden_tools": [
                "browser_navigate",
                "browser_click",
                "mcp_linear_save_issue",
                "mcp_linear_save_comment",
                "mcp_linear_save_document",
                "clarify",
            ],
            "ambiguity_policy": "fail_closed_and_ask",
        },
        "completion_policy": {
            "require_scope_attestation": True,
        },
    }


def _yaml_scalar(value: object) -> str:
    text = str(value).replace("\n", " ").strip()
    if not text:
        return "''"
    return text


def _render_yaml_list(name: str, values: list[str], *, indent: str = "  ") -> list[str]:
    if not values:
        return [f"{indent}{name}: []"]
    lines = [f"{indent}{name}:"]
    lines.extend(f"{indent}  - {_yaml_scalar(v)}" for v in values)
    return lines


def _render_scope_contract_yaml(contract: dict) -> str:
    scope = contract.get("scope_contract") or {}
    completion = contract.get("completion_policy") or {}
    lines = [
        "scope_contract:",
        f"  version: {int(scope.get('version', 2))}",
        f"  objective: {_yaml_scalar(scope.get('objective', 'worker task'))}",
    ]
    for key in (
        "allowed_systems",
        "allowed_paths",
        "allowed_tools",
        "forbidden_systems",
        "forbidden_paths",
        "forbidden_tools",
    ):
        vals = scope.get(key) or []
        lines.extend(_render_yaml_list(key, [str(v) for v in vals]))
    lines.append(
        f"  ambiguity_policy: {_yaml_scalar(scope.get('ambiguity_policy', 'fail_closed_and_ask'))}"
    )
    require_attestation = bool(completion.get("require_scope_attestation", True))
    lines.extend([
        "completion_policy:",
        f"  require_scope_attestation: {str(require_attestation).lower()}",
    ])
    return "\n".join(lines)


def _normalize_allowed_tools_block(body: str) -> str:
    """Force the worker scope contract's ``allowed_tools`` to the canonical
    kanban-lifecycle set.

    ``allowed_tools`` is a declarative attestation field — the worker's real
    tools come from its profile config, NOT from this list (it is not
    runtime-enforced). The decomposer LLM frequently lists work tools it thinks
    the worker needs (``file_read``, ``shell:cat ...``, ``terminal``); those
    are unknown to :func:`validate_worker_scope_contracts` and abort the whole
    decomposition silently (logged at debug in the gateway tick). Rewriting the
    block to the canonical kanban tools keeps auto-decompose from aborting
    while preserving every other field the model produced. Handles both block
    (``allowed_tools:`` + ``- item`` lines) and inline (``allowed_tools: [..]``)
    forms. Only the first occurrence (the scope_contract's) is rewritten.
    """
    if "allowed_tools:" not in body:
        return body
    lines = body.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    replaced = False
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not replaced and stripped.startswith("allowed_tools:"):
            base_indent = len(line) - len(line.lstrip())
            indent = " " * base_indent
            out.append(f"{indent}allowed_tools:")
            for tool in _BASE_WORKER_ALLOWED_TOOLS:
                out.append(f"{indent}  - {tool}")
            replaced = True
            i += 1
            # Drop the model's original block list items (deeper "- " lines).
            if stripped == "allowed_tools:":
                while i < n:
                    item = lines[i]
                    item_indent = len(item) - len(item.lstrip())
                    if item.strip().startswith("- ") and item_indent > base_indent:
                        i += 1
                        continue
                    break
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _ensure_worker_scope_contract(
    child: dict,
    *,
    parent_task: object | None = None,
) -> dict:
    """Return ``child`` with a deterministic worker scope block if needed."""
    assignee = child.get("assignee")
    if not isinstance(assignee, str) or not _is_worker_lane(assignee):
        return child

    raw_body = child.get("body")
    body = raw_body if isinstance(raw_body, str) else ""
    if _body_has_scope_contract(body):
        normalized = _normalize_allowed_tools_block(body)
        if normalized != body:
            enriched = dict(child)
            enriched["body"] = normalized
            return enriched
        return child

    contract = _default_worker_scope_contract(child, parent_task=parent_task)
    scope_block = _render_scope_contract_yaml(contract)
    enriched = dict(child)
    enriched["body"] = f"{scope_block}\n\n{body.strip()}".strip()
    return enriched


def _extract_scope_version(body: str) -> int | None:
    if "scope_contract:" not in body:
        return None
    match = re.search(r"(?m)^\s*version:\s*(\d+)\s*$", body)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _extract_allowed_tools(body: str) -> list[str]:
    lines = body.splitlines()
    tools: list[str] = []
    in_allowed_tools = False
    base_indent = 0
    for line in lines:
        stripped = line.strip()
        if not in_allowed_tools:
            if stripped == "allowed_tools:":
                in_allowed_tools = True
                base_indent = len(line) - len(line.lstrip())
            continue
        indent = len(line) - len(line.lstrip())
        if stripped.startswith("- ") and indent > base_indent:
            tools.append(stripped[2:].strip().strip('"\''))
            continue
        if stripped and indent <= base_indent:
            break
    return tools


def validate_worker_scope_contracts(children: list[dict]) -> WorkerScopeContractReport:
    """Fail-closed validation for worker-lane child body contracts."""
    issues: list[WorkerScopeContractIssue] = []
    known_tools = set(_BASE_WORKER_ALLOWED_TOOLS)
    for idx, child in enumerate(children):
        assignee = child.get("assignee")
        assignee_text = assignee if isinstance(assignee, str) else ""
        if not _is_worker_lane(assignee_text):
            continue
        raw_body = child.get("body")
        body = raw_body if isinstance(raw_body, str) else ""
        if _count_scope_contract_blocks(body) > 1:
            issues.append(WorkerScopeContractIssue(
                idx, assignee_text,
                "multiple scope_contract blocks rejected (exactly one allowed)",
            ))
            continue
        version = _extract_scope_version(body)
        if version != 2:
            issues.append(WorkerScopeContractIssue(
                idx, assignee_text, "worker scope contract missing version: 2",
            ))
            continue
        allowed_tools = _extract_allowed_tools(body)
        if not allowed_tools:
            issues.append(WorkerScopeContractIssue(
                idx, assignee_text, "worker scope contract missing allowed_tools",
            ))
            continue
        for tool in allowed_tools:
            normalized = tool.strip().lower()
            if normalized in _BROAD_ALLOWED_TOOL_MARKERS:
                issues.append(WorkerScopeContractIssue(
                    idx, assignee_text, f"broad allowed_tools marker {tool!r}",
                ))
                break
            if normalized in _FORBIDDEN_SCOPE_MARKERS:
                issues.append(WorkerScopeContractIssue(
                    idx, assignee_text, f"forbidden allowed_tools marker {tool!r}",
                ))
                break
            if tool not in known_tools:
                issues.append(WorkerScopeContractIssue(
                    idx, assignee_text, f"unknown allowed_tool {tool!r}",
                ))
                break
        if "completion_policy:" not in body or "require_scope_attestation: true" not in body:
            issues.append(WorkerScopeContractIssue(
                idx,
                assignee_text,
                "worker scope contract missing completion_policy.require_scope_attestation",
            ))
    return WorkerScopeContractReport(issues)


def _normalize_assignee_choice(
    assignee: object,
    *,
    default_assignee: str,
    valid_names: set[str],
) -> str:
    """Return a valid assignee, falling back to ``default_assignee``.

    Fan-out children and the single-task fallback should share the same
    routing guarantee: promoted work must not be left unassigned.
    """
    if not isinstance(assignee, str) or not assignee.strip():
        return default_assignee
    chosen = assignee.strip()
    if chosen not in valid_names:
        return default_assignee
    return chosen


def _normalize_kind_choice(
    kind: object,
    *,
    valid_kinds: frozenset[str],
) -> Optional[str]:
    if not isinstance(kind, str) or not kind.strip():
        return None
    chosen = kind.strip().lower()
    if chosen not in valid_kinds:
        return None
    return chosen


def _children_from_parsed(
    parsed: dict,
    task: "kb.Task",
    *,
    valid_names: set[str],
    default_assignee: str,
) -> tuple[Optional[list[dict]], Optional[str]]:
    """Build the validated, scope-contracted ``children`` list from a
    ``fanout=true`` decomposer response.

    Shared by :func:`decompose_task` (lean path) and
    :func:`plan_and_document` (documented path) so the two NEVER drift on
    how a raw LLM ``tasks`` array becomes the children the DB inserts.
    Returns ``(children, None)`` on success or ``(None, reason)`` on a
    structural error the caller surfaces as ``ok=False``.
    """
    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return None, "decomposer returned fanout=true with empty tasks list"

    # Rewrite invalid assignees to the default fallback. Never leave a
    # task with assignee=None — the user explicitly does not want that.
    children: list[dict] = []
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            return None, f"tasks[{idx}] is not an object"
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            return None, f"tasks[{idx}].title is missing or empty"
        body = entry.get("body")
        if not isinstance(body, str):
            body = ""
        assignee = entry.get("assignee")
        chosen = _normalize_assignee_choice(
            assignee,
            default_assignee=default_assignee,
            valid_names=valid_names,
        )
        kind = _normalize_kind_choice(
            entry.get("kind"),
            valid_kinds=_VALID_TASK_KINDS,
        )
        if (
            isinstance(assignee, str)
            and assignee.strip()
            and assignee.strip() not in valid_names
        ):
            logger.info(
                "decompose: task %s child %d picked unknown assignee %r — "
                "routing to default_assignee %r",
                task.id, idx, assignee, default_assignee,
            )
        parents = entry.get("parents") or []
        if not isinstance(parents, list):
            parents = []
        # Clean parent indices: drop non-int and out-of-range.
        clean_parents = [p for p in parents if isinstance(p, int) and 0 <= p < len(raw_tasks) and p != idx]
        child = {
            "title": title.strip()[:200],
            "body": body.strip(),
            "assignee": chosen,
            "kind": kind,
            "parents": clean_parents,
        }
        children.append(_ensure_worker_scope_contract(child, parent_task=task))

    scope_report = validate_worker_scope_contracts(children)
    if not scope_report.ok:
        first = scope_report.issues[0]
        return None, (
            "worker scope contract invalid before DB insert: "
            f"child {first.index} ({first.assignee}) {first.reason}"
        )

    # Post-validate that the LLM preserved CRITICAL: / MANDATORY: lines and
    # absolute paths / task ids from the parent body. Warn (not block) so
    # the operator can act via `kanban comment <kid_id> "MANDATORY: ..."`.
    preservation = validate_constraint_preservation(task.body or "", children)
    if not preservation.ok:
        if preservation.missing_constraints:
            logger.warning(
                "decompose: task %s — %d CRITICAL/MANDATORY/MUST/NEVER line(s) "
                "from parent body did NOT survive into any kid; set MANDATORY "
                "comments via `kanban comment <kid_id> ... --author user` "
                "before the dispatcher claims them. Missing: %r",
                task.id,
                len(preservation.missing_constraints),
                preservation.missing_constraints,
            )
        if preservation.missing_paths:
            logger.warning(
                "decompose: task %s — %d absolute path(s) from parent body "
                "did NOT survive into any kid: %r",
                task.id,
                len(preservation.missing_paths),
                preservation.missing_paths,
            )
        if preservation.missing_task_ids:
            logger.warning(
                "decompose: task %s — %d task id(s) from parent body did "
                "NOT survive into any kid: %r",
                task.id,
                len(preservation.missing_task_ids),
                preservation.missing_task_ids,
            )

    return children, None


# ── N-Epics P5: konservative Auto-Zuordnung beim Zerlegen ────────────────────
# Der Decomposer sieht die OFFENEN Epics (id+title) und darf das Top-Level-Feld
# "epic" setzen — nur bei klarer inhaltlicher Passung. Validierung hier ist die
# harte Grenze: nur existierende offene Epics aus genau dieser Liste, nie neu
# anlegen, Operator-Zuordnung (vorhandenes epic_id) gewinnt immer.

def _open_epics_context() -> tuple[set[str], str]:
    """(gültige offene Epic-IDs, Prompt-Block) — fail-soft zu "(none)"."""
    ids: set[str] = set()
    lines: list[str] = []
    try:
        with kb.connect_closing() as conn:
            for e in kb.list_epics(conn, include_closed=False):
                ids.add(e["id"])
                lines.append(f"- {e['id']}: {_truncate(e['title'] or '', 120)}")
    except Exception as exc:
        logger.debug("decompose: open-epics lookup failed: %s", exc)
    return ids, "\n".join(lines) if lines else "(none)"


def _apply_epic_choice(task, parsed: dict, open_epic_ids: set[str]) -> None:
    """Wendet die "epic"-Wahl des Decomposers an — konservativ.

    Greift nur, wenn die ID wörtlich aus der angebotenen Open-Epics-Liste
    stammt UND der Task noch kein Epic hat. Alles andere (halluzinierte ID,
    geschlossenes Epic, Unsicherheit=null) bleibt still ohne Epic. Läuft VOR
    dem Decompose-Write, damit die bestehende Root→Kinder-Propagation greift.
    """
    choice = parsed.get("epic")
    if not isinstance(choice, str):
        return
    choice = choice.strip()
    if not choice or choice not in open_epic_ids or task.epic_id:
        return
    try:
        with kb.connect_closing() as conn:
            kb.set_task_epic(conn, task.id, choice)
    except Exception as exc:
        # z.B. zwischenzeitlich geschlossen — konservativ: kein Epic.
        logger.debug("decompose: epic assignment skipped for %s: %s", task.id, exc)


def decompose_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> DecomposeOutcome:
    """Decompose a triage task into a graph of child tasks.

    Returns an outcome describing what happened. Never raises for
    expected failure modes (task not in triage, no aux client
    configured, API error, malformed response, decomposer returned
    fanout=true with empty task list) — those surface via ``ok=False``.
    """
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        return DecomposeOutcome(task_id, False, "unknown task id")
    if task.status != "triage":
        return DecomposeOutcome(
            task_id, False, f"task is not in triage (status={task.status!r})"
        )

    cfg = _load_config()
    orchestrator = _resolve_orchestrator_profile(cfg)
    default_assignee = _resolve_default_assignee(cfg)
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    auto_promote = _coerce_config_bool(
        kanban_cfg.get("auto_promote_children", True), default=True
    )
    roster, valid_names = _build_roster()
    try:
        with kb.connect_closing() as conn:
            _enrich_roster_with_outcome_stats(conn, roster)
    except Exception as exc:
        logger.debug("decompose: profile outcome stats connection failed: %s", exc)

    try:
        from agent.auxiliary_client import (  # type: ignore
            get_auxiliary_extra_body,
            get_text_auxiliary_client,
        )
    except Exception as exc:
        logger.debug("decompose: auxiliary client import failed: %s", exc)
        return DecomposeOutcome(task_id, False, "auxiliary client unavailable")

    try:
        client, model = get_text_auxiliary_client("kanban_decomposer")
    except Exception as exc:
        logger.debug("decompose: get_text_auxiliary_client failed: %s", exc)
        return DecomposeOutcome(task_id, False, "auxiliary client unavailable")

    if client is None or not model:
        return DecomposeOutcome(task_id, False, "no auxiliary client configured")

    open_epic_ids, epics_block = _open_epics_context()
    user_msg = _USER_TEMPLATE.format(
        task_id=task.id,
        title=_truncate(task.title or "", 400),
        body=_truncate(task.body or "(no body)", 4000),
        roster=_format_roster(roster),
        default_assignee=default_assignee,
        epics=epics_block,
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=4000,
            timeout=timeout or 180,
            extra_body=get_auxiliary_extra_body() or None,
        )
    except Exception as exc:
        logger.info(
            "decompose: API call failed for %s (%s)", task_id, exc,
        )
        return DecomposeOutcome(task_id, False, f"LLM error: {type(exc).__name__}")

    try:
        raw = resp.choices[0].message.content or ""
    except Exception:
        raw = ""

    parsed = _extract_json_blob(raw)
    if parsed is None:
        return DecomposeOutcome(task_id, False, "LLM returned malformed JSON")

    # Vor dem Decompose-Write, damit Kinder das Root-Epic erben (N-E3).
    _apply_epic_choice(task, parsed, open_epic_ids)

    fanout = bool(parsed.get("fanout"))
    audit_author = author or kb._profile_author()

    if not fanout:
        # Fall back to single-task spec promotion (same effect as specify).
        new_title = parsed.get("title")
        new_body = parsed.get("body")
        title_val = new_title.strip() if isinstance(new_title, str) and new_title.strip() else None
        body_val = new_body if isinstance(new_body, str) and new_body.strip() else None
        assignee_val = None
        if not task.assignee:
            assignee_val = _normalize_assignee_choice(
                parsed.get("assignee"),
                default_assignee=default_assignee,
                valid_names=valid_names,
            )
        if title_val is None and body_val is None:
            return DecomposeOutcome(
                task_id, False, "decomposer returned fanout=false with no title/body",
            )
        with kb.connect_closing() as conn:
            ok = kb.specify_triage_task(
                conn,
                task_id,
                title=title_val,
                body=body_val,
                assignee=assignee_val,
                author=audit_author,
            )
        if not ok:
            return DecomposeOutcome(
                task_id, False, "task moved out of triage before promotion",
            )
        return DecomposeOutcome(
            task_id, True, "single task (no fanout)",
            fanout=False, new_title=title_val,
        )

    children, child_err = _children_from_parsed(
        parsed, task,
        valid_names=valid_names,
        default_assignee=default_assignee,
    )
    if child_err is not None:
        return DecomposeOutcome(task_id, False, child_err)

    try:
        with kb.connect_closing() as conn:
            child_ids = kb.decompose_triage_task(
                conn,
                task_id,
                root_assignee=orchestrator,
                children=children,
                author=audit_author,
                auto_promote=auto_promote,
                validate_assignees=True,
            )
    except ValueError as exc:
        return DecomposeOutcome(task_id, False, f"DB rejected graph: {exc}")
    except Exception as exc:
        logger.exception("decompose: DB error on task %s", task_id)
        return DecomposeOutcome(task_id, False, f"DB error: {type(exc).__name__}")

    if child_ids is None:
        return DecomposeOutcome(
            task_id, False, "task moved out of triage before decomposition",
        )

    return DecomposeOutcome(
        task_id, True, f"decomposed into {len(child_ids)} children",
        fanout=True, child_ids=child_ids,
    )


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Return task ids currently in the triage column.

    Demand-Funnel-Vorschläge (``created_by`` in ``kb.FUNNEL_CREATED_BY``)
    sind ausgenommen: sie warten auf den Operator-Tap (Annahme = PATCH
    status→ready) und dürfen nie vom Auto-Decomposer gestartet werden.
    """
    with kb.connect_closing() as conn:
        rows = kb.list_tasks(
            conn,
            status="triage",
            tenant=tenant,
            limit=1000,
        )
    return [row.id for row in rows
            if (row.created_by or "") not in kb.FUNNEL_CREATED_BY]


# ---------------------------------------------------------------------------
# Documented ("plan + spec") method — Flow capture Phase B
# ---------------------------------------------------------------------------
#
# The documented method reuses the SAME aux decomposer (richer prompt) and the
# SAME child-building/validation as the lean path, but the BACKEND renders one
# durable Vault plan-spec (narrative on top, subtask table below) from the very
# same object it creates the kanban subtasks from — so the spec, the subtasks,
# and the executed work are one truth (no drift). The Flow capture endpoint
# parks the root in ``scheduled`` first, so the gateway's triage-only
# auto-decompose tick never races the (slow) LLM planning call; the fan-out
# then runs atomically straight from ``scheduled`` (expected_root_status).


def _flow_plans_dir() -> Path:
    """Directory the durable Flow plan-specs are written to.

    Defaults to the shared Vault (``~/vault/03-Agents/_flow-plans``); the
    ``HERMES_FLOW_PLANS_DIR`` env var overrides it (used by tests / the live
    E2E to avoid polluting the real Vault)."""
    raw = os.environ.get("HERMES_FLOW_PLANS_DIR")
    if raw and raw.strip():
        return Path(os.path.expanduser(raw.strip()))
    return Path(os.path.expanduser("~/vault/03-Agents/_flow-plans"))


def flow_plan_path(task_id: str) -> Path:
    """Absolute path of the plan-spec file for ``task_id`` (may not exist)."""
    return _flow_plans_dir() / f"{task_id}.md"


def _write_flow_plan_spec(task_id: str, markdown: str) -> Optional[str]:
    """Write the spec markdown; return the relative filename or ``None`` on
    failure (fail-soft — a spec-write error never aborts the decomposition)."""
    target = flow_plan_path(task_id)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
        return target.name
    except OSError as exc:
        logger.warning("flow-plan: could not write spec for %s: %s", task_id, exc)
        return None


def _md_cell(text: object) -> str:
    """Sanitise a value for a one-line markdown table cell."""
    s = text if isinstance(text, str) else ("" if text is None else str(text))
    return s.replace("\n", " ").replace("\r", " ").replace("|", "/").strip()


def _subtask_summaries(parsed: dict, n: int) -> list[Optional[str]]:
    """Pull the optional per-task ``summary`` (documented addendum), aligned to
    the children list. Index-aligned because ``_children_from_parsed`` keeps the
    same order and length as ``parsed['tasks']``."""
    raw = parsed.get("tasks") or []
    out: list[Optional[str]] = []
    for i in range(n):
        val = None
        if i < len(raw) and isinstance(raw[i], dict):
            v = raw[i].get("summary")
            if isinstance(v, str) and v.strip():
                val = v.strip()
        out.append(val)
    return out


def _render_flow_plan_spec(
    *,
    task: "kb.Task",
    narrative: str,
    rationale: str,
    children: list[dict],
    child_ids: list[str],
    summaries: list[Optional[str]],
    gated: bool,
    single: bool = False,
) -> str:
    """Render the durable plan-spec: frontmatter + narrative block on top, a
    structured subtask table below. Same source object as the kanban subtasks."""
    created = time.strftime("%Y-%m-%d", time.localtime())
    title = (task.title or task.id).strip()
    lines: list[str] = [
        "---",
        "flow_plan: true",
        f"root_task: {task.id}",
        f"created: {created}",
        "method: documented",
        f"gate: {'true' if gated else 'false'}",
        "---",
        "",
        f"# Flow-Plan — {title}",
        "",
        (
            f"> Quelle: Flow-Capture (dokumentierte Methode) · Root `{task.id}`. "
            "Diese Spec ≡ die angelegten Kanban-Subtasks ≡ die ausgeführte "
            "Arbeit — eine Wahrheit."
        ),
        "",
        "## Narrativ",
        "",
        narrative or "_(Kein Narrativ vom Planer geliefert.)_",
    ]
    if rationale:
        lines += ["", f"_Rationale:_ {rationale}"]
    lines.append("")

    if single:
        c = children[0] if children else {"title": title, "assignee": None}
        lines += ["## Aufgabe", ""]
        lines.append(f"- **{_md_cell(c.get('title'))}** → Profil `{c.get('assignee') or '—'}`")
        if summaries and summaries[0]:
            lines.append(f"  - {_md_cell(summaries[0])}")
    else:
        lines += [f"## Subtasks ({len(children)})", ""]
        lines.append("| # | Subtask | Profil | Abhängig von | Liefert |")
        lines.append("|---|---------|--------|--------------|---------|")
        for idx, c in enumerate(children):
            cid = child_ids[idx] if idx < len(child_ids) else "?"
            deps = c.get("parents") or []
            deps_str = ", ".join(f"#{p + 1}" for p in deps) if deps else "–"
            summ = summaries[idx] if idx < len(summaries) and summaries[idx] else (
                (c.get("body") or "").split("\n", 1)[0]
            )
            lines.append(
                f"| {idx + 1} | {_md_cell(c.get('title'))} (`{cid}`) | "
                f"`{c.get('assignee') or '—'}` | {deps_str} | {_md_cell(summ)[:160]} |"
            )
    lines += ["", "## Ausführung", ""]
    if gated:
        lines.append(
            "- **Gate aktiv:** Subtasks sind in `scheduled` gehalten und werden erst "
            "durch „Go ausführen“ im Flow-Tab freigegeben "
            "(dann `ready` → Dispatcher)."
        )
    else:
        lines.append(
            "- **Auto:** Subtasks sind sofort dispatchbar (`todo`/`ready`) — "
            "der Dispatcher übernimmt."
        )
    lines.append(
        f"- Root `{task.id}` bleibt offen und wacht auf, wenn alle Subtasks "
        "abgeschlossen sind."
    )
    lines.append("")
    return "\n".join(lines)


def _record_flow_plan(
    task_id: str,
    relpath: Optional[str],
    gate: bool,
    author: str,
    n_children: int,
    *,
    document: bool,
) -> None:
    """Mark the root with a ``flow_plan`` event (documented method, so the
    dashboard can surface the spec link) + an audit comment noting the gate
    state. Fail-soft — never aborts the capture."""
    try:
        with kb.connect_closing() as conn:
            if document and relpath:
                kb.add_event(conn, task_id, "flow_plan", {"spec": relpath, "gated": bool(gate)})
            if document:
                if relpath:
                    note = f"Flow-Plan-Spec geschrieben: {relpath}"
                else:
                    note = "Flow-Plan dokumentiert (Spec-Schreiben fehlgeschlagen, siehe Log)."
            else:
                note = "Flow-Plan (Lean) — keine Spec (Lean schreibt bewusst keine Vault-Spec)."
            if gate and n_children:
                note += (
                    f" · Gate aktiv: {n_children} Subtask(s) gehalten in 'scheduled' "
                    "bis „Go ausführen“."
                )
            kb.add_comment(conn, task_id, author, note)
    except Exception as exc:  # noqa: BLE001 — marker write must never fail the capture
        logger.warning("flow-plan: could not record spec marker for %s: %s", task_id, exc)


def _apply_single_task_from_scheduled(
    task_id: str,
    title: Optional[str],
    body: Optional[str],
    assignee: Optional[str],
    target_status: str,
    author: str,
) -> bool:
    """Tighten + land a fanout=false documented root straight from its parked
    ``scheduled`` state: ``gate`` keeps it ``scheduled`` (held); ``auto`` flips
    it to ``todo`` and lets ``recompute_ready`` promote it. Atomic + guarded on
    the current ``scheduled`` status so a concurrent dispatch can't be lost."""
    assignee_c = kb._canonical_assignee(assignee) if assignee else None
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET title = ?, body = ?, assignee = ?, status = ? "
                "WHERE id = ? AND status = 'scheduled'",
                (title, body, assignee_c, target_status, task_id),
            )
            updated = cur.rowcount == 1
        if not updated:
            return False
        kb.add_event(conn, task_id, "specified", {"flow_plan": True})
        if target_status == "todo":
            kb.recompute_ready(conn)
    return True


def plan_and_document(
    task_id: str,
    *,
    gate: bool,
    document: bool = True,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> DecomposeOutcome:
    """Backend-driven Flow capture planner (both planning modes).

    ``document=True`` is the documented method: rich decompose (narrative +
    structured subtasks) + a durable Vault plan-spec. ``document=False`` is the
    lean method routed through this same backend path (used only for the
    lean+GATE combo — lean+auto stays on the Stufe-A POST /tasks tick): base
    prompt, no spec.

    ``gate=True`` holds the subtasks in ``scheduled`` until an explicit release
    (Flow "Go ausführen"); ``gate=False`` auto-promotes them like today.

    PRECONDITION: ``task_id`` is parked in ``scheduled`` (the Flow capture
    endpoint parks it before calling this, so the gateway's triage-only
    auto-decompose tick can never see it during the LLM call). Returns an
    outcome with ``spec_relpath`` (documented only) + ``gated`` set. Never
    raises for expected failure modes — they surface via ``ok=False``.
    """
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        return DecomposeOutcome(task_id, False, "unknown task id")
    if task.status != "scheduled":
        return DecomposeOutcome(
            task_id, False,
            f"flow-plan root must be parked in 'scheduled' (status={task.status!r})",
        )

    cfg = _load_config()
    orchestrator = _resolve_orchestrator_profile(cfg)
    default_assignee = _resolve_default_assignee(cfg)
    roster, valid_names = _build_roster()
    try:
        with kb.connect_closing() as conn:
            _enrich_roster_with_outcome_stats(conn, roster)
    except Exception as exc:
        logger.debug("flow-plan: profile outcome stats connection failed: %s", exc)

    try:
        from agent.auxiliary_client import (  # type: ignore
            get_auxiliary_extra_body,
            get_text_auxiliary_client,
        )
    except Exception as exc:
        logger.debug("flow-plan: auxiliary client import failed: %s", exc)
        return DecomposeOutcome(task_id, False, "auxiliary client unavailable")

    try:
        client, model = get_text_auxiliary_client("kanban_decomposer")
    except Exception as exc:
        logger.debug("flow-plan: get_text_auxiliary_client failed: %s", exc)
        return DecomposeOutcome(task_id, False, "auxiliary client unavailable")

    if client is None or not model:
        return DecomposeOutcome(task_id, False, "no auxiliary client configured")

    open_epic_ids, epics_block = _open_epics_context()
    user_msg = _USER_TEMPLATE.format(
        task_id=task.id,
        title=_truncate(task.title or "", 400),
        body=_truncate(task.body or "(no body)", 4000),
        roster=_format_roster(roster),
        default_assignee=default_assignee,
        epics=epics_block,
    )

    system_prompt = _SYSTEM_PROMPT + _DOCUMENTED_PROMPT_ADDENDUM if document else _SYSTEM_PROMPT
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=4000,
            timeout=timeout or 180,
            extra_body=get_auxiliary_extra_body() or None,
        )
    except Exception as exc:
        logger.info("flow-plan: API call failed for %s (%s)", task_id, exc)
        return DecomposeOutcome(task_id, False, f"LLM error: {type(exc).__name__}")

    try:
        raw = resp.choices[0].message.content or ""
    except Exception:
        raw = ""

    parsed = _extract_json_blob(raw)
    if parsed is None:
        return DecomposeOutcome(task_id, False, "LLM returned malformed JSON")

    # Vor dem Decompose-Write, damit Kinder das Root-Epic erben (N-E3).
    _apply_epic_choice(task, parsed, open_epic_ids)

    narrative = parsed.get("narrative")
    narrative = narrative.strip() if isinstance(narrative, str) and narrative.strip() else ""
    rationale = parsed.get("rationale")
    rationale = rationale.strip() if isinstance(rationale, str) and rationale.strip() else ""
    audit_author = author or kb._profile_author()
    fanout = bool(parsed.get("fanout"))

    if fanout:
        children, child_err = _children_from_parsed(
            parsed, task, valid_names=valid_names, default_assignee=default_assignee,
        )
        if child_err is not None:
            return DecomposeOutcome(task_id, False, child_err)
        summaries = _subtask_summaries(parsed, len(children))
        child_status = "scheduled" if gate else "todo"
        try:
            with kb.connect_closing() as conn:
                child_ids = kb.decompose_triage_task(
                    conn,
                    task_id,
                    root_assignee=orchestrator,
                    children=children,
                    author=audit_author,
                    auto_promote=(not gate),
                    initial_child_status=child_status,
                    expected_root_status="scheduled",
                    validate_assignees=True,
                )
        except ValueError as exc:
            return DecomposeOutcome(task_id, False, f"DB rejected graph: {exc}")
        except Exception:
            logger.exception("flow-plan: DB error on task %s", task_id)
            return DecomposeOutcome(task_id, False, "DB error during fan-out")
        if child_ids is None:
            return DecomposeOutcome(
                task_id, False, "flow-plan root left 'scheduled' before fan-out",
            )
        relpath = None
        if document:
            markdown = _render_flow_plan_spec(
                task=task, narrative=narrative, rationale=rationale,
                children=children, child_ids=child_ids, summaries=summaries, gated=gate,
            )
            relpath = _write_flow_plan_spec(task_id, markdown)
        _record_flow_plan(task_id, relpath, gate, audit_author, len(child_ids), document=document)
        kind = "documented plan" if document else "lean plan"
        return DecomposeOutcome(
            task_id, True, f"{kind} with {len(child_ids)} subtasks",
            fanout=True, child_ids=child_ids, spec_relpath=relpath, gated=gate,
        )

    # fanout=false — single atomic task, still documented with a (1-row) spec.
    new_title = parsed.get("title")
    new_body = parsed.get("body")
    title_val = new_title.strip() if isinstance(new_title, str) and new_title.strip() else (task.title or "")
    body_val = new_body if isinstance(new_body, str) and new_body.strip() else task.body
    assignee_val = task.assignee
    if not assignee_val:
        assignee_val = _normalize_assignee_choice(
            parsed.get("assignee"), default_assignee=default_assignee, valid_names=valid_names,
        )
    target_status = "scheduled" if gate else "todo"
    ok = _apply_single_task_from_scheduled(
        task_id, title_val, body_val, assignee_val, target_status, audit_author,
    )
    if not ok:
        return DecomposeOutcome(
            task_id, False, "flow-plan root left 'scheduled' before promotion",
        )
    relpath = None
    if document:
        summaries = _subtask_summaries(parsed, 1)
        markdown = _render_flow_plan_spec(
            task=task, narrative=narrative, rationale=rationale,
            children=[{"title": title_val, "assignee": assignee_val, "parents": []}],
            child_ids=[task_id], summaries=summaries, gated=gate, single=True,
        )
        relpath = _write_flow_plan_spec(task_id, markdown)
    _record_flow_plan(task_id, relpath, gate, audit_author, 0, document=document)
    kind = "documented single task" if document else "lean single task"
    return DecomposeOutcome(
        task_id, True, f"{kind} (no fanout)",
        fanout=False, new_title=title_val, spec_relpath=relpath, gated=gate,
    )
