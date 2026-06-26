"""Kanban diagnostics — structured, actionable distress signals for tasks.

A ``Diagnostic`` is a machine-readable description of something that's wrong
with a kanban task: a hallucinated card id, a spawn crash-loop, a task
stuck blocked for too long, etc. Each one carries:

* A **kind** (canonical code; UI/tests match on this).
* A **severity** (``warning`` / ``error`` / ``critical``).
* A **title** (one-line human description) and **detail** (longer text).
* A list of **suggested actions** — structured entries the dashboard
  turns into buttons and the CLI turns into hints.

Rules run over (task, recent events, recent runs) and emit diagnostics.
They are stateless and read-only — no DB writes. Callers compute
diagnostics on demand (on ``/board`` load, ``/tasks/:id`` fetch, or
``hermes kanban diagnostics``).

Design goals:

* Fixable-on-the-operator's-side signals only (missing config, phantom
  ids, crash loop). Not "the provider returned 502 once" — that's a
  transient runtime blip, not a diagnostic.
* Recoverable: every diagnostic comes with at least one suggested
  recovery action the operator can actually take from the UI.
* Auto-clearing: when the underlying failure mode resolves (a clean
  ``completed`` event arrives, a spawn succeeds, the task gets
  unblocked), the diagnostic stops firing. The audit event trail stays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
import json
import re
import time


# Severity rungs, ordered least → most urgent. The UI colors them
# amber (warning), orange (error), red (critical). Sorted outputs put
# critical first so operators see the worst fires at the top.
SEVERITY_ORDER = ("warning", "error", "critical")


def severity_at_or_above(severity: Optional[str], threshold: Optional[str]) -> bool:
    """Return True when ``severity`` meets or exceeds ``threshold``."""
    if threshold is None:
        return True
    if severity not in SEVERITY_ORDER or threshold not in SEVERITY_ORDER:
        return False
    return SEVERITY_ORDER.index(severity) >= SEVERITY_ORDER.index(threshold)


@dataclass
class DiagnosticAction:
    """A single recovery action attached to a diagnostic.

    The ``kind`` determines how both the UI and CLI render it:

    * ``reclaim`` / ``reassign`` — POST to the matching /tasks/:id/*
      endpoint; dashboard wires into the existing recovery popover.
    * ``unblock`` — PATCH status back to ``ready`` (for stuck-blocked
      diagnostics).
    * ``cli_hint`` — print/copy a shell command (e.g.
      ``hermes -p <profile> auth``). No HTTP side effect.
    * ``open_docs`` — deep-link to the docs URL named in ``payload.url``.
    * ``comment`` — nudge the operator to add a comment (for
      stuck-blocked tasks that need human input).

    ``suggested=True`` marks the action as the recommended first step;
    the UI highlights it. Multiple actions can be suggested if they're
    equally valid.
    """

    kind: str
    label: str
    payload: dict = field(default_factory=dict)
    suggested: bool = False

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "payload": self.payload,
            "suggested": self.suggested,
        }


@dataclass
class Diagnostic:
    """One active distress signal on a task."""

    kind: str
    severity: str  # "warning" | "error" | "critical"
    title: str
    detail: str
    actions: list[DiagnosticAction] = field(default_factory=list)
    first_seen_at: int = 0
    last_seen_at: int = 0
    count: int = 1
    # Optional: the run id this diagnostic is scoped to. None = task-wide.
    run_id: Optional[int] = None
    # Optional structured payload for the UI (phantom ids, failure count).
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "actions": [a.to_dict() for a in self.actions],
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "count": self.count,
            "run_id": self.run_id,
            "data": self.data,
        }


# ---------------------------------------------------------------------------
# Rule helpers
# ---------------------------------------------------------------------------

def _task_field(task, name, default=None):
    """Read a field from a task regardless of representation.

    Callers pass sqlite3.Row (dict-like with [] but no attribute
    access), kanban_db.Task dataclasses (attribute access), or plain
    dicts (both). This normalises them so rule functions don't have
    to branch on type each time.
    """
    if task is None:
        return default
    # sqlite Row + plain dicts both support mapping access; Row also
    # supports .keys().
    try:
        # Row raises IndexError if the key isn't a column in the query;
        # dicts return default via .get. Handle both.
        if hasattr(task, "keys") and name in task.keys():
            return task[name]
    except Exception:
        pass
    if isinstance(task, dict):
        return task.get(name, default)
    return getattr(task, name, default)


def _parse_payload(ev) -> dict:
    """Tolerate event.payload being either a dict or a JSON string."""
    p = _task_field(ev, "payload", None)
    if p is None:
        return {}
    if isinstance(p, dict):
        return p
    if isinstance(p, str):
        try:
            return json.loads(p) or {}
        except Exception:
            return {}
    return {}


def _event_kind(ev) -> str:
    return _task_field(ev, "kind", "") or ""


def _event_ts(ev) -> int:
    t = _task_field(ev, "created_at", 0)
    return int(t or 0)


def _normalise_text(value: Any) -> str:
    return str(value or "").casefold()


def _text_contains_any(text: str, terms: Iterable[str]) -> list[str]:
    """Return configured terms that appear in ``text`` (case-insensitive)."""
    haystack = _normalise_text(text)
    return [term for term in terms if term in haystack]


def _stringify_payload(value: Any) -> str:
    """Best-effort text extraction for diagnostic marker scans.

    Event payloads are JSON-ish and not schema-stable across all callers.
    Keep this read-only and conservative: recursively include scalar values
    without assuming a dedicated reason/comment field exists.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        return " ".join(
            _stringify_payload(part)
            for item in value.items()
            for part in item
        )
    if isinstance(value, (list, tuple, set)):
        return " ".join(_stringify_payload(item) for item in value)
    return str(value)


def _task_text(task: Any) -> str:
    return "\n".join(
        str(_task_field(task, field_name, "") or "")
        for field_name in ("title", "body")
    )


def _event_text(ev: Any) -> str:
    payload = _parse_payload(ev)
    raw_payload = _task_field(ev, "payload", "")
    return "\n".join(
        part for part in (
            _event_kind(ev),
            _stringify_payload(payload),
            raw_payload if isinstance(raw_payload, str) else "",
        ) if part
    )


def _run_text(run: Any) -> str:
    return "\n".join(
        str(_task_field(run, field_name, "") or "")
        for field_name in ("summary", "error", "result")
    )


def _combined_text(task: Any, events: Iterable[Any], runs: Iterable[Any]) -> str:
    return "\n".join(
        [
            _task_text(task),
            *(_event_text(ev) for ev in events),
            *(_run_text(run) for run in runs),
        ]
    )


def _active_hallucination_events(
    events: Iterable[Any],
    kind: str,
) -> list[Any]:
    """Return events of ``kind`` that have no ``completed``/``edited``
    event *strictly after* them. Walks chronologically: each clean
    event resets the accumulator; each matching event gets appended.

    Events must be sorted by id (i.e. arrival order); callers pass the
    task's full event list which the DB already returns in that order.
    """
    # Events arrive sorted by id asc (chronological). Walk once, track
    # which hallucination events are still "active" (no clean event
    # supersedes them).
    active: list[Any] = []
    for ev in events:
        k = _event_kind(ev)
        if k in {"completed", "edited"}:
            active.clear()
        elif k == kind:
            active.append(ev)
    return active
# Standard always-available actions. Every diagnostic can offer these as
# fallbacks regardless of kind — they're the two baseline recovery
# primitives the kernel supports.
def _generic_recovery_actions(task: Any, *, running: bool) -> list[DiagnosticAction]:
    out: list[DiagnosticAction] = []
    if running:
        out.append(DiagnosticAction(
            kind="reclaim",
            label="Reclaim task",
            payload={},
        ))
    out.append(DiagnosticAction(
        kind="reassign",
        label="Reassign to different profile",
        payload={"reclaim_first": running},
    ))
    return out


# ---------------------------------------------------------------------------
# Rule implementations
# ---------------------------------------------------------------------------

# Each rule takes (task, events, runs, now_ts, config) and returns
# zero or more Diagnostic instances. ``events`` / ``runs`` are lists of
# kanban_db.Event / kanban_db.Run (or plain dicts matching the same
# shape — for test convenience).

RuleFn = Callable[[Any, list[Any], list[Any], int, dict], list[Diagnostic]]


_REVIEWER_EXECUTION_TERMS = (
    "führe reale gates aus",
    "fuehre reale gates aus",
    "reale gates laufen",
    "reale gates laufen mindestens",
    "run tests",
    "run pytest",
    "execute scripts/run_tests.sh",
    "run scripts/run_tests.sh",
    "execute run_tests",
    "run build",
    "run lint",
    "run ruff",
    "run git diff --check",
    "execute git diff --check",
    "prüfe im repo",
    "pruefe im repo",
    "terminal-gates laufen",
    "terminal gates laufen",
)

# Hard exemptions explicitly scope the reviewer to a verdict/evidence-only
# lane or negate execution. They suppress even when a generic phrase like
# "run tests" appears inside the negation.
_REVIEWER_VERDICT_ONLY_EXEMPT_TERMS = (
    "verdict-only",
    "verdict only",
    "read parent evidence",
    "do not run tests",
    "don't run tests",
    "do not execute tests",
    "not run tests",
    "only assess",
    "nur verdict",
    "evidence-only",
)

# Weak evidence mentions are enough to suppress evidence-only cards that quote
# passed gates, but not direct gate imperatives such as "Reale Gates laufen".
_REVIEWER_WEAK_EVIDENCE_EXEMPT_TERMS = (
    "parent evidence",
    "parent handoff",
    "evidence says",
    "evidence shows",
    "test results passed",
    "tests passed",
    "confirm the evidence",
)

_REVIEWER_DIRECT_EXECUTION_TERMS = tuple(
    term for term in _REVIEWER_EXECUTION_TERMS if term not in {"run tests", "run pytest"}
)

_REVIEWER_PASSED_GATE_EVIDENCE_RE = re.compile(
    r"\b(?:run pytest|run tests|scripts/run_tests\.sh|py_compile|ruff|git diff --check)\b"
    r".{0,160}\bpassed\b",
    re.IGNORECASE,
)
_REVIEWER_DIRECT_RUN_TESTS_RE = re.compile(
    r"(?:\b(?:reviewer|verifier)\s*:\s*|\b(?:must|please|needs? to)\s+)"
    r"run\s+(?:pytest|tests?)\b"
    r"|\brun\s+(?:pytest|tests?)\b.{0,120}\bin the repo\b"
    r"|\brun\s+(?:pytest|tests?)\b(?!.{0,160}\bpassed\b)",
    re.IGNORECASE,
)


def _reviewer_text_has_direct_execution_request(text: str) -> bool:
    """Return True for direct reviewer gate-execution imperatives.

    Evidence-only handoffs often quote commands (``run pytest ... passed``).
    Treat those as evidence unless the text contains a clear reviewer-directed
    execution cue or a stronger execution term such as ``Reale Gates laufen``.
    """
    return bool(
        _text_contains_any(text, _REVIEWER_DIRECT_EXECUTION_TERMS)
        or _REVIEWER_DIRECT_RUN_TESTS_RE.search(text)
    )


def _reviewer_text_has_passed_gate_evidence(text: str) -> bool:
    return bool(_REVIEWER_PASSED_GATE_EVIDENCE_RE.search(text))

_REVIEWER_BLOCKED_MISMATCH_TERMS = (
    "reviewer cannot run",
    "reviewer can't run",
    "reviewer has no terminal",
    "no terminal access",
    "role/tool mismatch",
    "cannot execute gates",
    "can't execute gates",
    "verdict-only reviewer asked to run",
)

_SUPERSEDED_REVIEW_MARKER_TERMS = (
    "superseded",
    "audit-only",
    "audit only",
    "removed as parent",
    "replaced by",
)

_REVIEW_LIKE_TERMS = (
    "review",
    "reviewer",
    "verifier",
    "verdict",
    "needs revision",
    "review-required",
    "request_changes",
)


def _is_review_like_task(task: Any, text: str) -> bool:
    assignee = _normalise_text(_task_field(task, "assignee", ""))
    if assignee in {"reviewer", "verifier"}:
        return True
    if assignee not in {"coder", "premium"}:
        return False
    return bool(_text_contains_any(text, _REVIEW_LIKE_TERMS))


def _superseded_review_markers(text: str) -> list[str]:
    return _text_contains_any(text, _SUPERSEDED_REVIEW_MARKER_TERMS)


def _aux_slot_explicit(slot: Any) -> bool:
    """Return True if the auxiliary slot has user-supplied non-default fields.

    Defaults from ``DEFAULT_CONFIG`` use ``provider: "auto"`` with empty
    model/base_url/api_key — that path falls through to the main model. An
    "explicit" config is one where the user actively set a provider (not
    "auto"), or supplied a model / base_url / api_key.
    """
    if not isinstance(slot, dict):
        return False
    provider = str(slot.get("provider") or "").strip().lower()
    if provider and provider != "auto":
        return True
    for key in ("model", "base_url", "api_key"):
        if str(slot.get(key) or "").strip():
            return True
    return False


def _main_model_visible(raw_config: Any) -> bool:
    """Best-effort check that a main model is configured.

    Diagnostics runs in the dashboard process which may not share the CLI's
    runtime state, so we read the raw config dict. If we cannot prove the
    main model is set, we err on the side of NOT firing the diagnostic.
    """
    if not isinstance(raw_config, dict):
        return False
    model_cfg = raw_config.get("model")
    if isinstance(model_cfg, dict):
        provider = str(model_cfg.get("provider") or "").strip()
        model = str(
            model_cfg.get("default")
            or model_cfg.get("model")
            or model_cfg.get("name")
            or ""
        ).strip()
        return bool(provider and model)
    return bool(str(model_cfg or "").strip())


def triage_aux_status(config: Optional[dict]) -> Optional[dict]:
    """Inspect raw config and report whether triage paths look configured.

    Returns ``None`` when config context is unavailable (suppress diagnostic
    to avoid noisy false positives in tests / low-level callers). Otherwise
    returns a dict with:

      - ``auto_decompose``: bool — whether the dispatcher auto-runs decompose
      - ``decomposer_explicit``: bool — user-supplied decomposer slot
      - ``specifier_explicit``: bool — user-supplied specifier slot
      - ``main_model_visible``: bool — main model can serve as auto fallback
    """
    if not isinstance(config, dict):
        return None

    explicit = config.get("triage_aux_status")
    if isinstance(explicit, dict):
        return explicit

    aux = config.get("auxiliary")
    kanban_cfg = config.get("kanban") if isinstance(config.get("kanban"), dict) else {}

    # Have we been handed any config context at all? When neither auxiliary
    # nor kanban nor model keys are present, the caller is a low-level test
    # passing {} — stay silent.
    if (
        not isinstance(aux, dict)
        and not kanban_cfg
        and "model" not in config
    ):
        return None

    decomposer_explicit = False
    specifier_explicit = False
    if isinstance(aux, dict):
        decomposer_explicit = _aux_slot_explicit(aux.get("kanban_decomposer"))
        specifier_explicit = _aux_slot_explicit(aux.get("triage_specifier"))

    # ``auto_decompose`` defaults to True per kanban DEFAULT_CONFIG.
    auto_decompose = True
    if isinstance(kanban_cfg, dict) and "auto_decompose" in kanban_cfg:
        auto_decompose = bool(kanban_cfg.get("auto_decompose"))

    return {
        "auto_decompose": auto_decompose,
        "decomposer_explicit": decomposer_explicit,
        "specifier_explicit": specifier_explicit,
        "main_model_visible": _main_model_visible(config),
    }


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _rule_reviewer_role_tool_mismatch(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Warn when a verdict-only reviewer card asks for repo gate execution."""
    assignee = _normalise_text(_task_field(task, "assignee", ""))
    if assignee != "reviewer":
        return []
    status = _task_field(task, "status")
    if status not in {"ready", "running", "blocked", "todo"}:
        return []

    task_text = _task_text(task)
    matched = _text_contains_any(task_text, _REVIEWER_EXECUTION_TERMS)
    if not matched:
        return []
    if _text_contains_any(task_text, _REVIEWER_VERDICT_ONLY_EXEMPT_TERMS):
        return []
    if (
        (
            _text_contains_any(task_text, _REVIEWER_WEAK_EVIDENCE_EXEMPT_TERMS)
            or _reviewer_text_has_passed_gate_evidence(task_text)
        )
        and not _reviewer_text_has_direct_execution_request(task_text)
    ):
        return []

    all_text = _combined_text(task, events, runs)
    severity = "warning"
    if status == "blocked" and _text_contains_any(
        all_text, _REVIEWER_BLOCKED_MISMATCH_TERMS,
    ):
        severity = "error"

    task_id = _task_field(task, "id") or "<task_id>"
    return [Diagnostic(
        kind="reviewer_role_tool_mismatch",
        severity=severity,
        title="Reviewer task asks for repo gate execution",
        detail=(
            "This task is assigned to the verdict-only reviewer lane but its "
            "title/body appears to ask the reviewer to execute repository "
            "gates. Use a coder/verifier evidence task to run tests, lint, "
            "or git checks, then ask reviewer for a verdict over that evidence."
        ),
        actions=[
            DiagnosticAction(
                kind="cli_hint",
                label="Inspect task context",
                payload={"command": f"hermes kanban show {task_id}"},
                suggested=True,
            ),
            DiagnosticAction(
                kind="cli_hint",
                label="Use evidence task before reviewer verdict",
                payload={
                    "recommended_shape": (
                        "coder_or_verifier_evidence_then_reviewer_verdict"
                    ),
                },
            ),
        ],
        first_seen_at=now,
        last_seen_at=now,
        count=1,
        data={
            "assignee": "reviewer",
            "matched_imperatives": matched,
            "recommended_shape": (
                "coder_or_verifier_evidence_then_reviewer_verdict"
            ),
        },
    )]


def _rule_superseded_blocked_review_artifact(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Surface explicit supersede/audit markers on blocked review cards.

    The rule is marker-based only. It does not inspect dependency graph state
    and therefore never claims that a blocked card is truly detached.
    """
    if _task_field(task, "status") != "blocked":
        return []
    text = _combined_text(task, events, runs)
    if not _is_review_like_task(task, text):
        return []
    markers = _superseded_review_markers(text)
    if not markers:
        return []

    task_id = _task_field(task, "id") or "<task_id>"
    first_seen = min((_event_ts(ev) for ev in events if _event_ts(ev)), default=now)
    last_seen = max((_event_ts(ev) for ev in events if _event_ts(ev)), default=now)
    return [Diagnostic(
        kind="superseded_blocked_review_artifact",
        severity="warning",
        title="Blocked review artifact has superseded/audit marker",
        detail=(
            "This blocked review-like task contains an explicit superseded or "
            "audit-only marker. This diagnostic is marker-based and operator-"
            "visible only; graph state was not checked, so it does not prove "
            "that the card is detached from all active work."
        ),
        actions=[
            DiagnosticAction(
                kind="cli_hint",
                label="Inspect task before acting",
                payload={"command": f"hermes kanban show {task_id}"},
                suggested=True,
            ),
            DiagnosticAction(
                kind="cli_hint",
                label="No automatic action; archive only after operator confirmation",
                payload={"safe_default": "leave_blocked_as_audit_artifact"},
            ),
            DiagnosticAction(
                kind="cli_hint",
                label="Use sanctioned operator-gated helper for real rewiring",
                payload={
                    "helpers": [
                        "kanban_rewire_superseding_review",
                        "kanban_ensure_needs_revision_fix",
                    ],
                },
            ),
        ],
        first_seen_at=first_seen,
        last_seen_at=last_seen,
        count=1,
        data={
            "audit_only_marker_present": True,
            "graph_state_checked": False,
            "matched_terms": markers,
            "safe_default": "leave_blocked_as_audit_artifact",
        },
    )]


def _rule_stale_review_block_needs_classification(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Warn on old blocked review-like cards that lack supersede markers.

    This intentionally avoids the stronger "detached" claim because the rule
    does not receive parent/child graph state.
    """
    if _task_field(task, "status") != "blocked":
        return []
    text = _combined_text(task, events, runs)
    if not _is_review_like_task(task, text):
        return []
    if _superseded_review_markers(text):
        return []

    last_blocked_ts = 0
    for ev in events:
        if _event_kind(ev) == "blocked":
            last_blocked_ts = max(last_blocked_ts, _event_ts(ev))
    if last_blocked_ts == 0:
        return []

    warning_seconds = float(cfg.get("stale_review_block_hours", 2)) * 3600
    error_seconds = float(cfg.get("stale_review_block_error_hours", 24)) * 3600
    age_seconds = now - last_blocked_ts
    if age_seconds < warning_seconds:
        return []
    severity = "error" if age_seconds >= error_seconds else "warning"

    task_id = _task_field(task, "id") or "<task_id>"
    return [Diagnostic(
        kind="stale_review_block_needs_classification",
        severity=severity,
        title="Stale blocked review card needs classification",
        detail=(
            "This review-like task has been blocked past the configured stale "
            "threshold and has no explicit superseded/audit-only marker. "
            "This diagnostic is age-based only; graph state was not checked, "
            "so an operator should classify whether it is an audit artifact, "
            "a stale blocker, or needs a bounded fix task."
        ),
        actions=[
            DiagnosticAction(
                kind="cli_hint",
                label="Inspect whether the block is still active",
                payload={"command": f"hermes kanban show {task_id}"},
                suggested=True,
            ),
            DiagnosticAction(
                kind="cli_hint",
                label="If superseded, add an explicit audit/supersede comment",
                payload={"requires_operator_classification": True},
            ),
            DiagnosticAction(
                kind="cli_hint",
                label="If still active, create a bounded fix task; do not auto-rewire",
                payload={"graph_state_checked": False},
            ),
        ],
        first_seen_at=last_blocked_ts,
        last_seen_at=last_blocked_ts,
        count=1,
        data={
            "blocked_age_seconds": int(age_seconds),
            "review_like": True,
            "graph_state_checked": False,
            "requires_operator_classification": True,
            "stale_review_block_hours": cfg.get("stale_review_block_hours", 2),
            "stale_review_block_error_hours": cfg.get(
                "stale_review_block_error_hours", 24,
            ),
        },
    )]


def _rule_hallucinated_cards(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Blocked-hallucination gate fires: a worker called kanban_complete
    with created_cards that didn't exist or weren't created by the
    completing profile. Task stayed in its prior state; the operator
    needs to decide how to proceed.

    Auto-clears when a successful completion (or edit) follows the
    blocked event.
    """
    hits = _active_hallucination_events(events, "completion_blocked_hallucination")
    if not hits:
        return []
    phantom_ids: list[str] = []
    first = _event_ts(hits[0])
    last = _event_ts(hits[-1])
    for ev in hits:
        payload = _parse_payload(ev)
        for pid in payload.get("phantom_cards", []) or []:
            if pid not in phantom_ids:
                phantom_ids.append(pid)
    running = _task_field(task, "status") == "running"
    actions: list[DiagnosticAction] = []
    actions.append(DiagnosticAction(
        kind="comment",
        label="Add a comment explaining what to do",
        suggested=False,
    ))
    actions.extend(_generic_recovery_actions(task, running=running))
    return [Diagnostic(
        kind="hallucinated_cards",
        severity="error",
        title="Worker claimed cards that don't exist",
        detail=(
            f"The completing worker declared created_cards that either didn't "
            f"exist or weren't created by its profile. The completion was "
            f"blocked and the task stayed in its prior state. "
            f"Usually means the worker hallucinated ids instead of capturing "
            f"return values from kanban_create."
        ),
        actions=actions,
        first_seen_at=first,
        last_seen_at=last,
        count=len(hits),
        data={"phantom_ids": phantom_ids},
    )]


def _rule_triage_aux_unavailable(task, events, runs, now, cfg) -> list[Diagnostic]:
    """A triage task cannot leave triage without an auxiliary helper.

    With the auto-decompose dispatcher (kanban.auto_decompose, default True),
    triage tasks fan out via ``auxiliary.kanban_decomposer`` and fall back to
    ``auxiliary.triage_specifier`` when the decomposer returns ``fanout=false``.
    With auto-decompose off, the user must run ``hermes kanban specify``,
    which only needs ``auxiliary.triage_specifier``.

    The default slot is ``provider: auto`` → auto-falls back to the main model,
    so this rule only fires when:

      - the relevant slot is explicitly set to something broken, OR
      - the auto fallback has no main model to fall back to.

    Config context is required; pass {} from tests to keep the rule silent.
    """
    if _task_field(task, "status") != "triage":
        return []

    status = triage_aux_status(cfg)
    if status is None:
        return []

    auto_decompose = bool(status.get("auto_decompose"))
    decomposer_explicit = bool(status.get("decomposer_explicit"))
    specifier_explicit = bool(status.get("specifier_explicit"))
    main_visible = bool(status.get("main_model_visible"))

    # Determine the primary slot and whether it is usable.
    if auto_decompose:
        primary_slot = "auxiliary.kanban_decomposer"
        primary_explicit = decomposer_explicit
        fallback_slot = "auxiliary.triage_specifier"
        fallback_explicit = specifier_explicit
        primary_desc = "decomposer"
        detail_path = (
            "Auto-decompose is on, so the dispatcher needs "
            "auxiliary.kanban_decomposer (with auxiliary.triage_specifier as "
            "a fallback for non-fan-out tasks)."
        )
    else:
        primary_slot = "auxiliary.triage_specifier"
        primary_explicit = specifier_explicit
        fallback_slot = "auxiliary.kanban_decomposer"
        fallback_explicit = decomposer_explicit
        primary_desc = "specifier"
        detail_path = (
            "Auto-decompose is off, so triage tasks need "
            "`hermes kanban specify`, which uses auxiliary.triage_specifier."
        )

    # The primary slot is usable when either: it was explicitly configured by
    # the user, OR the default `provider: auto` can fall back to the main
    # model. If both fail, we have a real configuration gap.
    if primary_explicit or main_visible:
        return []

    task_id = _task_field(task, "id") or "<task_id>"
    actions = [
        DiagnosticAction(
            kind="cli_hint",
            label=f"Configure {primary_slot}",
            payload={
                "command": (
                    f"hermes config set {primary_slot}.provider auto"
                )
            },
            suggested=True,
        ),
    ]
    if not fallback_explicit and not main_visible:
        actions.append(DiagnosticAction(
            kind="cli_hint",
            label=f"Or configure fallback {fallback_slot}",
            payload={
                "command": (
                    f"hermes config set {fallback_slot}.provider auto"
                )
            },
        ))
    if not auto_decompose:
        actions.append(DiagnosticAction(
            kind="cli_hint",
            label=f"Specify manually: hermes kanban specify {task_id}",
            payload={"command": f"hermes kanban specify {task_id}"},
        ))

    return [Diagnostic(
        kind="triage_aux_unavailable",
        severity="warning",
        title=f"Triage {primary_desc} has no usable model",
        detail=(
            f"This task is still in triage and no working auxiliary model is "
            f"visible to the dispatcher. {detail_path} The default slot uses "
            f"`provider: auto` which falls back to the main model, but no main "
            f"model is configured either. Configure the slot directly or set a "
            f"main model so the auto fallback can take over."
        ),
        actions=actions,
        first_seen_at=now,
        last_seen_at=now,
        count=1,
        data={
            "task_id": task_id,
            "auto_decompose": auto_decompose,
            "primary_slot": primary_slot,
            "main_model_visible": main_visible,
        },
    )]


def _rule_prose_phantom_refs(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Advisory prose-scan: the completion summary mentions ``t_<hex>``
    ids that don't resolve. Non-blocking; surfaced as a warning only.

    Auto-clears when a fresh clean completion arrives AFTER the
    suspected event.
    """
    hits = _active_hallucination_events(events, "suspected_hallucinated_references")
    if not hits:
        return []
    phantom_refs: list[str] = []
    for ev in hits:
        for pid in _parse_payload(ev).get("phantom_refs", []) or []:
            if pid not in phantom_refs:
                phantom_refs.append(pid)
    running = _task_field(task, "status") == "running"
    return [Diagnostic(
        kind="prose_phantom_refs",
        severity="warning",
        title="Completion summary references unknown task ids",
        detail=(
            "The completion summary mentions task ids that don't resolve "
            "in this board's database. The completion itself succeeded, "
            "but downstream consumers parsing the summary may be pointed "
            "at cards that never existed."
        ),
        actions=_generic_recovery_actions(task, running=running),
        first_seen_at=_event_ts(hits[0]),
        last_seen_at=_event_ts(hits[-1]),
        count=len(hits),
        data={"phantom_refs": phantom_refs},
    )]


def _rule_repeated_failures(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Task's unified ``consecutive_failures`` counter is climbing —
    something about this task+profile combo is broken and each retry
    fails the same way. Triggers regardless of the specific failure
    mode (spawn error, timeout, crash) because operationally they
    all look the same: the kernel keeps retrying and the operator
    needs to intervene.

    Threshold: cfg["failure_threshold"]. Runtime callers should derive
    this from ``kanban.failure_limit`` unless the user explicitly set a
    diagnostics threshold, so the signal does not lag behind the
    dispatcher's circuit breaker.

    Accepts the legacy ``spawn_failure_threshold`` config key for
    back-compat.
    """
    threshold = _positive_int(cfg.get(
        "failure_threshold",
        cfg.get("spawn_failure_threshold", 3),
    ), 3)
    failure_limit = _positive_int(cfg.get("failure_limit"), threshold)
    # Read the new unified counter name, with a fallback to the legacy
    # column name so this rule keeps working against old DB rows the
    # caller somehow materialised without running the migration.
    failures = (
        _task_field(task, "consecutive_failures", None)
        if _task_field(task, "consecutive_failures", None) is not None
        else _task_field(task, "spawn_failures", 0)
    )
    if failures is None or failures < threshold:
        return []
    last_err = (
        _task_field(task, "last_failure_error", None)
        if _task_field(task, "last_failure_error", None) is not None
        else _task_field(task, "last_spawn_error", None)
    )
    assignee = _task_field(task, "assignee")

    # Classify the most recent failure by peeking at run outcomes so
    # the title + suggested action can be specific without a separate
    # per-outcome rule.
    ordered_runs = sorted(runs, key=lambda r: _task_field(r, "id", 0))
    most_recent_outcome = None
    for r in reversed(ordered_runs):
        oc = _task_field(r, "outcome")
        if oc in {"spawn_failed", "timed_out", "crashed"}:
            most_recent_outcome = oc
            break

    actions: list[DiagnosticAction] = []
    if most_recent_outcome == "spawn_failed" and assignee and assignee != "default":
        # Spawn is failing specifically — profile setup issue.
        actions.append(DiagnosticAction(
            kind="cli_hint",
            label=f"Verify profile: hermes -p {assignee} doctor",
            payload={"command": f"hermes -p {assignee} doctor"},
            suggested=True,
        ))
        actions.append(DiagnosticAction(
            kind="cli_hint",
            label=f"Fix profile auth: hermes -p {assignee} auth",
            payload={"command": f"hermes -p {assignee} auth"},
        ))
    elif most_recent_outcome in {"timed_out", "crashed"}:
        # Worker got off the ground but died. Logs are the right place
        # to diagnose; reclaim/reassign are the recovery levers.
        task_id = _task_field(task, "id")
        if task_id:
            actions.append(DiagnosticAction(
                kind="cli_hint",
                label=f"Check logs: hermes kanban log {task_id}",
                payload={"command": f"hermes kanban log {task_id}"},
                suggested=True,
            ))
    actions.extend(_generic_recovery_actions(
        task, running=_task_field(task, "status") == "running",
    ))

    severity = "critical" if failures >= threshold * 2 else "error"
    err_text = (last_err or "").strip() if last_err else ""
    err_snippet = err_text[:500] + ("…" if len(err_text) > 500 else "") if err_text else ""
    outcome_label = {
        "spawn_failed": "spawn",
        "timed_out": "timeout",
        "crashed": "crash",
    }.get(most_recent_outcome or "", "failure")
    if err_snippet:
        title = f"Agent {outcome_label} x{failures}: {err_snippet.splitlines()[0][:160]}"
        detail = (
            f"This task has failed {failures} times in a row "
            f"(most recent: {outcome_label}). Full last error:\n\n"
            f"{err_snippet}\n\n"
            f"The dispatcher circuit breaker is configured for "
            f"{failure_limit} consecutive non-success attempts. Fix the "
            f"root cause and reclaim or unblock the task to retry."
        )
    else:
        title = f"Agent {outcome_label} x{failures} (no error recorded)"
        detail = (
            f"This task has failed {failures} times in a row "
            f"(most recent: {outcome_label}) but no error text was "
            f"captured. Check the suggested command or the worker log."
        )
    return [Diagnostic(
        kind="repeated_failures",
        severity=severity,
        title=title,
        detail=detail,
        actions=actions,
        first_seen_at=now,
        last_seen_at=now,
        count=failures,
        data={
            "consecutive_failures": failures,
            "most_recent_outcome": most_recent_outcome,
            "last_error": last_err,
            "failure_threshold": threshold,
            "failure_limit": failure_limit,
        },
    )]


def _rule_repeated_crashes(task, events, runs, now, cfg) -> list[Diagnostic]:
    """The worker spawns fine but keeps crashing mid-run. Check the last
    N runs' outcomes; N consecutive ``crashed`` without a successful
    ``completed`` means something about the task + profile combo is
    broken (OOM, missing dependency, tool it needs is down).

    Threshold: cfg["crash_threshold"] (default 2).

    Narrower than ``repeated_failures`` — fires earlier (2 crashes vs 3
    total failures) so the operator gets a crash-specific heads-up
    before the unified rule kicks in. Suppresses itself when the
    unified rule is also about to fire, to avoid double-flagging.
    """
    failure_threshold = int(cfg.get(
        "failure_threshold",
        cfg.get("spawn_failure_threshold", 3),
    ))
    unified_counter = (
        _task_field(task, "consecutive_failures", 0) or 0
    )
    # Unified rule will catch this — let it handle to avoid double fire.
    if unified_counter >= failure_threshold:
        return []

    threshold = int(cfg.get("crash_threshold", 2))
    ordered = sorted(runs, key=lambda r: _task_field(r, "id", 0))
    # Count trailing consecutive 'crashed' outcomes.
    consecutive = 0
    last_err = None
    for r in reversed(ordered):
        outcome = _task_field(r, "outcome")
        if outcome == "crashed":
            consecutive += 1
            if last_err is None:
                last_err = _task_field(r, "error")
        elif outcome in {"completed", "reclaimed", "integration_parked"}:
            # A success, a manual reclaim, or an integration park (the worker
            # ran to completion; only the merge was parked — see
            # INTEGRATION_PARKED_OUTCOME) breaks the crash streak. Keeping
            # 'integration_parked' here preserves the pre-relabel behavior, when
            # a park was stamped 'completed'.
            break
        else:
            # Other outcomes (timed_out, blocked, spawn_failed, gave_up)
            # aren't crash signals — don't count them, but they also
            # don't break the crash streak.
            continue
    if consecutive < threshold:
        return []
    task_id = _task_field(task, "id")
    actions: list[DiagnosticAction] = []
    if task_id:
        actions.append(DiagnosticAction(
            kind="cli_hint",
            label=f"Check logs: hermes kanban log {task_id}",
            payload={"command": f"hermes kanban log {task_id}"},
            suggested=True,
        ))
    running = _task_field(task, "status") == "running"
    actions.extend(_generic_recovery_actions(task, running=running))
    severity = "critical" if consecutive >= threshold * 2 else "error"
    # Put the actual error up-front so operators see WHAT broke without
    # having to open the logs. Truncate defensively — these can be huge
    # (full tracebacks).
    err_text = (last_err or "").strip() if last_err else ""
    err_snippet = err_text[:500] + ("…" if len(err_text) > 500 else "") if err_text else ""
    if err_snippet:
        title = f"Agent crashed {consecutive}x: {err_snippet.splitlines()[0][:160]}"
        detail = (
            f"The last {consecutive} runs ended with outcome=crashed. "
            f"Full last error:\n\n{err_snippet}"
        )
    else:
        title = f"Agent crashed {consecutive}x (no error recorded)"
        detail = (
            f"The last {consecutive} runs ended with outcome=crashed but "
            f"no error text was captured. Check the worker log for more."
        )
    return [Diagnostic(
        kind="repeated_crashes",
        severity=severity,
        title=title,
        detail=detail,
        actions=actions,
        first_seen_at=now,
        last_seen_at=now,
        count=consecutive,
        data={"consecutive_crashes": consecutive, "last_error": last_err},
    )]


def _rule_stuck_in_blocked(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Task has been in ``blocked`` status for too long without a comment.

    Threshold: cfg["blocked_stale_hours"] (default 24).
    Surfaced as a warning so humans know there's a pending unblock.
    """
    hours = float(cfg.get("blocked_stale_hours", 24))
    status = _task_field(task, "status")
    if status != "blocked":
        return []
    # Find the most recent ``blocked`` event.
    last_blocked_ts = 0
    for ev in events:
        if _event_kind(ev) == "blocked":
            t = _event_ts(ev)
            last_blocked_ts = max(last_blocked_ts, t)
    if last_blocked_ts == 0:
        return []
    age_hours = (now - last_blocked_ts) / 3600.0
    if age_hours < hours:
        return []
    # Any comment / unblock after the block breaks the "stale" signal.
    for ev in events:
        if _event_kind(ev) in {"commented", "unblocked"} and _event_ts(ev) > last_blocked_ts:
            return []
    actions: list[DiagnosticAction] = [
        DiagnosticAction(
            kind="comment",
            label="Add a comment / unblock the task",
            suggested=True,
        ),
    ]
    return [Diagnostic(
        kind="stuck_in_blocked",
        severity="warning",
        title=f"Task has been blocked for {int(age_hours)}h",
        detail=(
            f"This task transitioned to blocked {int(age_hours)}h ago and "
            f"has had no comments or unblock attempts since. Blocked tasks "
            f"are waiting for human input — check the block reason and "
            f"either unblock with feedback or answer with a comment."
        ),
        actions=actions,
        first_seen_at=last_blocked_ts,
        last_seen_at=last_blocked_ts,
        count=1,
        data={"blocked_at": last_blocked_ts, "age_hours": round(age_hours, 1)},
    )]


def _rule_block_unblock_cycling(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Task has cycled through blocked → unblocked many times — the
    ``unblock`` is not fixing the underlying problem and the worker
    keeps re-blocking for substantially the same reason.

    ``_rule_stuck_in_blocked`` resets its timer on any ``commented`` /
    ``unblocked`` event, so a task that cycles every few minutes is
    invisible to it regardless of how many times it cycles (#29747
    gap 1). This rule complements that one by counting block→unblock
    cycles in a sliding window.

    Threshold: cfg["block_cycle_threshold"] (default 3) cycles within
    cfg["block_cycle_window_seconds"] (default 24h).
    """
    threshold = _positive_int(cfg.get("block_cycle_threshold"), 3)
    window_seconds = float(cfg.get("block_cycle_window_seconds", 24 * 3600))
    cycle_cutoff = now - window_seconds

    # Walk events chronologically (arrival order — callers pre-sort by
    # id, which is the canonical chronological order; ``created_at``
    # alone is insufficient because multiple events can share the same
    # second).  Count "blocked after unblocked" transitions: every time
    # a blocked event follows at least one unblocked event since the
    # last cycle was counted, that's a new cycle.
    cycles = 0
    seen_unblock_since_last_cycle = False
    initial_blocked_ts = 0
    last_cycle_blocked_ts = 0
    for ev in events:
        ts = _event_ts(ev)
        if ts < cycle_cutoff:
            continue
        kind = _event_kind(ev)
        if kind == "blocked":
            if initial_blocked_ts == 0:
                initial_blocked_ts = ts
            if seen_unblock_since_last_cycle:
                cycles += 1
                last_cycle_blocked_ts = ts
                seen_unblock_since_last_cycle = False
        elif kind == "unblocked":
            seen_unblock_since_last_cycle = True

    if cycles < threshold:
        return []

    task_id = _task_field(task, "id")
    actions: list[DiagnosticAction] = []
    if task_id:
        actions.append(DiagnosticAction(
            kind="cli_hint",
            label=f"Check block reasons: hermes kanban events {task_id}",
            payload={"command": f"hermes kanban events {task_id}"},
            suggested=True,
        ))
    return [Diagnostic(
        kind="block_unblock_cycling",
        severity="warning",
        title=f"Task block→unblock cycled {cycles}x in {int(window_seconds/3600)}h",
        detail=(
            f"This task has been blocked {cycles} times after being "
            "unblocked, suggesting the unblock is not addressing the "
            "root cause and the worker keeps hitting the same wall. "
            "Review the block reasons in the event history; a different "
            "intervention (reassign, change scope, archive) may be needed."
        ),
        actions=actions,
        first_seen_at=int(initial_blocked_ts) if initial_blocked_ts else int(now),
        last_seen_at=int(last_cycle_blocked_ts) if last_cycle_blocked_ts else int(now),
        count=cycles,
        data={
            "cycles": cycles,
            "window_seconds": int(window_seconds),
        },
    )]


def _rule_stranded_in_ready(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Task has been in ``ready`` status for too long without any worker
    claiming it.

    Threshold: cfg["stranded_threshold_seconds"] (default 1800 = 30 min).

    Catches every "task waiting for a worker that never comes" case
    without caring WHY:

    * Operator typo'd the assignee — no profile or external worker matches.
    * Profile was deleted, leaving its tasks stranded.
    * External worker pool (Codex CLI, Claude Code lane, custom daemon)
      is down, hung, or wasn't started.
    * Dispatcher is misconfigured (wrong board, wrong HERMES_HOME).

    Pre-rule, all of these silently rotted in ``skipped_nonspawnable`` —
    the dispatcher correctly skipped them (good — no respawn loop) but
    nobody surfaced the fact that operator-actionable work was
    accumulating. The rule fires when a ready task's promoted-to-ready
    timestamp is older than the threshold AND the assignee is non-empty
    (truly unassigned tasks have their own ``skipped_unassigned`` signal
    on the dispatcher and a different operator response).

    The signal is age-based on purpose: it's identity-agnostic, so it
    works for Hermes profiles, registered lanes, external workers, and
    typos uniformly. No registry to curate, no per-board allowlist.
    """
    threshold_seconds = float(
        cfg.get("stranded_threshold_seconds", 30 * 60)
    )
    status = _task_field(task, "status")
    if status != "ready":
        return []
    # Skip tasks with a live claim — they're being worked on, even if
    # the worker hasn't reported progress yet (run-level liveness
    # extends the claim TTL; we don't want to second-guess that here).
    if _task_field(task, "claim_lock"):
        return []
    assignee = _task_field(task, "assignee") or ""
    if not assignee.strip():
        # Unassigned tasks: the dispatcher's ``skipped_unassigned`` is
        # already the right signal. A separate diagnostic here would
        # double-flag the same condition.
        return []

    # Find the most recent event that put this task into ready.
    # ``created`` covers tasks born ready; ``promoted`` covers parent-
    # done auto-promotion; ``reclaimed`` covers TTL/crash recovery;
    # ``unblocked`` covers human-driven resumes.
    READY_TRANSITION_KINDS = {
        "created", "promoted", "reclaimed", "unblocked",
    }
    last_ready_ts = 0
    for ev in events:
        if _event_kind(ev) in READY_TRANSITION_KINDS:
            t = _event_ts(ev)
            last_ready_ts = max(last_ready_ts, t)

    # Fallback: if no qualifying event exists (very old task or events
    # truncated), fall back to ``created_at`` on the task row. Better
    # to occasionally over-flag an ancient task than miss a stranded one.
    if last_ready_ts == 0:
        last_ready_ts = int(_task_field(task, "created_at", default=0) or 0)
    if last_ready_ts == 0:
        return []

    age_seconds = now - last_ready_ts
    if age_seconds < threshold_seconds:
        return []

    # Format the age in the largest sensible unit.
    if age_seconds >= 3600:
        age_str = f"{age_seconds / 3600:.1f}h"
    else:
        age_str = f"{int(age_seconds / 60)}m"

    # Severity escalates with age. Below 2x threshold = warning;
    # 2x – 6x = error; beyond 6x = critical (something is clearly
    # broken, not just slow).
    if age_seconds >= threshold_seconds * 6:
        severity = "critical"
    elif age_seconds >= threshold_seconds * 2:
        severity = "error"
    else:
        severity = "warning"

    actions = [
        DiagnosticAction(
            kind="reassign",
            label="Reassign to a different worker",
            payload={"current_assignee": assignee},
        ),
        DiagnosticAction(
            kind="cli_hint",
            label="Check dispatcher status",
            payload={"command": "hermes kanban diagnostics"},
        ),
    ]

    return [Diagnostic(
        kind="stranded_in_ready",
        severity=severity,
        title=f"Ready for {age_str} with no worker",
        detail=(
            f"This task has been ready for {age_str} but nothing has "
            f"claimed it. Common causes: assignee {assignee!r} is "
            f"misspelled, the profile was deleted, or the external "
            f"worker pool for this lane is down. Confirm the assignee "
            f"is correct and that a worker is actually polling for it."
        ),
        actions=actions,
        first_seen_at=last_ready_ts,
        last_seen_at=last_ready_ts,
        count=1,
        data={
            "ready_since": last_ready_ts,
            "age_seconds": int(age_seconds),
            "assignee": assignee,
            "threshold_seconds": int(threshold_seconds),
        },
    )]


# Registry — order matters: rules higher on the list render first when
# severity ties. Add new rules here.
def _rule_orphaned_worktree(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Worker isolation: a terminal task whose dispatcher-provisioned
    worktree (``<repo>/.worktrees/kanban/<root>``) still exists on disk
    well after completion — the chain was never drained by the integrator
    (e.g. last sibling archived instead of completed, or a parked chain
    whose task was archived). Disk-state check is read-only and cheap
    (one ``Path.exists``)."""
    status = _task_field(task, "status")
    if status not in ("done", "archived", "cancelled", "failed"):
        return []
    ws = _task_field(task, "workspace_path") or ""
    if not ws:
        return []
    try:
        from pathlib import Path as _Path

        parts = _Path(ws).parts
        wt = None
        for i in range(len(parts) - 2):
            if parts[i] == ".worktrees" and parts[i + 1] == "kanban":
                wt = _Path(*parts[: i + 3])
                break
        if wt is None or not wt.exists():
            return []
    except OSError:
        return []
    threshold = _positive_int(
        cfg.get("orphaned_worktree_age_seconds"), 48 * 3600,
    )
    completed = _task_field(task, "completed_at") or 0
    anchor = completed or (_task_field(task, "created_at") or now)
    if now - anchor < threshold:
        return []
    task_id = _task_field(task, "id") or "<task_id>"
    return [Diagnostic(
        kind="orphaned_worktree",
        severity="warning",
        title="Verwaister Kanban-Worktree",
        detail=(
            f"Task ist terminal ({status}), aber der provisionierte "
            f"Worktree existiert noch: {wt}. Die Kette wurde nie vom "
            "Integrator gemergt/aufgeräumt (z.B. letzter Task archiviert "
            "statt completed, oder geparkter Merge nie entschieden)."
        ),
        actions=[
            DiagnosticAction(
                kind="cli_hint",
                label="Worktree inspizieren (uncommittete Arbeit?)",
                payload={"command": f"git -C {wt} status --short && git -C {wt} log --oneline -5"},
                suggested=True,
            ),
            DiagnosticAction(
                kind="cli_hint",
                label="Nach Sichtung entfernen",
                payload={"command": (
                    f"git -C {wt.parent.parent.parent} worktree remove {wt}"
                )},
            ),
        ],
        first_seen_at=anchor,
        last_seen_at=now,
    )]


_RULES: list[RuleFn] = [
    _rule_hallucinated_cards,
    _rule_triage_aux_unavailable,
    _rule_reviewer_role_tool_mismatch,
    _rule_superseded_blocked_review_artifact,
    _rule_stale_review_block_needs_classification,
    _rule_prose_phantom_refs,
    _rule_repeated_failures,
    _rule_repeated_crashes,
    _rule_stuck_in_blocked,
    _rule_block_unblock_cycling,
    _rule_stranded_in_ready,
    _rule_orphaned_worktree,
]


# Known kinds (for the UI's filter / legend / i18n keys). Update when
# rules are added.
DIAGNOSTIC_KINDS = (
    "hallucinated_cards",
    "triage_aux_unavailable",
    "reviewer_role_tool_mismatch",
    "superseded_blocked_review_artifact",
    "stale_review_block_needs_classification",
    "prose_phantom_refs",
    "repeated_failures",
    "repeated_crashes",
    "stuck_in_blocked",
    "block_unblock_cycling",
    "stranded_in_ready",
    "descendants_blocked_by_stuck_parent",
    "stranded_decompose_root_branch",
    "orphaned_worktree",
)


DEFAULT_CONFIG = {
    # Match the dispatcher default (kanban.failure_limit) so repeated-failure
    # diagnostics do not lag behind the default auto-block threshold.
    "failure_threshold": 2,
    # Legacy alias accepted at read time by _rule_repeated_failures.
    "spawn_failure_threshold": 2,
    "crash_threshold": 2,
    "blocked_stale_hours": 24,
    "stale_review_block_hours": 2,
    "stale_review_block_error_hours": 24,
    # Stranded-task threshold. 30 min by default — below that, the
    # signal is dominated by tasks that are about to be claimed on the
    # next dispatcher tick (default 60s) and would just be noise.
    "stranded_threshold_seconds": 30 * 60,
    # How long a parent must be sticky-blocked before its waiting ``todo``
    # descendants are flagged as stranded (K4). Mirrors blocked_stale_hours.
    "descendants_blocked_parent_hours": 24,
}


def find_descendants_blocked_by_stuck_parent(
    conn,
    *,
    now: Optional[int] = None,
    config: Optional[dict] = None,
) -> dict[str, list[Diagnostic]]:
    """Flag ``todo`` tasks whose transitive parent is long-sticky-blocked (K4).

    This is a *cross-task* diagnostic and therefore lives OUTSIDE the per-task
    rule engine (``compute_task_diagnostics``), which is graph-blind — it only
    ever sees one task and that task's own events. Here we take the open board
    connection and walk ``task_links`` downward from each long-sticky-blocked
    task to the ``todo`` descendants it is stranding, returning a
    ``{todo_task_id: [Diagnostic]}`` map the caller can merge into its
    per-task diagnostics.

    "long-sticky-blocked" = status ``blocked`` AND a worker/operator
    ``kanban_block`` (``_has_sticky_block``, i.e. not a circuit-breaker
    ``gave_up``) AND the newest ``blocked`` event is at least
    ``config['descendants_blocked_parent_hours']`` old. Read-only; cycle-safe.
    """
    now = int(now if now is not None else time.time())
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    hours = float(
        cfg.get("descendants_blocked_parent_hours")
        or cfg.get("blocked_stale_hours", 24)
    )

    from hermes_cli import kanban_db as _kb  # lazy: avoid import cycle

    # 1) Collect long-sticky-blocked tasks (the stuck parents/blockers).
    stuck: dict[str, float] = {}
    for row in conn.execute("SELECT id FROM tasks WHERE status = 'blocked'"):
        bid = row["id"]
        last_blocked = conn.execute(
            "SELECT MAX(created_at) FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked'",
            (bid,),
        ).fetchone()[0]
        if not last_blocked:
            continue  # no real block event → circuit-breaker / direct DB set
        age_hours = (now - int(last_blocked)) / 3600.0
        if age_hours < hours:
            continue
        try:
            if not _kb._has_sticky_block(conn, bid):
                continue
        except Exception:
            continue
        stuck[bid] = age_hours
    if not stuck:
        return {}

    # 2) Status map once, so the descendant walk is pure in-memory after links.
    status_by = {
        r["id"]: r["status"]
        for r in conn.execute("SELECT id, status FROM tasks")
    }

    # 3) Walk descendants downward (cycle-safe) from each stuck blocker; record
    #    every ``todo`` task it strands and which blocker(s) reach it.
    stranded: dict[str, dict[str, Any]] = {}
    for bid, age_hours in stuck.items():
        seen: set[str] = set()
        stack = list(_kb.child_ids(conn, bid))
        while stack:
            cid = stack.pop()
            if cid in seen:
                continue
            seen.add(cid)
            if status_by.get(cid) == "todo":
                entry = stranded.setdefault(
                    cid, {"blockers": set(), "max_age": 0.0}
                )
                entry["blockers"].add(bid)
                entry["max_age"] = max(entry["max_age"], age_hours)
            stack.extend(_kb.child_ids(conn, cid))

    if not stranded:
        return {}

    out: dict[str, list[Diagnostic]] = {}
    for tid, info in stranded.items():
        blockers = sorted(info["blockers"])
        primary = blockers[0]
        age = int(info["max_age"])
        out[tid] = [Diagnostic(
            kind="descendants_blocked_by_stuck_parent",
            severity="warning",
            title=(
                f"Stranded by a sticky-blocked parent "
                f"({len(blockers)} blocker(s), {age}h)"
            ),
            detail=(
                "This todo task cannot start because a task it depends on has "
                f"been sticky-blocked for ~{age}h. Until the blocking parent is "
                "unblocked (or the dependency removed), this task waits "
                "indefinitely. Unblock the parent with feedback, or re-route "
                "this task if the dependency is stale."
            ),
            actions=[
                DiagnosticAction(
                    kind="cli_hint",
                    label="Unblock the stuck parent",
                    payload={"command": f"hermes kanban unblock {primary}"},
                    suggested=True,
                ),
                DiagnosticAction(
                    kind="cli_hint",
                    label="Inspect the blocking parent",
                    payload={"command": f"hermes kanban show {primary}"},
                ),
            ],
            first_seen_at=now,
            last_seen_at=now,
            count=1,
            data={
                "blocked_parents": blockers,
                "max_block_age_hours": round(info["max_age"], 1),
            },
        )]
    return out


def find_stranded_decompose_root_branches(
    conn,
    *,
    repo_root: Optional[str | Path] = None,
    main_ref: str = "main",
) -> dict[str, list[Diagnostic]]:
    """Flag blocked/gave_up decompose roots whose shared root branch is unmerged.

    Decompose children share the root worktree branch (``tasks.branch_name`` on
    the decompose root). If integration genuinely fails, the root task can be
    blocked while that branch still contains recoverable commits not reachable
    from ``main``. This read-only board-level pass surfaces the branch and head
    SHA before manual branch deletion or GC can make recovery harder.
    """
    from hermes_cli import kanban_worktrees as _kwt  # lazy: avoid import cycle

    def _resolve_repo(path_value: Optional[str]) -> Optional[Path]:
        if repo_root is not None:
            root = Path(repo_root)
            return root if root.exists() else None
        if path_value:
            root = _kwt.repo_root_for(path_value)
            if root is not None:
                return root
        return _kwt.repo_root_for(Path.cwd())

    def _branch_head(root: Path, branch_name: str) -> Optional[str]:
        try:
            return _kwt._git(
                root,
                "rev-parse",
                "--verify",
                f"{branch_name}^{{commit}}",
            ).strip()
        except Exception:
            return None

    def _is_ancestor(root: Path, ancestor_sha: str, descendant_ref: str) -> Optional[bool]:
        try:
            descendant_sha = _kwt._git(
                root,
                "rev-parse",
                "--verify",
                f"{descendant_ref}^{{commit}}",
            ).strip()
        except Exception:
            return None
        try:
            _kwt._git(root, "merge-base", "--is-ancestor", ancestor_sha, descendant_sha)
            return True
        except Exception:
            return False

    out: dict[str, list[Diagnostic]] = {}
    rows = conn.execute(
        "SELECT id, title, status, branch_name, workspace_path "
        "FROM tasks "
        "WHERE status IN ('blocked', 'gave_up') "
        "  AND branch_name IS NOT NULL AND branch_name != ''"
    ).fetchall()
    for row in rows:
        task_id = row["id"]
        try:
            if not _kwt._is_decompose_root(conn, task_id):
                continue
        except Exception:
            continue
        branch_name = str(row["branch_name"])
        root = _resolve_repo(row["workspace_path"])
        if root is None:
            continue
        head_sha = _branch_head(root, branch_name)
        if not head_sha:
            continue
        merged = _is_ancestor(root, head_sha, main_ref)
        if merged is not False:
            continue
        out[task_id] = [
            Diagnostic(
                kind="stranded_decompose_root_branch",
                severity="critical",
                title=(
                    f"Decompose root branch {branch_name} has unmerged work "
                    f"at {head_sha[:12]}"
                ),
                detail=(
                    f"Decompose root {task_id} is {row['status']} but shared "
                    f"branch {branch_name} points at {head_sha}, which is not "
                    f"reachable from {main_ref}. Recover the work before "
                    f"deleting the branch or running git gc; inspect the branch "
                    f"or run git cherry-pick {head_sha} onto main."
                ),
                actions=[
                    DiagnosticAction(
                        kind="cli_hint",
                        label=(
                            f"Inspect {branch_name} and recover with "
                            f"git cherry-pick {head_sha}"
                        ),
                        payload={
                            "command": f"git cherry-pick {head_sha}",
                            "branch": branch_name,
                            "head_sha": head_sha,
                        },
                        suggested=True,
                    )
                ],
                data={
                    "branch_name": branch_name,
                    "head_sha": head_sha,
                    "main_ref": main_ref,
                    "recovery_hint": f"git cherry-pick {head_sha}",
                },
            )
        ]
    return out


def config_from_kanban_config(kanban_cfg: Optional[dict]) -> dict:
    """Build diagnostics config from the runtime ``kanban`` config section.

    ``kanban.diagnostics.failure_threshold`` remains an explicit override.
    Otherwise, derive the repeated-failure threshold from
    ``kanban.failure_limit`` so CLI/dashboard diagnostics match the
    dispatcher's actual circuit-breaker threshold.
    """
    kanban_cfg = kanban_cfg or {}
    diag_cfg = dict(kanban_cfg.get("diagnostics") or {})
    diag_cfg.setdefault(
        "failure_limit",
        kanban_cfg.get("failure_limit", DEFAULT_CONFIG["failure_threshold"]),
    )
    if (
        "failure_threshold" not in diag_cfg
        and "spawn_failure_threshold" not in diag_cfg
    ):
        diag_cfg["failure_threshold"] = diag_cfg["failure_limit"]
    return diag_cfg


def config_from_runtime_config(raw_config: Optional[dict]) -> dict:
    """Build diagnostics config from the full Hermes runtime config.

    Carries through ``kanban``, ``auxiliary``, and ``model`` keys so triage-
    aware rules can inspect the active aux-helper and main-model state.
    Folds the ``kanban`` block through ``config_from_kanban_config`` so the
    repeated-failure threshold derivation still applies.
    """
    raw_config = raw_config or {}
    if not isinstance(raw_config, dict):
        return {}
    cfg: dict = {}
    kanban_cfg = raw_config.get("kanban")
    if isinstance(kanban_cfg, dict):
        cfg.update(config_from_kanban_config(kanban_cfg))
        cfg["kanban"] = kanban_cfg
    for key in ("auxiliary", "model"):
        value = raw_config.get(key)
        if value is not None:
            cfg[key] = value
    return cfg


def compute_task_diagnostics(
    task,
    events: list,
    runs: list,
    *,
    now: Optional[int] = None,
    config: Optional[dict] = None,
) -> list[Diagnostic]:
    """Run every rule against a single task's state and return a
    severity-sorted list of active diagnostics.

    Sorting: critical first, then error, then warning; ties broken by
    most-recent ``last_seen_at``.
    """
    now_ts = int(now if now is not None else time.time())
    config = config or {}
    cfg = {**DEFAULT_CONFIG, **config}
    if (
        "failure_threshold" not in config
        and "spawn_failure_threshold" not in config
        and "failure_limit" in config
    ):
        cfg["failure_threshold"] = _positive_int(
            config.get("failure_limit"),
            DEFAULT_CONFIG["failure_threshold"],
        )
    out: list[Diagnostic] = []
    for rule in _RULES:
        try:
            out.extend(rule(task, events, runs, now_ts, cfg))
        except Exception:
            # A broken rule must never crash the dashboard. Rule bugs
            # get caught in tests; in production we'd rather drop the
            # diagnostic than 500 a whole /board request.
            continue
    severity_idx = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    out.sort(
        key=lambda d: (
            -severity_idx.get(d.severity, -1),
            -(d.last_seen_at or 0),
        )
    )
    return out
