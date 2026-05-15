"""Strict Contract-to-Taskgraph compiler v1.1.

Compiles a Vault Markdown plan with YAML frontmatter into a normalized
``contract.yaml``, a clearly non-binding ``taskgraph.draft.yaml``, and a
human receipt. ``taskgraph_hints`` remain optional planning hints only; the
emitted taskgraph draft is never an execution contract.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

DEFAULT_TEMPLATES_ROOT = Path("/home/piet/vault/03-Agents/Hermes/plans/templates")
DEFAULT_COMPILED_ROOT = Path("/home/piet/vault/03-Agents/Hermes/plans/compiled")
_REQUIRED_MARKDOWN_SECTIONS = ("## Goal", "## Acceptance Criteria", "## Anti-Scope", "## Evidence Required")


class TaskgraphHints(BaseModel):
    """Optional, non-binding hints for a later taskgraph draft compiler."""

    candidate_tasks: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    recommended_roles: list[str] = Field(default_factory=list)
    non_binding: bool = True


class PlanContract(BaseModel):
    """Contract schema source of truth for compiler v1."""

    contract_version: Literal[1] = 1
    goal: str
    anti_scope: list[str]
    acceptance_criteria: list[str]
    risk_class: Literal["LOW", "MEDIUM", "HIGH", "CROSS_SYSTEM"]
    evidence_required: list[str]
    next_decision: str
    allowed_actions: list[str]
    forbidden_actions: list[str]
    requires_approval: list[str]
    taskgraph_hints: TaskgraphHints = Field(default_factory=TaskgraphHints)

    @field_validator(
        "goal",
        "next_decision",
    )
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return value.strip()

    @field_validator(
        "anti_scope",
        "acceptance_criteria",
        "evidence_required",
        "allowed_actions",
        "forbidden_actions",
        "requires_approval",
    )
    @classmethod
    def _non_empty_string_list(cls, value: list[str]) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("must be a non-empty list")
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        if not cleaned:
            raise ValueError("must contain at least one non-empty item")
        return cleaned


class CompileBlocked(RuntimeError):
    def __init__(self, findings: list[str]):
        self.findings = findings
        super().__init__("; ".join(findings))


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug[:80] or "plan"


def _extract_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise CompileBlocked(["missing YAML frontmatter delimited by ---"])
    end = text.find("\n---", 4)
    if end == -1:
        raise CompileBlocked(["unterminated YAML frontmatter; add closing ---"])
    raw = text[4:end]
    body = text[end + len("\n---") :].lstrip("\n")
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise CompileBlocked([f"invalid YAML frontmatter: {exc}"]) from exc
    if not isinstance(data, dict):
        raise CompileBlocked(["frontmatter must be a YAML mapping"])
    return data, body


def _missing_sections(body: str) -> list[str]:
    return [section for section in _REQUIRED_MARKDOWN_SECTIONS if section not in body]


def _as_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _role_hint(roles: list[str], index: int) -> str | None:
    if not roles:
        return None
    if index < len(roles):
        return roles[index]
    return roles[0]


def build_taskgraph_draft(contract: PlanContract) -> dict[str, Any]:
    """Build a non-binding taskgraph draft from optional taskgraph hints."""

    hints = contract.taskgraph_hints
    tasks = []
    for index, title in enumerate(hints.candidate_tasks):
        task: dict[str, Any] = {
            "id": slugify(title),
            "title": title,
        }
        role = _role_hint(hints.recommended_roles, index)
        if role:
            task["role_hint"] = role
        tasks.append(task)

    return {
        "schema_version": "taskgraph.draft.v1.1",
        "non_binding": True,
        "binding": "non-binding",
        "disclaimer": "NON-BINDING DRAFT — planning aid only; not approved for dispatch or execution.",
        "source": "contract.taskgraph_hints",
        "contract_goal": contract.goal,
        "tasks": tasks,
        "dependencies": hints.dependencies,
        "recommended_roles": hints.recommended_roles,
    }


def parse_plan(path: Path) -> PlanContract:
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _extract_frontmatter(text)
    findings: list[str] = []
    for section in _missing_sections(body):
        findings.append(f"missing required markdown section: {section}")

    payload = {
        "contract_version": frontmatter.get("contract_version", 1),
        "goal": frontmatter.get("goal") or frontmatter.get("title") or "",
        "anti_scope": _as_list(frontmatter, "anti_scope"),
        "acceptance_criteria": _as_list(frontmatter, "acceptance_criteria"),
        "risk_class": str(frontmatter.get("risk_class", "")).upper(),
        "evidence_required": _as_list(frontmatter, "evidence_required"),
        "next_decision": frontmatter.get("next_decision") or "",
        "allowed_actions": _as_list(frontmatter, "allowed_actions"),
        "forbidden_actions": _as_list(frontmatter, "forbidden_actions"),
        "requires_approval": _as_list(frontmatter, "requires_approval"),
        "taskgraph_hints": frontmatter.get("taskgraph_hints") or {},
    }
    try:
        contract = PlanContract.model_validate(payload)
    except ValidationError as exc:
        for error in exc.errors():
            loc = ".".join(str(part) for part in error.get("loc", ()))
            findings.append(f"{loc}: {error.get('msg')}")
        raise CompileBlocked(findings or ["contract validation failed"]) from exc
    if findings:
        raise CompileBlocked(findings)
    return contract


def patch_suggestion(findings: list[str]) -> str:
    missing = "\n".join(f"- {finding}" for finding in findings)
    return (
        "BLOCKED: plan does not satisfy contract v1. Patch the plan frontmatter/sections:\n"
        f"{missing}\n\n"
        "Required frontmatter keys: contract_version, goal, anti_scope, "
        "acceptance_criteria, risk_class, evidence_required, next_decision, "
        "allowed_actions, forbidden_actions, requires_approval.\n"
        "Required sections: ## Goal, ## Acceptance Criteria, ## Anti-Scope, ## Evidence Required."
    )


def export_schema(templates_root: Path) -> Path:
    templates_root.mkdir(parents=True, exist_ok=True)
    schema_path = templates_root / "contract.schema.json"
    schema_path.write_text(
        json.dumps(PlanContract.model_json_schema(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return schema_path


def compile_plan(path: Path, *, compiled_root: Path = DEFAULT_COMPILED_ROOT, templates_root: Path = DEFAULT_TEMPLATES_ROOT) -> dict[str, Path]:
    contract = parse_plan(path)
    slug = slugify(path.stem)
    out_dir = compiled_root / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    source_path = out_dir / "source.md"
    contract_path = out_dir / "contract.yaml"
    taskgraph_draft_path = out_dir / "taskgraph.draft.yaml"
    receipt_path = out_dir / "contract.receipt.md"
    schema_path = export_schema(templates_root)

    shutil.copyfile(path, source_path)
    contract_path.write_text(
        yaml.safe_dump(contract.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    taskgraph_draft_path.write_text(
        yaml.safe_dump(build_taskgraph_draft(contract), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    receipt_path.write_text(
        "\n".join(
            [
                "---",
                "status: GREEN",
                f"created: {_utc_now()}",
                "compiler: hermes-plan-compile v1.1",
                "---",
                "",
                "# Contract Compile Receipt",
                "",
                f"Result: GREEN — compiled `{path}` into `{out_dir}`.",
                "",
                "## Artifacts",
                f"- source: `{source_path}`",
                f"- contract: `{contract_path}`",
                f"- taskgraph draft: `{taskgraph_draft_path}` (NON-BINDING)",
                f"- schema: `{schema_path}`",
                "",
                "## Non-binding Taskgraph Hints",
                "`taskgraph_hints` are optional preparation only; `taskgraph.draft.yaml` is a non-binding planning aid and must not be dispatched.",
                "",
                "## Next Decision",
                contract.next_decision,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {"source": source_path, "contract": contract_path, "taskgraph_draft": taskgraph_draft_path, "receipt": receipt_path, "schema": schema_path}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-plan-compile", description="Compile a strict Vault Markdown plan into a validated contract.")
    parser.add_argument("plan", type=Path, help="Path to plan markdown with YAML frontmatter")
    parser.add_argument("--compiled-root", type=Path, default=DEFAULT_COMPILED_ROOT)
    parser.add_argument("--templates-root", type=Path, default=DEFAULT_TEMPLATES_ROOT)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable result")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        artifacts = compile_plan(args.plan, compiled_root=args.compiled_root, templates_root=args.templates_root)
    except CompileBlocked as exc:
        message = patch_suggestion(exc.findings)
        if args.json:
            print(json.dumps({"status": "BLOCKED", "findings": exc.findings, "patch_suggestion": message}, ensure_ascii=False))
        else:
            print(message, file=sys.stderr)
        return 2
    except Exception as exc:
        if args.json:
            print(json.dumps({"status": "BLOCKED", "findings": [str(exc)]}, ensure_ascii=False))
        else:
            print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2

    payload = {"status": "GREEN", "artifacts": {key: str(value) for key, value in artifacts.items()}}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"GREEN: contract compiled to {artifacts['contract']}")
        print(f"Receipt: {artifacts['receipt']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
