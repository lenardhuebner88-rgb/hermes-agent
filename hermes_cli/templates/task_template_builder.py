"""Shared Kanban task template builder.

The builder owns the canonical YAML ordering for scope/report authoring.
Callers can still pass free-form task prose, but the policy header is a
structured object so it round-trips through kanban_db's parser.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
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


@dataclass(frozen=True)
class RawCreateLintRoute:
    """Lint decision for create calls that did not use the template builder."""

    lint_payload: dict[str, Any]
    triage: bool
    routed_to_triage: bool
    reason: str | None = None


class AuthoringLintError(ValueError):
    """Raised when a template-authored task fails the authoring lint gate."""

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        codes = [
            str(item.get("code"))
            for item in payload.get("errors", [])
            if item.get("code")
        ]
        detail = ", ".join(codes) if codes else "unknown_error"
        super().__init__(f"authoring lint failed: {detail}")


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


def authoring_lint_mode() -> str:
    """Return the authoring lint mode from the process environment."""

    mode = os.environ.get("HERMES_AUTHORING_LINT", "enforce").strip().lower()
    if mode in {"warn", "warning"}:
        return "warn"
    return "enforce"


def format_authoring_lint_errors(payload: Mapping[str, Any]) -> str:
    errors = payload.get("errors") or []
    if not errors:
        return "unknown_error"
    parts: list[str] = []
    for item in errors:
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("code") or "error")
        message = str(item.get("message") or "").strip()
        parts.append(f"{code}: {message}" if message else code)
    return "; ".join(parts) if parts else "unknown_error"


def validate_authoring_template(
    template: TaskTemplate,
    *,
    title: str | None = None,
    unsafe: bool = False,
) -> dict[str, Any]:
    """Validate a freshly built task template before any tasks insert.

    ``HERMES_AUTHORING_LINT=warn`` is the rollout rollback flag: diagnostics
    are still produced but invalid templates do not block insertion. ``unsafe``
    is reserved for explicit manual CLI operations.
    """

    if unsafe:
        return {
            "schema": "kanban.authoring_lint.v1",
            "ok": True,
            "mode": authoring_lint_mode(),
            "bypassed": True,
            "errors": [],
            "warnings": [],
        }

    from hermes_cli.cli.kanban_scope_lint import TaskSpec, validate_task_spec

    payload = validate_task_spec(
        TaskSpec(
            source="authoring",
            task_id=None,
            title=title,
            body=template.body_template,
            assignee=template.assignee,
            skills=template.skills,
        )
    )
    payload["mode"] = authoring_lint_mode()
    payload["bypassed"] = False
    if payload.get("ok") or payload["mode"] == "warn":
        return payload
    raise AuthoringLintError(payload)


def lint_raw_create_route(
    *,
    title: str | None,
    body: str | None,
    assignee: str | None,
    skills: list[str] | None,
    triage: bool = False,
    unsafe: bool = False,
) -> RawCreateLintRoute:
    """Lint non-template creates and route unsafe runnable work to triage.

    Raw creates are still allowed for quick capture, but an assigned task without
    a valid scope contract must not become immediately dispatchable by default.
    Invalid raw specs are parked in ``triage`` for specification instead of
    entering ``ready``. ``HERMES_AUTHORING_LINT=warn`` and explicit ``unsafe``
    preserve the rollback/manual-bypass behavior used by the template gate.
    """

    from hermes_cli.cli.kanban_scope_lint import TaskSpec, validate_task_spec

    payload = validate_task_spec(
        TaskSpec(
            source="authoring_raw",
            task_id=None,
            title=title,
            body=body or "",
            assignee=assignee,
            skills=skills or [],
        )
    )
    payload["mode"] = authoring_lint_mode()
    payload["bypassed"] = bool(unsafe)
    should_route = (
        not payload.get("ok")
        and not triage
        and bool(assignee)
        and not unsafe
        and payload["mode"] != "warn"
    )
    return RawCreateLintRoute(
        lint_payload=payload,
        triage=True if should_route else triage,
        routed_to_triage=should_route,
        reason="raw_authoring_lint_failed" if should_route else None,
    )
