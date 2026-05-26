"""Read-only ``hermes kanban validate-spec`` implementation."""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db import (
    _extract_frontmatter_block,
    _iter_task_policy_mappings,
    _resolve_scope_runtime_tool_schema_names,
    _safe_yaml_mapping,
    _selected_scope_contract,
    _validate_scope_allowed_tools,
    _validate_scope_forbidden_systems,
    _validate_task_extra_skills,
)


_FAST_RUNTIME_SCHEMA_NAMES = {
    "kanban_show",
    "kanban_complete",
    "kanban_block",
    "kanban_comment",
    "kanban_heartbeat",
}


@dataclass(frozen=True)
class TaskSpec:
    source: str
    task_id: Optional[str]
    title: Optional[str]
    body: str
    assignee: Optional[str]
    skills: list[str]


def _issue(code: str, message: str, *, severity: str = "error") -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _frontmatter_mapping(body: str) -> Optional[dict[str, Any]]:
    block = _extract_frontmatter_block(body)
    if block is None:
        return None
    return _safe_yaml_mapping(block)


def _row_spec(task_id: str) -> TaskSpec:
    path = kb.kanban_db_path()
    if not path.exists():
        raise FileNotFoundError(f"kanban DB not found: {path}")
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, title, body, assignee, skills FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise LookupError(f"no such task: {task_id}")
    try:
        skills = json.loads(row["skills"]) if row["skills"] else []
    except Exception:
        skills = []
    return TaskSpec(
        source="db",
        task_id=str(row["id"]),
        title=row["title"],
        body=row["body"] or "",
        assignee=row["assignee"],
        skills=[str(s) for s in skills if str(s).strip()],
    )


def _file_spec(path: Path) -> TaskSpec:
    body = path.read_text(encoding="utf-8")
    fm = _frontmatter_mapping(body) or {}
    raw_skills = fm.get("skills") or []
    if isinstance(raw_skills, str):
        raw_skills = [raw_skills]
    return TaskSpec(
        source="file",
        task_id=None,
        title=fm.get("title"),
        body=body,
        assignee=fm.get("assignee"),
        skills=[str(s).strip() for s in raw_skills if str(s).strip()],
    )


def load_task_spec(target: str, *, task_id_mode: bool = False) -> TaskSpec:
    target = str(target or "").strip()
    if not target:
        raise ValueError("task file path or task id is required")
    path = Path(target).expanduser()
    if not task_id_mode and path.exists():
        return _file_spec(path)
    return _row_spec(target)


def validate_task_spec(spec: TaskSpec) -> dict[str, Any]:
    started = time.perf_counter()
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    body = spec.body or ""
    frontmatter = _frontmatter_mapping(body)
    if _extract_frontmatter_block(body) is not None and frontmatter is None:
        errors.append(_issue("invalid_frontmatter", "Markdown frontmatter must parse as a YAML mapping"))

    mappings = _iter_task_policy_mappings(body)
    contract_fingerprints = {
        json.dumps(m.get("scope_contract"), sort_keys=True, ensure_ascii=False)
        for m in mappings
        if isinstance(m.get("scope_contract"), dict)
    }
    if len(contract_fingerprints) > 1:
        errors.append(_issue("duplicate_scope_contract", "Multiple scope_contract blocks found"))
    contract = _selected_scope_contract(body)
    if not isinstance(contract, dict):
        errors.append(_issue("missing_scope_contract", "scope_contract version 2 is required"))
    else:
        try:
            if int(contract.get("version")) < 2:
                errors.append(_issue("missing_scope_contract", "scope_contract.version must be >= 2"))
        except (TypeError, ValueError):
            errors.append(_issue("missing_scope_contract", "scope_contract.version must be >= 2"))

    assignee = spec.assignee or (frontmatter or {}).get("assignee")
    if not assignee:
        errors.append(_issue("invalid_assignee", "assignee is required"))
    else:
        try:
            from hermes_cli.profiles import profile_exists

            if not profile_exists(str(assignee)):
                errors.append(_issue("invalid_assignee", f"unknown assignee profile: {assignee}"))
        except ValueError as exc:
            errors.append(_issue("invalid_assignee", str(exc)))

    effective_tools: list[str] = []
    resolved_tools: list[str] = []
    allowed_errors: list[str] = []
    if isinstance(contract, dict):
        effective_tools, allowed_errors = _validate_scope_allowed_tools(body)
        errors.extend(_issue("invalid_allowed_tools", msg) for msg in allowed_errors)
        errors.extend(
            _issue("missing_forbidden_systems", msg)
            for msg in _validate_scope_forbidden_systems(body)
        )
        if effective_tools and not allowed_errors:
            if set(effective_tools).issubset(_FAST_RUNTIME_SCHEMA_NAMES):
                resolved_tools = list(effective_tools)
            else:
                resolved_tools = _resolve_scope_runtime_tool_schema_names(
                    effective_tools,
                    task_id=spec.task_id or "__validate_spec__",
                    profile=str(assignee) if assignee else None,
                )
            missing_runtime = [name for name in effective_tools if name not in set(resolved_tools)]
            if missing_runtime:
                errors.append(
                    _issue(
                        "invalid_allowed_tools",
                        "runtime tool schema missing declared allowed tools: "
                        + ", ".join(missing_runtime),
                    )
                )

    if spec.skills and assignee:
        missing_skills = _validate_task_extra_skills(spec.skills, profile=str(assignee))
        if missing_skills:
            errors.append(
                _issue("unknown_skills", "unknown force-loaded skill(s): " + ", ".join(missing_skills))
            )

    report_version = (frontmatter or {}).get("report_contract_version")
    if report_version is None:
        warnings.append(
            _issue(
                "missing_report_contract_version",
                "report_contract_version missing; defaulting to 1",
                severity="warning",
            )
        )
        report_version = 1

    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "schema": "kanban.validate_spec.v1",
        "ok": not errors,
        "source": spec.source,
        "task_id": spec.task_id,
        "title": spec.title,
        "assignee": assignee,
        "report_contract_version": report_version,
        "scope_contract_version": contract.get("version") if isinstance(contract, dict) else None,
        "effective_toolsets": effective_tools,
        "runtime_tool_schema_names": resolved_tools,
        "skills": spec.skills,
        "errors": errors,
        "warnings": warnings,
        "diagnostics": {"elapsed_ms": elapsed_ms},
    }


def main(args: argparse.Namespace) -> int:
    try:
        spec = load_task_spec(
            getattr(args, "target", None) or getattr(args, "task_id", None),
            task_id_mode=bool(getattr(args, "task_id", None)),
        )
        payload = validate_task_spec(spec)
    except Exception as exc:
        payload = {
            "schema": "kanban.validate_spec.v1",
            "ok": False,
            "source": "unknown",
            "task_id": getattr(args, "task_id", None),
            "errors": [_issue("load_error", str(exc))],
            "warnings": [],
            "diagnostics": {"elapsed_ms": 0.0},
        }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if payload.get("ok") else 1
