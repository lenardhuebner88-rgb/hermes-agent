"""PlanSpec discovery, binding validation, and deterministic Kanban ingest."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from typing import Any, Literal

import yaml

from hermes_cli import kanban_db
from hermes_cli.plan_compiler import CompileBlocked, TaskgraphHints, _extract_frontmatter, taskgraph_hints_to_children

DEFAULT_PLANS_ROOT = Path("/home/piet/vault/03-Agents")
LIVE_TEST_DEPTHS = {"smoke", "contract", "ui-real"}
PlanSpecScope = Literal["open", "all"]

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
    candidate = Path(path).expanduser().resolve(strict=False)
    root = plans_root.expanduser().resolve(strict=False)
    if not _is_relative_to(candidate, root):
        raise PlanSpecBlocked([f"planspec path must be under {root}"])
    if candidate.suffix.lower() != ".md":
        raise PlanSpecBlocked(["planspec path must point to a markdown file"])
    if not candidate.is_file():
        raise PlanSpecBlocked([f"planspec file not found: {candidate}"])
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

    try:
        children = taskgraph_hints_to_children(hints)
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


def list_planspecs(
    *,
    plans_root: Path = DEFAULT_PLANS_ROOT,
    scope: PlanSpecScope = "open",
    valid: bool | None = None,
    limit: int | None = None,
    search: str | None = None,
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
            open_record = closed is None and (valid or display_only_open)
            record_errors = list(errors)
            if display_only_open:
                record_errors.append("display-only: taskgraph_hints.binding is missing; Kanban ingest disabled")
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
                    "closed_reason": closed if closed else (None if open_record else "not a binding PlanSpec"),
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
    return "\n".join(
        [
            f"PlanSpec source: {spec.path}",
            f"Freigabe: {spec.freigabe}",
            f"Live-Test-Depth: {spec.live_test_depth}",
            "",
            "Frontmatter:",
            "```yaml",
            fm,
            "```",
        ]
    )


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


def ingest_planspec(
    path: str | Path,
    *,
    board: str | None = None,
    author: str = "planspec-ingest",
    plans_root: Path = DEFAULT_PLANS_ROOT,
) -> dict[str, Any]:
    spec = parse_binding_planspec(path, plans_root=plans_root)
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
