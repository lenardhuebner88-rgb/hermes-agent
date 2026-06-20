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
VALID_REVIEW_TIERS = {"standard", "review", "critical"}

VALID_PLANSPEC_LANES = {
    "coder",
    "coder-claude",
    "premium",
    "scout",
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
    # cross-family review = a CC orchestration review-ladder concept (Claude
    # builds → Codex reviews); NOT a Hermes lane. Baked into a worker AC (e.g.
    # "Codex-Cross-Family-Review on the diffs") it is unfulfillable by a headless
    # worker. Real leak 2026-06-19 (verifier-acceptance spec). Matches inside
    # "codex-cross-family-review" via the \b…\b scan.
    "cross-family",
}

# Template-residue patterns: a literal ``<…>`` angle placeholder, a TODO/FIXME/TBD
# marker, or a bare ``…`` / ``...`` ellipsis left over from a spec template.
# The TODO/FIXME/TBD marker is CASE-SENSITIVE (no re.IGNORECASE): the literal
# template markers are all-caps by convention, so the lowercase kanban status word
# ``todo`` is not a false positive. A genuine all-caps marker still blocks.
_RESIDUE_ANGLE_RE = re.compile(r"<[^<>\n]{1,80}>")
_RESIDUE_MARKER_RE = re.compile(r"\b(?:TODO|FIXME|TBD)\b")
_RESIDUE_ELLIPSIS_RE = re.compile(r"\.\.\.|…")

# An *obvious* path token: slash-joined word segments with an optional leading dot
# — e.g. ``a/b/c.py``, ``.worktrees/kanban/t_x``, ``hermes_cli/planspecs.py``. A
# documentary file/path reference, NOT template residue, so it is masked out
# (together with backtick code spans) before the residue scan. Angle brackets are
# not in the segment class, so a genuine ``foo/<id>`` placeholder is never hidden.
_PATH_TOKEN_RE = re.compile(r"\.?\w[\w.\-]*(?:/[\w.\-]+)+")

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
            grounding=_meta_str(meta.get("grounding")),
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


def _mask_code_spans(text: str) -> str:
    """Return *text* with backtick code spans (inline ``code`` or fenced
    ```` ```…``` ````) replaced by equal-length runs of spaces (newlines kept), so
    a marker that is only *quoted* inside code is not mistaken for unfilled
    template residue.

    Implements the CommonMark code-span rule: a run of *N* backticks opens a span
    that is closed by the next run of *exactly* *N* backticks; everything between
    (delimiters included) is code. An opener with no matching closer is left as
    literal text — a lone stray backtick never swallows the rest of the line. This
    also correctly handles the nested backtick *display* of inline/fence examples
    (e.g. ``(`` `…` ``)``). Offsets are preserved 1:1 so the angle/ellipsis span
    bookkeeping in :func:`_residue_tokens` stays valid.
    """
    chars = list(text)
    n = len(chars)
    i = 0
    while i < n:
        if chars[i] != "`":
            i += 1
            continue
        run_start = i
        while i < n and chars[i] == "`":
            i += 1
        run_len = i - run_start
        # Hunt for a closing run of *exactly* run_len backticks.
        k = i
        closer_end = -1
        while k < n:
            if chars[k] != "`":
                k += 1
                continue
            close_start = k
            while k < n and chars[k] == "`":
                k += 1
            if (k - close_start) == run_len:
                closer_end = k
                break
            # A run of a different length cannot close this span — keep scanning.
        if closer_end == -1:
            # Unbalanced opener: leave it literal, resume just past the run.
            continue
        for p in range(run_start, closer_end):
            if chars[p] != "\n":
                chars[p] = " "
        i = closer_end
    return "".join(chars)


def _strip_code_and_paths(text: str) -> str:
    """Mask backtick code spans and obvious path tokens out of *text* before the
    residue scan. Equal-length blank replacement keeps character offsets stable.
    """
    masked = _mask_code_spans(text)
    return _PATH_TOKEN_RE.sub(lambda m: " " * len(m.group(0)), masked)


def _residue_tokens(text: str) -> list[str]:
    """Return distinct template-residue tokens found in *text* (order-preserving).

    A ``<…>`` angle placeholder reports the whole bracketed token; an ellipsis
    that sits *inside* an angle placeholder is not reported twice.

    Markers that are only *quoted* inside a backtick code span / code fence, or
    that are part of an obvious path token, are documentary citations — NOT an
    unfilled template slot — so those spans are masked out (replaced by
    equal-length blanks, offsets preserved) before the scan. A genuine unfilled
    placeholder sitting in prose is still caught, and the bare ``…``/``...``
    ellipsis is only flagged *outside* of code.
    """
    if not text:
        return []
    scan = _strip_code_and_paths(text)
    found: list[str] = []
    seen: set[str] = set()
    angle_spans: list[tuple[int, int]] = []

    def _add(token: str) -> None:
        if token and token not in seen:
            seen.add(token)
            found.append(token)

    for match in _RESIDUE_ANGLE_RE.finditer(scan):
        angle_spans.append(match.span())
        _add(match.group(0))
    for match in _RESIDUE_MARKER_RE.finditer(scan):
        _add(match.group(0))
    for match in _RESIDUE_ELLIPSIS_RE.finditer(scan):
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

        # 4c) B: review_tier (if set) must be a known staged-review tier.
        rt = (getattr(subtask, "review_tier", "") or "").strip().lower()
        if rt and rt not in VALID_REVIEW_TIERS:
            findings.append(f"unknown review_tier in {sid}: {subtask.review_tier}")

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


def _spec_is_signed(spec: BindingPlanSpec) -> bool:
    """A PlanSpec is *operator-signed* when ``approved_by`` is set (non-empty)
    AND ``freigabe == "complete"``.

    A signed spec's deterministic-rubric findings are logged as warnings rather
    than blocking ingest, and the subjective quality judge is skipped — the
    explicit operator sign-off replaces them. The structural validation in
    :func:`parse_binding_planspec` stays hard for everyone (it runs first).

    Both conditions are required on purpose: every existing rubric block-test
    sets ``freigabe: complete`` but no ``approved_by`` and so stays unsigned and
    fully gated. Strategist-authored specs (no ``approved_by``) likewise remain
    gated.
    """
    approved_by = str(spec.frontmatter.get("approved_by") or "").strip()
    return bool(approved_by) and spec.freigabe.strip().lower() == "complete"


def validate_planspec(
    path: str | Path, *, plans_root: Path = DEFAULT_PLANS_ROOT
) -> dict[str, Any]:
    """Read-only validation preview for a PlanSpec — creates NOTHING, opens NO DB
    connection. The dry-run companion to :func:`ingest_planspec`.

    Runs the same structural parse and deterministic rubric, but never raises and
    never writes. Returns a result dict whose ``disposition`` says what an
    ``ingest`` (without ``--force``) would do:

    * ``clean``   — no rubric findings; ingests cleanly.
    * ``warn``    — operator-signed (see :func:`_spec_is_signed`) WITH findings;
      ingests, logging the findings as warnings.
    * ``block``   — unsigned WITH findings; ingest blocks (fix, sign, or --force).
    * ``invalid`` — structural / YAML / missing-required-field error; blocks for
      everyone regardless of signing.

    Note: the subjective quality judge is NOT run here (it needs the network and
    is non-deterministic). A ``clean`` unsigned spec can still be stopped by the
    judge on a real ingest.
    """
    try:
        spec = parse_binding_planspec(path, plans_root=plans_root)
    except PlanSpecBlocked as exc:
        return {
            "ok": False,
            "disposition": "invalid",
            "path": str(path),
            "signed": False,
            "approved_by": "",
            "freigabe": "",
            "findings": exc.findings,
            "would_block": True,
        }

    findings = _collect_spec_rubric_findings(spec)
    signed = _spec_is_signed(spec)
    if not findings:
        disposition = "clean"
    elif signed:
        disposition = "warn"
    else:
        disposition = "block"
    return {
        "ok": disposition in ("clean", "warn"),
        "disposition": disposition,
        "path": str(spec.path),
        "signed": signed,
        "approved_by": str(spec.frontmatter.get("approved_by") or "").strip(),
        "freigabe": spec.freigabe,
        "findings": findings,
        "would_block": disposition in ("block", "invalid"),
    }


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


def _planspec_identity(spec: BindingPlanSpec) -> tuple[str, str]:
    """Return the (slice, source-path) identity pair for *spec*.

    ``slice`` is the primary identity (a moved file keeps it); the resolved
    source path is the fallback (an in-place edit keeps it). Either matching a
    prior chain marks the same logical PlanSpec — see :func:`_find_superseding_conflicts`.
    """
    slice_id = str(spec.frontmatter.get("slice") or "").strip()
    return slice_id, str(spec.path)


def _find_superseding_conflicts(
    conn, spec: BindingPlanSpec, current_key: str
) -> list[str]:
    """Find non-archived PlanSpec chains that share *spec*'s identity but were
    ingested from *different* content (a changed ``.md`` → would duplicate).

    Identity is matched on the frontmatter ``slice`` (primary) OR the resolved
    source path (fallback), recovered from the durable ``specified`` ingest
    event. Returns the conflicting root ids, most-recent first. The byte-identical
    re-ingest is handled earlier by the exact idempotency-key no-op, so any hit
    here necessarily carries a different content hash.
    """
    slice_id, spec_path = _planspec_identity(spec)
    rows = conn.execute(
        "SELECT task_id, payload FROM task_events "
        "WHERE kind = 'specified' AND payload LIKE ? "
        "ORDER BY created_at DESC, id DESC LIMIT 500",
        ("%planspec_ingest%",),
    ).fetchall()
    conflicts: list[str] = []
    seen: set[str] = set()
    for row in rows:
        root_id = str(row["task_id"])
        if root_id in seen:
            continue
        seen.add(root_id)
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("source") != "planspec_ingest":
            continue
        event_path = str(payload.get("path") or "")
        event_slice = str(payload.get("slice") or "").strip()
        same_identity = (
            (bool(event_path) and event_path == spec_path)
            or (bool(slice_id) and bool(event_slice) and slice_id == event_slice)
        )
        if not same_identity:
            continue
        root = kanban_db.get_task(conn, root_id)
        if root is None or root.status == "archived":
            continue
        # The exact-content chain is the idempotent no-op, never its own conflict.
        if root.idempotency_key == current_key:
            continue
        conflicts.append(root_id)
    return conflicts


def _parse_deps_from_body(body: str) -> frozenset[str]:
    """Extract symbolic dependency ids from a persisted subtask body.

    The plan compiler writes ``"Depends on: B1-S1, B1-S2"`` as a dedicated body
    line; parse that line and return the ids as a frozenset so comparison is
    order-insensitive (a pure reorder is not a change).  Returns an empty
    frozenset when no such line is present (i.e. the subtask has no deps).
    """
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Depends on:"):
            raw = stripped[len("Depends on:"):].strip()
            if not raw:
                return frozenset()
            return frozenset(part.strip() for part in raw.split(",") if part.strip())
    return frozenset()


def _describe_chain_diff(conn, spec: BindingPlanSpec, root_id: str) -> list[str]:
    """Human-readable summary of how *spec* differs from the live chain *root_id*.

    Compares the durable provenance the chain persisted (root ``freigabe`` /
    ``live_test_depth`` columns and per-subtask ``planspec_subtask_id`` / title /
    lane / deps) against the new spec, so the abort message says *what* changed.
    Deps are compared order-insensitively (as sets) so a pure reorder is not
    reported as a change.
    """
    lines: list[str] = []
    root_row = conn.execute(
        "SELECT freigabe, live_test_depth FROM tasks WHERE id = ?",
        (root_id,),
    ).fetchone()
    old_freigabe = str((root_row["freigabe"] if root_row else "") or "").strip()
    old_depth = str((root_row["live_test_depth"] if root_row else "") or "").strip()
    if old_freigabe != spec.freigabe:
        lines.append(f"freigabe: {old_freigabe or '∅'} → {spec.freigabe or '∅'}")
    if old_depth != spec.live_test_depth:
        lines.append(f"live_test_depth: {old_depth or '∅'} → {spec.live_test_depth or '∅'}")

    # tuple: (title, lane/assignee, deps_frozenset)
    old_subtasks: dict[str, tuple[str, str, frozenset[str]]] = {}
    for sid in kanban_db.parent_ids(conn, root_id):
        srow = conn.execute(
            "SELECT title, assignee, planspec_subtask_id, body FROM tasks WHERE id = ?",
            (sid,),
        ).fetchone()
        if srow is None:
            continue
        key = str(srow["planspec_subtask_id"] or "").strip() or f"#{sid}"
        old_deps = _parse_deps_from_body(str(srow["body"] or ""))
        old_subtasks[key] = (str(srow["title"] or ""), str(srow["assignee"] or ""), old_deps)
    new_subtasks: dict[str, tuple[str, str, frozenset[str]]] = {}
    for child in spec.children:
        key = str(child.get("planspec_subtask_id") or "").strip() or str(child.get("title") or "")
        new_deps = frozenset(child.get("planspec_deps") or [])
        new_subtasks[key] = (str(child.get("title") or ""), str(child.get("assignee") or ""), new_deps)

    added = sorted(k for k in new_subtasks if k not in old_subtasks)
    removed = sorted(k for k in old_subtasks if k not in new_subtasks)
    if added:
        lines.append(f"subtasks added: {', '.join(added)}")
    if removed:
        lines.append(f"subtasks removed: {', '.join(removed)}")
    for key in sorted(k for k in new_subtasks if k in old_subtasks):
        old_title, old_lane, old_deps = old_subtasks[key]
        new_title, new_lane, new_deps = new_subtasks[key]
        parts: list[str] = []
        if old_title != new_title:
            parts.append(f"title {old_title!r} → {new_title!r}")
        if old_lane != new_lane:
            parts.append(f"lane {old_lane} → {new_lane}")
        if old_deps != new_deps:
            old_str = "[" + ", ".join(sorted(old_deps)) + "]"
            new_str = "[" + ", ".join(sorted(new_deps)) + "]"
            parts.append(f"deps {old_str} → {new_str}")
        if parts:
            lines.append(f"subtask {key}: " + "; ".join(parts))
    if not lines:
        lines.append("content hash changed (body/whitespace) with no structural field diff")
    return lines


def _archive_planspec_chain(conn, root_id: str) -> None:
    """Archive a whole PlanSpec chain (every subtask, then the root sink).

    Reuses :func:`kanban_db.archive_task` so the existing archive bookkeeping
    (run reclaim, ``archived`` event, ``recompute_ready``) runs per task.
    """
    for sid in kanban_db.parent_ids(conn, root_id):
        kanban_db.archive_task(conn, sid)
    kanban_db.archive_task(conn, root_id)


def ingest_planspec(
    path: str | Path,
    *,
    board: str | None = None,
    author: str = "planspec-ingest",
    plans_root: Path = DEFAULT_PLANS_ROOT,
    force: bool = False,
    supersede: bool = False,
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
    elif _spec_is_signed(spec):
        # Operator-signed (approved_by + freigabe==complete): the deterministic
        # rubric is advisory here — collect findings and log them, but do NOT
        # block, and SKIP the subjective judge (the explicit sign-off replaces
        # it; otherwise a non-deterministic LLM call would gate a spec the
        # operator already approved). Structural validation already ran above
        # (parse_binding_planspec) and stays hard for everyone.
        warned = _collect_spec_rubric_findings(spec)
        if warned:
            logger.warning(
                "PlanSpec rubric findings WARNED (not blocked) for operator-signed "
                "spec (approved_by=%s) %s: %s",
                str(spec.frontmatter.get("approved_by") or "").strip(),
                spec.path,
                "; ".join(warned),
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

        # F6: the exact-content chain didn't match, but a chain with the SAME
        # identity (frontmatter ``slice`` or source path) and a DIFFERENT content
        # hash means the PlanSpec was edited. Refuse to silently mint a duplicate
        # chain; require an explicit ``--supersede`` to archive the stale one.
        conflicts = _find_superseding_conflicts(conn, spec, idempotency_key)
        superseded: list[str] = []
        if conflicts:
            if not supersede:
                detail = _describe_chain_diff(conn, spec, conflicts[0])
                findings = [
                    "PlanSpec changed since it was last ingested — a live (non-archived) "
                    f"chain already exists for this identity (root {conflicts[0]}"
                    + (f", +{len(conflicts) - 1} more" if len(conflicts) > 1 else "")
                    + "). Re-run with --supersede to archive the stale chain and ingest "
                    "the new version. Changed:",
                    *detail,
                ]
                raise PlanSpecBlocked(findings)
            # --supersede requested: refuse while any stale chain has a worker
            # mid-flight (status='running') — that is an operator call, not ours.
            running_blockers: list[str] = []
            for stale_root in conflicts:
                running = kanban_db.planspec_chain_running_subtasks(conn, stale_root)
                if running:
                    running_blockers.append(
                        f"root {stale_root}: running subtask(s) {', '.join(running)}"
                    )
            if running_blockers:
                raise PlanSpecBlocked(
                    [
                        "--supersede refused: the prior chain has running children — "
                        "an operator must let them finish or stop them before superseding.",
                        *running_blockers,
                    ]
                )
            # NOTE: archive-then-create is not one atomic transaction (each
            # archive_task + the new-chain INSERT carry their own write_txn). If
            # the process dies between archiving here and creating the new root,
            # the stale chain is archived and no new chain exists — this is
            # RECOVERABLE: re-run ``hermes plan ingest --supersede`` (the conflict
            # scan skips archived roots, so the new version then ingests cleanly).
            for stale_root in conflicts:
                _archive_planspec_chain(conn, stale_root)
                superseded.append(stale_root)

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
        kanban_db.add_event(
            conn,
            root_id,
            "specified",
            {
                "source": "planspec_ingest",
                "path": str(spec.path),
                # F6: persist the frontmatter slice so a moved-but-same-slice
                # re-ingest is recognised as the same identity. Additive — the
                # existing readers only consult ``source``/``path``.
                "slice": str(spec.frontmatter.get("slice") or "").strip(),
            },
        )
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
            "superseded": superseded,
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
