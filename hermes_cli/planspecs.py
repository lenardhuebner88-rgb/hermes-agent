"""PlanSpec discovery, binding validation, and deterministic Kanban ingest."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Literal

import yaml

from hermes_cli import kanban_db
from hermes_cli.plan_compiler import (
    AcceptanceCriterion,
    CompileBlocked,
    TaskgraphHints,
    _extract_frontmatter,
    _normalize_acceptance_criteria,
    taskgraph_hints_to_children,
)

logger = logging.getLogger(__name__)

DEFAULT_PLANS_ROOT = Path("/home/piet/vault/03-Agents")
LIVE_TEST_DEPTHS = {"smoke", "contract", "ui-real"}
PlanSpecScope = Literal["open", "all"]

# --- Deterministic spec-rubric (validate_spec_rubric) ----------------------
# The on-disk Hermes worker lanes a binding PlanSpec subtask may target. A lane
# is mapped 1:1 to the Kanban child assignee, so an unknown lane mints a task no
# roster profile can ever claim. Keep in sync with the lane roster on disk.
VALID_PLANSPEC_LANES = {
    "coder",
    "coder-claude",
    "premium",
    "reviewer",
    "critic",
    "verifier",
    "research",
    "admin",
    "family-ui",
    "fo-brain",
}

# Claude-Code instruments (subagents / panels / slash-commands) that are NOT
# Hermes worker lanes. Used as a `lane:` they create an unfulfillable contract
# for a headless Kanban worker; baked into a worker AC they spawn an AC the
# worker can never satisfy (lesson t_2477e10f → an endless REQUEST_CHANGES loop).
# NB: ``council`` keeps a legitimate home in the operator ``freigabe`` gate — the
# rubric only ever scans subtask lanes + ACs, never ``freigabe``, so that stays
# valid. Exact-match against the single-token ``lane:`` field, so the full set is
# safe here.
_CC_INSTRUMENT_LANES = {
    "council",
    "minimax-auditor",
    "ui-verifier",
    "dep-scout",
    "log-analyst",
    "auditor",
    "integrator",
    "builder",
    "mechanic",
    "scribe",
    "general-purpose",
    "explore",
    "plan",
}

# High-signal CC-instrument tokens to scan *inside* free-text AC statements. Only
# hyphenated/compound tool names (plus ``council``) that never occur as innocent
# prose — bare English words like "auditor"/"builder"/"plan"/"explore" are
# deliberately excluded so a normal German/English AC sentence cannot false-trip.
_CC_INSTRUMENT_AC_TOKENS = {
    "council",
    "minimax-auditor",
    "ui-verifier",
    "dep-scout",
    "log-analyst",
}

# Template-residue patterns: a literal ``<…>`` angle placeholder, a TODO/FIXME/TBD
# marker, or a bare ``…`` / ``...`` ellipsis left over from a spec template.
_RESIDUE_ANGLE_RE = re.compile(r"<[^<>\n]{1,80}>")
_RESIDUE_MARKER_RE = re.compile(r"\b(?:TODO|FIXME|TBD)\b", re.IGNORECASE)
_RESIDUE_ELLIPSIS_RE = re.compile(r"\.\.\.|…")

_CLOSED_STATUS_PREFIXES = (
    "archived",
    "closed",
    "complete",
    "completed",
    "done",
    "implemented",
    "merged",
    "obsolete",
    "shipped",
    "superseded",
)


class PlanSpecBlocked(RuntimeError):
    def __init__(self, findings: list[str]):
        self.findings = findings
        super().__init__("; ".join(findings))


class PlanSpecNotFound(PlanSpecBlocked):
    """No ``.md`` file exists at the (valid, under-root) resolved path.

    A typed subclass so HTTP callers can map *missing file* → 404 and every
    other block → 400 via ``except``-ordering, instead of substring-matching
    the human-readable finding text (which silently breaks if the wording in
    :func:`resolve_planspec_path` ever changes). It still *is-a*
    ``PlanSpecBlocked``, so every existing ``except PlanSpecBlocked`` keeps
    catching it unchanged.
    """


@dataclass(frozen=True)
class BindingPlanSpec:
    path: Path
    frontmatter: dict[str, Any]
    topic: str
    status: str
    freigabe: str
    live_test_depth: str
    hints: TaskgraphHints
    children: list[dict[str, Any]]


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _status_slug(status: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", status.lower()).strip("-")


def _closed_reason(status: str) -> str | None:
    slug = _status_slug(status)
    if any(slug == prefix or slug.startswith(f"{prefix}-") for prefix in _CLOSED_STATUS_PREFIXES):
        return f"closed status: {status}"
    return None


def _is_display_only_open_plan(frontmatter: dict[str, Any], status: str) -> bool:
    gate = str(frontmatter.get("gate") or "").strip().lower()
    if gate != "plangate":
        return False
    slug = _status_slug(status)
    return slug.startswith("signiert") or slug.startswith("signed")


def resolve_planspec_path(path: str | Path, *, plans_root: Path = DEFAULT_PLANS_ROOT) -> Path:
    try:
        candidate = Path(path).expanduser().resolve(strict=False)
    except ValueError as exc:
        # An embedded NUL byte (or other OS-rejected path) makes realpath()
        # raise ValueError. Surface it as a 400-class block instead of letting
        # it propagate as an unhandled 500. Stay path-free (see #13 below): the
        # message must not echo the attacker-influenced ``path``.
        raise PlanSpecBlocked(["planspec path is malformed"]) from exc
    root = plans_root.expanduser().resolve(strict=False)
    # #13: error findings are surfaced verbatim to the dashboard / HTTP callers.
    # Never embed the resolved server-side path (``root`` / ``candidate``) — that
    # discloses the server's filesystem layout to an attacker-influenced caller.
    if not _is_relative_to(candidate, root):
        raise PlanSpecBlocked(["planspec path must be under the allowed plans directory"])
    if candidate.suffix.lower() != ".md":
        raise PlanSpecBlocked(["planspec path must point to a markdown file"])
    if not candidate.is_file():
        raise PlanSpecNotFound(["planspec file not found"])
    return candidate


def parse_binding_planspec(path: str | Path, *, plans_root: Path = DEFAULT_PLANS_ROOT) -> BindingPlanSpec:
    resolved = resolve_planspec_path(path, plans_root=plans_root)
    try:
        frontmatter, body = _extract_frontmatter(resolved.read_text(encoding="utf-8"))
    except CompileBlocked as exc:
        raise PlanSpecBlocked(exc.findings) from exc
    except UnicodeDecodeError as exc:
        raise PlanSpecBlocked([f"planspec is not valid utf-8: {exc}"]) from exc

    findings: list[str] = []
    raw_hints = frontmatter.get("taskgraph_hints")
    if not isinstance(raw_hints, dict):
        findings.append("taskgraph_hints must be a YAML mapping")
        raw_hints = {}
    try:
        hints = TaskgraphHints.model_validate(raw_hints)
    except Exception as exc:
        findings.append(str(exc))
        hints = TaskgraphHints()
    if not hints.binding:
        findings.append("taskgraph_hints.binding must be true")

    live_test_depth = str(frontmatter.get("live_test_depth") or "smoke").strip()
    if live_test_depth not in LIVE_TEST_DEPTHS:
        findings.append(
            "live_test_depth must be one of: " + ", ".join(sorted(LIVE_TEST_DEPTHS))
        )
    freigabe = str(frontmatter.get("freigabe") or "").strip()
    if not freigabe:
        findings.append("freigabe is required")
    status = str(frontmatter.get("status") or "").strip()
    closed = _closed_reason(status)
    if closed:
        findings.append(closed)

    if findings:
        raise PlanSpecBlocked(findings)

    # Parse plan-level acceptance_criteria from frontmatter for AC threading.
    raw_ac = frontmatter.get("acceptance_criteria")
    plan_ac: list[str | AcceptanceCriterion] = []
    if isinstance(raw_ac, list):
        ac_findings: list[str] = []
        plan_ac = _normalize_acceptance_criteria(raw_ac, ac_findings)
        # A malformed plan-level AC criterion must not block ingest (we drop it
        # and continue) — but it must NOT vanish silently, so log it.
        if ac_findings:
            logger.warning(
                "PlanSpec %s: %d plan-level acceptance_criteria dropped: %s",
                resolved, len(ac_findings), "; ".join(ac_findings),
            )

    try:
        children = taskgraph_hints_to_children(
            hints,
            plan_ac=plan_ac,
            planspec_source=str(resolved),
        )
    except CompileBlocked as exc:
        raise PlanSpecBlocked(exc.findings) from exc

    topic = (
        str(frontmatter.get("topic") or "").strip()
        or str(frontmatter.get("title") or "").strip()
        or _first_heading(body)
        or resolved.stem
    )
    return BindingPlanSpec(
        path=resolved,
        frontmatter=frontmatter,
        topic=topic,
        status=status,
        freigabe=freigabe,
        live_test_depth=live_test_depth,
        hints=hints,
        children=children,
    )


def _planspec_kanban_state(path: Path, *, board: str | None = None) -> dict[str, Any] | None:
    """Return the latest Kanban execution state for a PlanSpec source path.

    Variant A is deliberately read-only: the Vault frontmatter is not touched.
    Ingest already records a durable root event with
    ``payload.source == 'planspec_ingest'`` and ``payload.path == <path>``;
    the dashboard can derive whether that root is queued, running, blocked, or
    completed from the live Kanban tree.
    """
    resolved = str(path.resolve(strict=False))
    conn = kanban_db.connect(board=board)
    try:
        rows = conn.execute(
            "SELECT task_id, payload, created_at FROM task_events "
            "WHERE kind = 'specified' AND payload LIKE ? "
            "ORDER BY created_at DESC, id DESC LIMIT 50",
            ("%planspec_ingest%",),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get("source") != "planspec_ingest":
                continue
            if str(payload.get("path") or "") != resolved:
                continue
            root_id = str(row["task_id"])
            root = kanban_db.get_task(conn, root_id)
            if root is None:
                continue
            child_ids = kanban_db.parent_ids(conn, root_id)
            child_statuses: list[str] = []
            for child_id in child_ids:
                child = kanban_db.get_task(conn, child_id)
                if child is not None:
                    child_statuses.append(child.status)
            statuses = [root.status, *child_statuses]
            if root.status == "done":
                state = "completed"
            elif "blocked" in statuses:
                state = "blocked"
            elif any(status in {"running", "review"} for status in statuses):
                state = "running"
            elif any(status in {"triage", "todo", "scheduled", "ready"} for status in statuses):
                state = "queued"
            else:
                state = root.status or "unknown"
            total = len(child_statuses)
            done = sum(1 for status in child_statuses if status == "done")
            blocked = sum(1 for status in child_statuses if status == "blocked")
            running = sum(1 for status in child_statuses if status in {"running", "review"})
            return {
                "root_task_id": root_id,
                "root_status": root.status,
                "state": state,
                "child_total": total,
                "child_done": done,
                "child_blocked": blocked,
                "child_running": running,
                "ingested_at": int(row["created_at"]),
            }
        return None
    finally:
        conn.close()


def list_planspecs(
    *,
    plans_root: Path = DEFAULT_PLANS_ROOT,
    scope: PlanSpecScope = "open",
    valid: bool | None = None,
    limit: int | None = None,
    search: str | None = None,
    include_kanban_status: bool = False,
    board: str | None = None,
) -> list[dict[str, Any]]:
    if scope not in ("open", "all"):
        raise ValueError("scope must be 'open' or 'all'")
    valid_filter = valid
    root = plans_root.expanduser().resolve(strict=False)
    paths = sorted(root.glob("*/plans/*.md"), key=lambda p: str(p).lower())
    records: list[dict[str, Any]] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
            frontmatter, body = _extract_frontmatter(text)
            raw_hints = frontmatter.get("taskgraph_hints")
            errors: list[str] = []
            hints: TaskgraphHints | None = None
            if isinstance(raw_hints, dict):
                try:
                    hints = TaskgraphHints.model_validate(raw_hints)
                except Exception as exc:
                    errors.append(str(exc))
            elif raw_hints is not None:
                errors.append("taskgraph_hints must be a YAML mapping")
            live_test_depth = str(frontmatter.get("live_test_depth") or "").strip()
            if live_test_depth and live_test_depth not in LIVE_TEST_DEPTHS:
                errors.append("invalid live_test_depth")
            topic = (
                str(frontmatter.get("topic") or "").strip()
                or str(frontmatter.get("title") or "").strip()
                or _first_heading(body)
                or path.stem
            )
            status = str(frontmatter.get("status") or "").strip()
            binding = bool(hints and hints.binding)
            valid = not errors and binding
            closed = _closed_reason(status)
            display_only_open = not errors and not binding and _is_display_only_open_plan(frontmatter, status)
            kanban_state = None
            kanban_state_error = None
            if include_kanban_status:
                try:
                    kanban_state = _planspec_kanban_state(path, board=board)
                except Exception as exc:  # pragma: no cover - defensive UI fallback
                    kanban_state_error = str(exc)
            kanban_terminal_state = (kanban_state or {}).get("state") or "not_ingested"
            open_record = closed is None and (valid or display_only_open) and kanban_terminal_state != "completed"
            if closed:
                closed_reason = closed
            elif kanban_terminal_state == "completed":
                closed_reason = "kanban state: completed"
            elif open_record:
                closed_reason = None
            else:
                closed_reason = "not a binding PlanSpec"
            record_errors = list(errors)
            if display_only_open:
                record_errors.append("display-only: taskgraph_hints.binding is missing; Kanban ingest disabled")
            if kanban_state_error:
                record_errors.append(f"kanban status unavailable: {kanban_state_error}")
            records.append(
                {
                    "path": str(path.resolve(strict=False)),
                    "agent": path.parent.parent.name,
                    "filename": path.name,
                    "topic": topic,
                    "status": status,
                    "freigabe": str(frontmatter.get("freigabe") or "").strip(),
                    "live_test_depth": live_test_depth or None,
                    "binding": binding,
                    "subtask_count": len(hints.subtasks) if hints else 0,
                    "valid": valid,
                    "open": open_record,
                    "closed_reason": closed_reason,
                    "kanban_root_task_id": (kanban_state or {}).get("root_task_id"),
                    "kanban_root_status": (kanban_state or {}).get("root_status"),
                    "kanban_state": kanban_terminal_state,
                    "kanban_child_total": (kanban_state or {}).get("child_total", 0),
                    "kanban_child_done": (kanban_state or {}).get("child_done", 0),
                    "kanban_child_blocked": (kanban_state or {}).get("child_blocked", 0),
                    "kanban_child_running": (kanban_state or {}).get("child_running", 0),
                    "kanban_ingested_at": (kanban_state or {}).get("ingested_at"),
                    "errors": record_errors,
                }
            )
        except Exception as exc:
            records.append(
                {
                    "path": str(path.resolve(strict=False)),
                    "agent": path.parent.parent.name,
                    "filename": path.name,
                    "topic": path.stem,
                    "status": "",
                    "freigabe": "",
                    "live_test_depth": None,
                    "binding": False,
                    "subtask_count": 0,
                    "valid": False,
                    "open": False,
                    "closed_reason": "invalid PlanSpec",
                    "errors": [str(exc)],
                }
            )
    if scope == "open":
        records = [item for item in records if item["open"]]
    if valid_filter is not None:
        records = [item for item in records if item["valid"] is valid_filter]
    query = (search or "").strip().lower()
    if query:
        records = [item for item in records if _planspec_record_matches_query(item, query)]
    records.sort(key=lambda item: (item["open"] is False, item["valid"] is False, item["path"]), reverse=False)
    if limit and limit > 0:
        records = records[:limit]
    return records


def _planspec_record_matches_query(item: dict[str, Any], query: str) -> bool:
    haystack = "\n".join(
        str(item.get(field) or "")
        for field in ("topic", "filename", "agent", "path", "status", "freigabe")
    ).lower()
    return query in haystack


def mark_planspec_not_needed(
    path: str | Path,
    *,
    plans_root: Path = DEFAULT_PLANS_ROOT,
    author: str = "dashboard",
) -> dict[str, Any]:
    resolved = resolve_planspec_path(path, plans_root=plans_root)
    try:
        frontmatter, body = _extract_frontmatter(resolved.read_text(encoding="utf-8"))
    except CompileBlocked as exc:
        raise PlanSpecBlocked(exc.findings) from exc
    except UnicodeDecodeError as exc:
        raise PlanSpecBlocked([f"planspec is not valid utf-8: {exc}"]) from exc

    current_status = str(frontmatter.get("status") or "").strip()
    if _closed_reason(current_status):
        return {
            "ok": True,
            "path": str(resolved),
            "status": current_status,
            "closed_reason": _closed_reason(current_status),
        }

    frontmatter["status"] = "obsolete"
    frontmatter["closed_at"] = datetime.now(timezone.utc).date().isoformat()
    frontmatter["closed_by"] = author.strip() or "dashboard"
    frontmatter["closed_reason"] = "not needed anymore"

    raw_frontmatter = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    next_text = f"---\n{raw_frontmatter}\n---\n\n{body.lstrip()}"
    tmp_path = resolved.with_name(f".{resolved.name}.tmp")
    tmp_path.write_text(next_text, encoding="utf-8")
    tmp_path.replace(resolved)
    return {
        "ok": True,
        "path": str(resolved),
        "status": "obsolete",
        "closed_reason": "not needed anymore",
    }


def build_root_body(spec: BindingPlanSpec) -> str:
    fm = yaml.safe_dump(spec.frontmatter, sort_keys=False, allow_unicode=True).strip()
    lines = [
        f"PlanSpec source: {spec.path}",
        f"Freigabe: {spec.freigabe}",
        f"Live-Test-Depth: {spec.live_test_depth}",
        "",
        "Frontmatter:",
        "```yaml",
        fm,
        "```",
    ]
    # Strategist annotation contract (I1 ↔ G1): when a PlanSpec carries a
    # ``strategist_meta`` frontmatter block, render it as the machine-readable
    # annotation the held-proposal surface parses (target/ROI/counter-metric).
    # Guarded — a spec without the block is byte-for-byte unaffected.
    meta = spec.frontmatter.get("strategist_meta")
    if isinstance(meta, dict):
        from hermes_cli.strategist_surface import format_annotation

        def _meta_str(value: Any) -> str:
            return str(value).strip() if value not in (None, "") else ""

        annotation = format_annotation(
            target_metric=_meta_str(meta.get("target_metric")),
            roi=_meta_str(meta.get("roi")),
            counter_metric=_meta_str(meta.get("counter_metric")),
        )
        lines.extend(["", annotation])
    return "\n".join(lines)


def ingest_idempotency_key(spec: BindingPlanSpec) -> str:
    """Deterministic idempotency key for a PlanSpec ingest.

    Combines the resolved source path with a SHA-256 of the file's raw
    bytes (the content-hash). Re-ingesting the identical file — same path,
    same content — yields the same key, so the second run links back to the
    existing chain instead of minting a duplicate. Editing the PlanSpec
    (new content) or moving it (new path) produces a new key, hence a fresh
    chain. The key is stamped onto the root task and queried on re-ingest.
    """
    content_hash = hashlib.sha256(spec.path.read_bytes()).hexdigest()
    return f"planspec-ingest:{spec.path}:{content_hash}"


def _residue_tokens(text: str) -> list[str]:
    """Return distinct template-residue tokens found in *text* (order-preserving).

    A ``<…>`` angle placeholder reports the whole bracketed token; an ellipsis
    that sits *inside* an angle placeholder is not reported twice.
    """
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    angle_spans: list[tuple[int, int]] = []

    def _add(token: str) -> None:
        if token and token not in seen:
            seen.add(token)
            found.append(token)

    for match in _RESIDUE_ANGLE_RE.finditer(text):
        angle_spans.append(match.span())
        _add(match.group(0))
    for match in _RESIDUE_MARKER_RE.finditer(text):
        _add(match.group(0))
    for match in _RESIDUE_ELLIPSIS_RE.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in angle_spans):
            continue
        _add(match.group(0))
    return found


def _ac_statements_for_child(child: dict[str, Any]) -> list[str]:
    """Return the resolved AC statements (own + inherited) threaded into *child*."""
    statements: list[str] = []
    for item in child.get("acceptance_criteria_struct") or []:
        if isinstance(item, dict):
            statement = str(item.get("statement") or "").strip()
            if statement:
                statements.append(statement)
    return statements


def _collect_spec_rubric_findings(spec: BindingPlanSpec) -> list[str]:
    """Run the deterministic rubric over an already-parsed *spec* and return all
    findings (empty list = the spec passes the rubric).

    Layered ON TOP of :func:`parse_binding_planspec` (which already validated the
    structural shape: binding, ids, deps, live_test_depth, freigabe, status). The
    rubric adds the *quality* checks that keep an under-specified PlanSpec from
    minting unworkable Kanban tasks. Each finding is its own actionable line.
    """
    findings: list[str] = []
    # ``spec.children`` (from taskgraph_hints_to_children) carry the resolved AC
    # — per-subtask plus the plan-wide / applies_to-inherited threading — keyed by
    # planspec_subtask_id. ``spec.hints.subtasks`` carry the verbatim title/body/
    # lane the author wrote. Use each for what it knows.
    children_by_id = {str(c.get("planspec_subtask_id") or ""): c for c in spec.children}
    for subtask in spec.hints.subtasks:
        sid = subtask.id
        child = children_by_id.get(sid, {})
        ac_statements = _ac_statements_for_child(child)

        # 1) Every subtask needs >= 1 AC (own or inherited). The child's resolved
        #    acceptance_criteria_struct is empty exactly when no AC applies.
        if not (child.get("acceptance_criteria_struct") or []):
            findings.append(f"AC-less subtask: {sid}")

        # 2) No template residue in title / body / AC.
        for token in _residue_tokens(subtask.title):
            findings.append(f"placeholder residue in {sid}: {token}")
        for token in _residue_tokens(subtask.body):
            findings.append(f"placeholder residue in {sid}: {token}")
        for statement in ac_statements:
            for token in _residue_tokens(statement):
                findings.append(f"placeholder residue in {sid}: {token}")

        # 3) + 4a) Lane must be a known Hermes worker lane, and must not be a
        #    Claude-Code instrument (which gets a more actionable message).
        lane_norm = subtask.lane.strip().lower()
        if lane_norm in _CC_INSTRUMENT_LANES:
            findings.append(f"CC-instrument as lane in {sid}: {subtask.lane}")
        elif lane_norm not in VALID_PLANSPEC_LANES:
            findings.append(f"unknown lane: {subtask.lane}")

        # 4b) No CC instrument baked into a worker AC.
        for statement in ac_statements:
            low = statement.lower()
            for token in _CC_INSTRUMENT_AC_TOKENS:
                if re.search(rf"\b{re.escape(token)}\b", low):
                    findings.append(f"CC-instrument in AC of {sid}: {token}")
    return findings


def validate_spec_rubric(spec: BindingPlanSpec) -> None:
    """Deterministic rubric gate over a parsed binding PlanSpec.

    Raises :class:`PlanSpecBlocked` with one actionable finding per violation:

    1. AC-less subtask (no own or inherited acceptance criterion).
    2. Template residue (``<…>`` placeholder, TODO/FIXME/TBD, bare ``…``/``...``)
       in a subtask title / body / AC.
    3. Unknown ``lane`` (not a known Hermes worker lane).
    4. A Claude-Code instrument used as a ``lane`` or baked into a worker AC.

    Returns ``None`` when the spec passes. Layered on top of the structural
    validation in :func:`parse_binding_planspec`; meant to run synchronously in
    :func:`ingest_planspec` before any DB write.
    """
    findings = _collect_spec_rubric_findings(spec)
    if findings:
        raise PlanSpecBlocked(findings)


# --- Subjective quality judge (Sonnet) -------------------------------------
# After the deterministic rubric passes, a SYNCHRONOUS LLM judge scores the
# three qualities a regex can't see: are the AC testable/observable (not
# vague), is "done" sharp, and is the goal coherent with — and covered by —
# its subtasks. The judge is a GATE (a fail verdict raises PlanSpecBlocked so
# the generating session learns to fix the ingest) AND a TEACHER (the verdict
# names exactly what to fix).
#
# PFLICHT-WIEDERVERWENDUNG: it reuses the in-repo auxiliary-client call path
# (``agent.auxiliary_client.get_text_auxiliary_client``) that
# ``hermes_cli/kanban_specify.py`` already drives — NO new HTTP client, NO new
# SDK dependency. The aux client owns provider/auth/transport; we only request
# the model and parse the reply.
#
# GRACEFUL FALLBACK (mandatory): on any LLM infra / network / auth / timeout
# error — import failure, no client configured, the call raising, or an
# unparseable verdict — the judge logs a WARNING and falls back to
# deterministic-only ingest. It NEVER hard-fails ingest on infra trouble; only
# an actual ``fail`` verdict blocks.
#
# Operator note: the judge model defaults to claude-sonnet-4-6. Configure the
# provider that serves it via ``auxiliary.spec_judge.*`` in config.yaml — it
# must be a Sonnet-capable lane (e.g. Anthropic direct), NOT OpenRouter for an
# anthropic/* model (provider rule). Override the model via
# ``HERMES_PLANSPEC_JUDGE_MODEL`` or ``auxiliary.spec_judge.model``; disable the
# judge entirely with ``HERMES_PLANSPEC_JUDGE=0`` (deterministic-only).
SPEC_JUDGE_MODEL = "claude-sonnet-4-6"
SPEC_JUDGE_TASK = "spec_judge"
SPEC_JUDGE_MAX_TOKENS = 1500
SPEC_JUDGE_TIMEOUT = 90

_SPEC_JUDGE_SYSTEM_PROMPT = """You are the PlanSpec quality judge for the Hermes \
Agent kanban board. A binding PlanSpec has already passed deterministic checks \
(every subtask has acceptance criteria, no template residue, valid lanes). Your \
job is the SUBJECTIVE quality the deterministic gate cannot see.

Judge the spec on three axes:
  (a) Testable/observable AC — is every acceptance criterion concrete and \
verifiable (a command, an observable state, a measurable condition), or is it \
vague ("works well", "is robust", "looks good")?
  (b) Sharp "done" — can a worker tell unambiguously when the work is finished, \
or is the completion condition fuzzy / open-ended?
  (c) Coherent goal <-> subtasks — is the overall goal clear, and do the \
subtasks together actually cover it (no obvious gap, no subtask that drifts \
off-goal)?

Respond with ONLY a single JSON object, no prose, no code fences:

  {"verdict": "pass" | "fail", "reasons": ["<concrete, actionable reason>", ...]}

Rules:
  - "pass" only when all three axes are satisfied. Be pragmatic, not pedantic — \
a normal, workable spec passes. Block only genuinely under-specified work.
  - On "fail", every reason MUST name the specific subtask id and the concrete \
fix ("J1-S1: AC 'works well' is not observable — state the measurable \
condition"). The generating session reads these to repair the ingest.
  - On "pass", "reasons" may be an empty list.
"""


@dataclass(frozen=True)
class SpecJudgeVerdict:
    passed: bool
    reasons: list[str]


def _spec_judge_enabled() -> bool:
    """The judge runs by default; ``HERMES_PLANSPEC_JUDGE=0`` (or false/off/no)
    turns it off for a deterministic-only ingest."""
    val = os.environ.get("HERMES_PLANSPEC_JUDGE", "").strip().lower()
    return val not in {"0", "false", "off", "no"}


def _resolve_spec_judge_model() -> str:
    """Resolve the judge model: env override > ``auxiliary.spec_judge.model``
    config > the pinned Sonnet default. The aux client owns provider/transport;
    here we only decide *which* model to request through it."""
    override = os.environ.get("HERMES_PLANSPEC_JUDGE_MODEL", "").strip()
    if override:
        return override
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        aux = cfg.get("auxiliary")
        if isinstance(aux, dict):
            task_cfg = aux.get(SPEC_JUDGE_TASK)
            if isinstance(task_cfg, dict):
                model = str(task_cfg.get("model") or "").strip()
                if model:
                    return model
    except Exception:  # pragma: no cover - defensive: never block on config read
        pass
    return SPEC_JUDGE_MODEL


def _spec_judge_subtask_blocks(spec: BindingPlanSpec) -> str:
    """Render each subtask (id, lane, title, body, resolved AC) for the judge."""
    children_by_id = {str(c.get("planspec_subtask_id") or ""): c for c in spec.children}
    blocks: list[str] = []
    for subtask in spec.hints.subtasks:
        child = children_by_id.get(subtask.id, {})
        acs = _ac_statements_for_child(child)
        lines = [
            f"### Subtask {subtask.id} (lane={subtask.lane})",
            f"Title: {subtask.title}",
        ]
        body = (subtask.body or "").strip()
        if body:
            lines.append(f"Body: {body}")
        deps = ", ".join(subtask.deps) if subtask.deps else "(none)"
        lines.append(f"Depends on: {deps}")
        if acs:
            lines.append("Acceptance criteria:")
            lines.extend(f"  - {a}" for a in acs)
        else:
            lines.append("Acceptance criteria: (none)")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _build_spec_judge_payload(spec: BindingPlanSpec) -> str:
    return "\n".join(
        [
            f"PlanSpec topic / goal: {spec.topic}",
            f"Freigabe (release gate): {spec.freigabe}",
            f"Live-test depth: {spec.live_test_depth}",
            "",
            "Subtasks (the binding taskgraph the worker fleet will execute):",
            "",
            _spec_judge_subtask_blocks(spec),
        ]
    )


def _parse_spec_judge_verdict(resp: Any) -> SpecJudgeVerdict | None:
    """Parse the judge reply into a verdict, or ``None`` when nothing usable
    can be extracted (drives the graceful fallback). Lenient: tolerates code
    fences and accepts either ``verdict: pass|fail`` or a boolean ``passed``."""
    try:
        raw = (resp.choices[0].message.content or "").strip()
    except Exception:
        return None
    if not raw:
        return None
    first = raw.find("{")
    last = raw.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    try:
        obj = json.loads(raw[first : last + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None

    passed: bool | None = None
    verdict_val = obj.get("verdict")
    if isinstance(verdict_val, str):
        v = verdict_val.strip().lower()
        if v in {"pass", "passed", "ok", "accept", "accepted"}:
            passed = True
        elif v in {"fail", "failed", "block", "blocked", "reject", "rejected"}:
            passed = False
    if passed is None and isinstance(obj.get("passed"), bool):
        passed = bool(obj["passed"])
    if passed is None:
        return None

    reasons_raw = obj.get("reasons")
    reasons: list[str] = []
    if isinstance(reasons_raw, list):
        reasons = [str(r).strip() for r in reasons_raw if str(r).strip()]
    elif isinstance(reasons_raw, str) and reasons_raw.strip():
        reasons = [reasons_raw.strip()]
    return SpecJudgeVerdict(passed=passed, reasons=reasons)


def _log_spec_judge_cost(resp: Any, model: str) -> None:
    """Best-effort cost observability for the judge call.

    The ``hermes plan ingest`` path runs OUTSIDE a kanban worker dispatch, so
    there is no ``task_runs`` row to attribute the cost to — K17 ``cost_usd`` is
    written per worker run by the dispatcher (kanban_db.task_runs), not for an
    ad-hoc CLI sub-call. We therefore cannot stamp ``task_runs.cost_usd`` from
    here; instead we log the token usage so the spend stays observable.
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    total = getattr(usage, "total_tokens", None)
    logger.info(
        "spec quality judge (%s) token usage: prompt=%s completion=%s total=%s "
        "(not stamped to task_runs — ingest runs outside a worker dispatch)",
        model,
        prompt,
        completion,
        total,
    )


def run_spec_quality_judge(spec: BindingPlanSpec) -> None:
    """Synchronous subjective quality judge — see the module section comment.

    Raises :class:`PlanSpecBlocked` (with the judge's reasons) on a ``fail``
    verdict. Returns ``None`` on a ``pass`` verdict OR on any graceful fallback
    (judge disabled, no client, infra/net/auth error, unparseable verdict).
    Meant to run synchronously in :func:`ingest_planspec` right after
    :func:`validate_spec_rubric` and before any DB write.
    """
    if not _spec_judge_enabled():
        logger.debug("spec quality judge disabled via HERMES_PLANSPEC_JUDGE")
        return

    try:
        from agent.auxiliary_client import (
            get_auxiliary_extra_body,
            get_text_auxiliary_client,
        )
    except Exception as exc:
        logger.warning(
            "spec quality judge: auxiliary client import failed (%s) — falling "
            "back to deterministic-only ingest",
            type(exc).__name__,
        )
        return

    try:
        client, _aux_model = get_text_auxiliary_client(SPEC_JUDGE_TASK)
    except Exception as exc:
        logger.warning(
            "spec quality judge: client resolution failed (%s) — falling back "
            "to deterministic-only ingest",
            type(exc).__name__,
        )
        return
    if client is None:
        logger.info(
            "spec quality judge: no auxiliary client configured — "
            "deterministic-only ingest (configure auxiliary.%s.* to enable)",
            SPEC_JUDGE_TASK,
        )
        return

    judge_model = _resolve_spec_judge_model()
    payload = _build_spec_judge_payload(spec)
    try:
        resp = client.chat.completions.create(
            model=judge_model,
            messages=[
                {"role": "system", "content": _SPEC_JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
            temperature=0.0,
            max_tokens=SPEC_JUDGE_MAX_TOKENS,
            timeout=SPEC_JUDGE_TIMEOUT,
            extra_body=get_auxiliary_extra_body() or None,
        )
    except Exception as exc:
        logger.warning(
            "spec quality judge: model call failed (%s) — falling back to "
            "deterministic-only ingest",
            type(exc).__name__,
        )
        return

    _log_spec_judge_cost(resp, judge_model)

    verdict = _parse_spec_judge_verdict(resp)
    if verdict is None:
        logger.warning(
            "spec quality judge: unparseable verdict from %s — falling back to "
            "deterministic-only ingest",
            judge_model,
        )
        return
    if not verdict.passed:
        reasons = verdict.reasons or ["failed without a stated reason"]
        raise PlanSpecBlocked(
            [f"spec quality judge ({judge_model}): {reason}" for reason in reasons]
        )
    logger.info("spec quality judge (%s): pass", judge_model)


def ingest_planspec(
    path: str | Path,
    *,
    board: str | None = None,
    author: str = "planspec-ingest",
    plans_root: Path = DEFAULT_PLANS_ROOT,
    force: bool = False,
) -> dict[str, Any]:
    spec = parse_binding_planspec(path, plans_root=plans_root)
    # Deterministic rubric gate — layered ON TOP of parse_binding_planspec's
    # structural validation and applied BEFORE any DB write. ``--force`` bypasses
    # it but the skipped reasons are logged so the override is never silent.
    if force:
        bypassed = _collect_spec_rubric_findings(spec)
        if bypassed:
            logger.warning(
                "PlanSpec rubric bypassed via --force for %s: %s",
                spec.path,
                "; ".join(bypassed),
            )
        else:
            logger.warning(
                "PlanSpec quality judge bypassed via --force for %s",
                spec.path,
            )
    else:
        # Deterministic rubric first (cheap, no LLM), then the synchronous
        # subjective Sonnet judge. Both run BEFORE any DB write; the judge
        # raises only on an actual fail verdict and degrades gracefully on any
        # infra trouble (see run_spec_quality_judge).
        validate_spec_rubric(spec)
        run_spec_quality_judge(spec)
    idempotency_key = ingest_idempotency_key(spec)
    conn = kanban_db.connect(board=board)
    try:
        # Idempotent re-ingest: if this exact PlanSpec was already ingested
        # (same source path + content), point back at the existing chain's
        # root instead of creating a second one. The root carries the key as
        # the durable marker; its subtasks are its tree-parents (the K2/F1
        # sink convention), so ``parent_ids`` recovers the existing chain.
        existing = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            root_id = existing["id"]
            existing_children = kanban_db.parent_ids(conn, root_id)
            return {
                "ok": True,
                "already_ingested": True,
                "path": str(spec.path),
                "root_task_id": root_id,
                "child_ids": existing_children,
                "children": spec.children,
                "freigabe": spec.freigabe,
                "live_test_depth": spec.live_test_depth,
                "subtask_count": len(existing_children),
                "idempotency_key": idempotency_key,
            }

        root_title = f"PlanSpec {spec.frontmatter.get('slice') or spec.path.stem}: {spec.topic}"
        # A2/#8: stamp freigabe + live_test_depth from the PlanSpec frontmatter
        # as part of the INSERT so the provenance is atomic with row creation —
        # no separate UPDATE window where a reader sees NULL, and no way for an
        # exception between INSERT and a follow-up UPDATE to strand the fields.
        root_id = kanban_db.create_task(
            conn,
            title=root_title,
            body=build_root_body(spec),
            assignee=None,
            created_by=author,
            tenant="planspec",
            priority=0,
            triage=True,
            idempotency_key=idempotency_key,
            freigabe=spec.freigabe,
            live_test_depth=spec.live_test_depth,
        )
        with kanban_db.write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'triage'",
                (root_id,),
            )
            if cur.rowcount != 1:
                raise PlanSpecBlocked([f"root task {root_id} left triage before scheduling"])
        kanban_db.add_event(conn, root_id, "specified", {"source": "planspec_ingest", "path": str(spec.path)})
        if not kanban_db.schedule_task(conn, root_id, reason="Planspec ingest: held before release"):
            raise PlanSpecBlocked([f"could not park root task {root_id} in scheduled"])
        try:
            child_ids = kanban_db.decompose_triage_task(
                conn,
                root_id,
                root_assignee=None,
                children=spec.children,
                author=author,
                auto_promote=False,
                initial_child_status="scheduled",
                expected_root_status="scheduled",
            )
        except ValueError as exc:
            raise PlanSpecBlocked([f"DB rejected binding taskgraph: {exc}"]) from exc
        if child_ids is None:
            raise PlanSpecBlocked([f"could not ingest taskgraph for root {root_id}"])
        return {
            "ok": True,
            "already_ingested": False,
            "path": str(spec.path),
            "root_task_id": root_id,
            "child_ids": child_ids,
            "children": spec.children,
            "freigabe": spec.freigabe,
            "live_test_depth": spec.live_test_depth,
            "subtask_count": len(child_ids),
            "idempotency_key": idempotency_key,
        }
    finally:
        conn.close()


def sprint_prompt_for_planspec(path: str | Path, *, plans_root: Path = DEFAULT_PLANS_ROOT) -> dict[str, Any]:
    spec = parse_binding_planspec(path, plans_root=plans_root)
    lines = [
        "Arbeite ISOLIERT in einem eigenen Worktree, nie im Live-Main.",
        f"Implementiere die Planspec end-to-end: {spec.path}",
        f"Freigabe: {spec.freigabe}; live_test_depth: {spec.live_test_depth}.",
        "Baue, aktiviere die Runtime, führe die passenden Live-Tests aus, rollbacke automatisch bei Live-Fail und pushe nie auf origin.",
        "",
        "Taskgraph-Hints sind bindend und deterministisch abzuarbeiten:",
    ]
    for task in spec.hints.subtasks:
        deps = f" deps=[{', '.join(task.deps)}]" if task.deps else ""
        lines.append(f"- {task.id} · lane={task.lane}{deps}: {task.title}")
    lines.extend(
        [
            "",
            "Gates: ruff + scripts/run_tests.sh; web: npm run lint:control + npx tsc -b --noEmit + npx vitest run + npm run build.",
        ]
    )
    return {"path": str(spec.path), "prompt": "\n".join(lines)}
