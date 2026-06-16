"""PlanSpec discovery, binding validation, and deterministic Kanban ingest."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from hermes_cli import kanban_db
from hermes_cli.plan_compiler import CompileBlocked, TaskgraphHints, _extract_frontmatter, taskgraph_hints_to_children

DEFAULT_PLANS_ROOT = Path("/home/piet/vault/03-Agents")
LIVE_TEST_DEPTHS = {"smoke", "contract", "ui-real"}


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
        status=str(frontmatter.get("status") or "").strip(),
        freigabe=freigabe,
        live_test_depth=live_test_depth,
        hints=hints,
        children=children,
    )


def list_planspecs(*, plans_root: Path = DEFAULT_PLANS_ROOT) -> list[dict[str, Any]]:
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
            records.append(
                {
                    "path": str(path.resolve(strict=False)),
                    "agent": path.parent.parent.name,
                    "filename": path.name,
                    "topic": topic,
                    "status": str(frontmatter.get("status") or "").strip(),
                    "freigabe": str(frontmatter.get("freigabe") or "").strip(),
                    "live_test_depth": live_test_depth or None,
                    "binding": bool(hints.binding) if hints else False,
                    "subtask_count": len(hints.subtasks) if hints else 0,
                    "valid": not errors and bool(hints and hints.binding),
                    "errors": errors,
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
                    "errors": [str(exc)],
                }
            )
    records.sort(key=lambda item: (item["valid"] is False, item["path"]), reverse=False)
    return records


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


def ingest_planspec(
    path: str | Path,
    *,
    board: str | None = None,
    author: str = "planspec-ingest",
    plans_root: Path = DEFAULT_PLANS_ROOT,
) -> dict[str, Any]:
    spec = parse_binding_planspec(path, plans_root=plans_root)
    conn = kanban_db.connect(board=board)
    try:
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
            "path": str(spec.path),
            "root_task_id": root_id,
            "child_ids": child_ids,
            "children": spec.children,
            "freigabe": spec.freigabe,
            "live_test_depth": spec.live_test_depth,
            "subtask_count": len(child_ids),
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
