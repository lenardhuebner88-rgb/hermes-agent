"""Shared Kanban task template builder.

The builder owns the canonical YAML ordering for scope/report authoring.
Callers can still pass free-form task prose, but the policy header is a
structured object so it round-trips through kanban_db's parser.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping


DEFAULT_ALLOWED_TOOLS = [
    "kanban_show",
    "kanban_complete",
    "kanban_block",
    "kanban_comment",
    "kanban_heartbeat",
]
DEFAULT_FORBIDDEN_SYSTEMS = ["OpenClaw", "Atlas", "Mission-Control", "Telegram"]


@dataclass(frozen=True)
class TaskTemplate:
    assignee: str
    allowed_tools: list[str]
    forbidden_systems: list[str]
    skills: list[str]
    scope_contract: dict[str, Any]
    report_contract_version: int
    body_template: str

    def as_task_kwargs(self) -> dict[str, Any]:
        return {
            "assignee": self.assignee,
            "body": self.body_template,
            "skills": list(self.skills) or None,
        }


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _dump_scalar(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _dump_string_list(name: str, values: list[str]) -> list[str]:
    if not values:
        return [f"{name}: []"]
    prefix = name[: len(name) - len(name.lstrip())]
    return [f"{name}:", *[f"{prefix}  - {_dump_scalar(value)}" for value in values]]


def _render_body(
    *,
    assignee: str,
    scope_contract: Mapping[str, Any],
    report_contract_version: int,
    body: str,
) -> str:
    allowed = _as_list(scope_contract.get("allowed_tools"))
    forbidden = _as_list(scope_contract.get("forbidden_systems"))
    version = int(scope_contract.get("version") or 2)
    lines = [
        "---",
        f"assignee: {_dump_scalar(assignee)}",
        f"report_contract_version: {int(report_contract_version)}",
        "scope_contract:",
        f"  version: {version}",
        *_dump_string_list("  allowed_tools", allowed),
        *_dump_string_list("  forbidden_systems", forbidden),
        "---",
        "",
        (body or "").strip(),
    ]
    rendered = "\n".join(lines).rstrip()
    return rendered + "\n"


def build_task_template(
    profile: str,
    scope_contract: Mapping[str, Any] | None,
    report_contract_version: int = 1,
    **overrides: Any,
) -> TaskTemplate:
    """Build a canonical Kanban task body for an assignee profile."""

    assignee = str(overrides.get("assignee") or profile or "").strip()
    if not assignee:
        raise ValueError("profile is required")
    contract = dict(scope_contract or {})
    contract["version"] = int(contract.get("version") or 2)
    allowed = _as_list(overrides.get("allowed_tools") or contract.get("allowed_tools"))
    if not allowed:
        allowed = list(DEFAULT_ALLOWED_TOOLS)
    forbidden = _as_list(
        overrides.get("forbidden_systems") or contract.get("forbidden_systems")
    )
    if not forbidden:
        forbidden = list(DEFAULT_FORBIDDEN_SYSTEMS)
    skills = _as_list(overrides.get("skills") or contract.get("skills"))

    contract["allowed_tools"] = allowed
    contract["forbidden_systems"] = forbidden
    contract.pop("skills", None)
    version = int(report_contract_version)
    body = str(overrides.get("body") or overrides.get("body_template") or "").strip()
    body_template = _render_body(
        assignee=assignee,
        scope_contract=contract,
        report_contract_version=version,
        body=body,
    )
    return TaskTemplate(
        assignee=assignee,
        allowed_tools=allowed,
        forbidden_systems=forbidden,
        skills=skills,
        scope_contract=contract,
        report_contract_version=version,
        body_template=body_template,
    )
