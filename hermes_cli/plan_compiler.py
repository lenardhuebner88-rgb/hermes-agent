"""Strict Contract-to-Taskgraph compiler v1.1.

Compiles a Vault Markdown plan with YAML frontmatter into a normalized
``contract.yaml``, a clearly non-binding ``taskgraph.draft.yaml``, and a
human receipt. ``taskgraph_hints`` remain optional planning hints only; the
emitted taskgraph draft is never an execution contract.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

DEFAULT_TEMPLATES_ROOT = Path("/home/piet/vault/03-Agents/Hermes/plans/templates")
DEFAULT_COMPILED_ROOT = Path("/home/piet/vault/03-Agents/Hermes/plans/compiled")
_REQUIRED_MARKDOWN_SECTIONS = ("## Goal", "## Acceptance Criteria", "## Anti-Scope", "## Evidence Required")


class BindingSubtask(BaseModel):
    """Binding taskgraph child from PlanSpec frontmatter.

    ``deps`` are symbolic ids that are resolved to sibling parent indices right
    before inserting into the kanban graph.

    ``acceptance_criteria`` is an optional per-subtask list of criteria ids or
    full AcceptanceCriterion objects.  When absent, the caller falls back to the
    plan-level criteria whose ``applies_to`` includes this subtask's id.
    """

    id: str
    title: str
    lane: str
    deps: list[str] = Field(default_factory=list)
    body: str = ""
    acceptance_criteria: list[Any] = Field(default_factory=list)
    # A1-classaware: opt-in marker for a read-only analysis subtask. Default ""
    # ⇒ the child's kind is lane-derived (the long-standing strict behaviour).
    # Only the explicit value ``analysis`` is honoured as an override; any other
    # value falls back to lane-derivation, so this is strictly additive.
    kind: str = ""

    @field_validator("id", "title", "lane")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return value.strip()

    @field_validator("deps")
    @classmethod
    def _clean_deps(cls, value: list[str]) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("must be a list")
        return [str(item).strip() for item in value if str(item).strip()]

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        """A1-classaware: keep serialized subtasks byte-identical to pre-classaware
        PlanSpecs. The opt-in ``kind`` field is emitted ONLY when a value is set;
        an unset/empty ``kind`` is dropped so unmarked subtasks never gain a new
        ``kind: ''`` key in ``taskgraph.draft.yaml`` / ``contract.yaml`` (and stay
        byte-identical to main). ``kind`` is declared last, so popping it preserves
        the original field order of the remaining keys."""
        data = handler(self)
        if not str(data.get("kind") or "").strip():
            data.pop("kind", None)
        return data


class TaskgraphHints(BaseModel):
    """Optional hints for taskgraph draft or binding PlanSpec ingestion."""

    candidate_tasks: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    recommended_roles: list[str] = Field(default_factory=list)
    non_binding: bool = True
    binding: bool = False
    subtasks: list[BindingSubtask] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_binding_graph(self) -> "TaskgraphHints":
        if self.subtasks and not self.binding:
            raise ValueError("taskgraph_hints.subtasks requires taskgraph_hints.binding: true")
        if self.binding and not self.subtasks:
            raise ValueError("taskgraph_hints.binding=true requires at least one subtask")
        ids = [task.id for task in self.subtasks]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"duplicate taskgraph_hints.subtasks id(s): {', '.join(duplicates)}")
        known = set(ids)
        for task in self.subtasks:
            unknown = [dep for dep in task.deps if dep not in known]
            if unknown:
                raise ValueError(
                    f"taskgraph_hints.subtasks[{task.id}].deps unknown id(s): {', '.join(unknown)}"
                )
            if task.id in task.deps:
                raise ValueError(f"taskgraph_hints.subtasks[{task.id}] cannot depend on itself")
        return self


class AcceptanceCriterion(BaseModel):
    """Structured PlanSpec acceptance criterion.

    Legacy v1 plans may still provide free-form strings. Dict items are treated
    as PlanSpec-style criteria and must carry operator-verifiable evidence
    fields so downstream workers/reviewers can prove the done signal.
    """

    id: str
    scope_level: Literal["plan", "child", "review", "receipt"]
    statement: str
    verification: str
    done_signal: str
    owner: str = "coder"
    applies_to: list[str] = Field(default_factory=list)
    required: bool = True

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        value = str(value).strip()
        if not value:
            raise ValueError("must be a non-empty string")
        if any(char.isspace() for char in value):
            raise ValueError("must not contain whitespace")
        return value

    @field_validator("statement", "verification", "done_signal", "owner")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return value.strip()

    @field_validator("applies_to")
    @classmethod
    def _clean_applies_to(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]


class PlanContract(BaseModel):
    """Contract schema source of truth for compiler v1."""

    contract_version: Literal[1] = 1
    goal: str
    anti_scope: list[str]
    acceptance_criteria: list[str | AcceptanceCriterion]
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

    @field_validator("acceptance_criteria")
    @classmethod
    def _non_empty_acceptance_criteria(cls, value: list[str | AcceptanceCriterion]) -> list[str | AcceptanceCriterion]:
        if not isinstance(value, list) or not value:
            raise ValueError("must be a non-empty list")
        cleaned: list[str | AcceptanceCriterion] = []
        for item in value:
            if isinstance(item, AcceptanceCriterion):
                cleaned.append(item)
            else:
                text = str(item).strip()
                if text:
                    cleaned.append(text)
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


def _as_acceptance_criteria(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    return []


def _normalize_acceptance_criteria(raw_items: list[Any], findings: list[str]) -> list[str | AcceptanceCriterion]:
    normalized: list[str | AcceptanceCriterion] = []
    seen_ids: set[str] = set()

    for index, item in enumerate(raw_items):
        if isinstance(item, str):
            text = item.strip()
            if text:
                normalized.append(text)
            continue

        if not isinstance(item, dict):
            findings.append(f"acceptance_criteria.{index}: must be a string or mapping")
            continue

        try:
            criterion = AcceptanceCriterion.model_validate(item)
        except ValidationError as exc:
            for error in exc.errors():
                loc_parts = ["acceptance_criteria", str(index), *(str(part) for part in error.get("loc", ()))]
                findings.append(f"{'.'.join(loc_parts)}: {error.get('msg')}")
            continue

        if criterion.id in seen_ids:
            findings.append(f"duplicate acceptance_criteria id: {criterion.id}")
            continue
        seen_ids.add(criterion.id)
        normalized.append(criterion)

    return normalized


def _role_hint(roles: list[str], index: int) -> str | None:
    if not roles:
        return None
    if index < len(roles):
        return roles[index]
    return roles[0]


# A1-classaware: the explicit read-only task class. Kept as a local literal
# (not imported from kanban_db) to avoid a circular import — kanban_db imports
# taskgraph_hints_to_children from here. Must match kanban_db._VERIFIER_ANALYSIS_CLASS.
_VERIFIER_ANALYSIS_CLASS = "analysis"


def _kind_for_planspec_lane(lane: str) -> str:
    normalized = (lane or "").strip().lower()
    if normalized in {"reviewer", "critic"}:
        return "review"
    if normalized == "research":
        return "research"
    return "code"


logger = logging.getLogger(__name__)

# Strip a leading "AC"/"AC-"/"AC_" (with optional separators) from an AC id so an
# id that already starts with "AC" (e.g. "AC1-foo") yields a single clean
# "AC-1-foo" token instead of a doubled "AC-AC1-foo". The "AC" is only stripped
# when followed by a separator or digit, so words like "ACCOUNT" are left intact.
_AC_PREFIX_RE = re.compile(r"^ac(?=[-_\s\d])[-_\s]*", re.IGNORECASE)


def _ac_token(ac_id: str, *, fallback: str) -> str:
    """Build a single ``AC-<core>`` token for an AC bullet.

    Guarantees the ``AC-`` marker that ``_parse_acceptance_criteria``
    (kanban_db.py) requires, without doubling it for ids that already start with
    ``AC``. Falls back to *fallback* when stripping leaves nothing.
    """
    core = _AC_PREFIX_RE.sub("", (ac_id or "").strip()).strip()
    return f"AC-{core or fallback}"


def _ac_statement_oneline(text: str) -> str:
    """Collapse whitespace/newlines so a multi-line statement survives the
    single-line bullet round-trip through ``_parse_acceptance_criteria``, which
    only matches the first line of each bullet."""
    return " ".join(str(text).split())


def _ac_items_for_subtask(
    subtask: "BindingSubtask",
    plan_ac: "list[str | AcceptanceCriterion]",
) -> list[str | AcceptanceCriterion]:
    """Return normalized AC items that apply to *subtask*."""
    if subtask.acceptance_criteria:
        findings: list[str] = []
        normalized = _normalize_acceptance_criteria(subtask.acceptance_criteria, findings)
        if findings:
            logger.warning(
                "PlanSpec subtask %s: %d acceptance_criteria dropped: %s",
                subtask.id, len(findings), "; ".join(findings),
            )
        if normalized:
            return normalized
    fallback: list[str | AcceptanceCriterion] = []
    for item in plan_ac:
        if isinstance(item, AcceptanceCriterion):
            if not item.applies_to or subtask.id in item.applies_to:
                fallback.append(item)
        else:
            fallback.append(item)
    return fallback


def _ac_bullets_for_subtask(
    subtask: "BindingSubtask",
    plan_ac: "list[str | AcceptanceCriterion]",
) -> list[str]:
    """Return the AC bullet lines that apply to *subtask*.

    Priority order:
    1. Per-subtask ``acceptance_criteria`` list (id strings or full dicts from
       the subtask block itself).
    2. Fallback: plan-level AC entries whose ``applies_to`` includes this
       subtask's id.

    Each line is formatted as ``- AC-<id>: <statement>`` so that
    ``_parse_acceptance_criteria`` (kanban_db.py) can pick them up via its
    ``\\bAC-\\w+`` bullet regex.
    """
    def _bullets(items: "list[str | AcceptanceCriterion]") -> list[str]:
        out: list[str] = []
        for n, item in enumerate(items, start=1):
            if isinstance(item, str):
                # Free-form string — synthesise a unique AC-<n> token per item so
                # multiple free-form criteria don't collapse onto one shared id.
                out.append(f"- AC-{n}: {_ac_statement_oneline(item)}")
            else:
                out.append(
                    f"- {_ac_token(item.id, fallback=str(n))}: "
                    f"{_ac_statement_oneline(item.statement)}"
                )
        return out

    # 1. Per-subtask AC wins — but only when it yields at least one bullet. If
    #    the subtask carries acceptance_criteria that are ALL invalid (every item
    #    drops to findings), fall through to the plan-level fallback rather than
    #    silently leaving the child with no AC at all.
    if subtask.acceptance_criteria:
        findings: list[str] = []
        normalized = _normalize_acceptance_criteria(subtask.acceptance_criteria, findings)
        if findings:
            logger.warning(
                "PlanSpec subtask %s: %d acceptance_criteria dropped: %s",
                subtask.id, len(findings), "; ".join(findings),
            )
        bullets = _bullets(normalized)
        if bullets:
            return bullets

    # 2. Fallback: plan-level AC that apply to this subtask. An entry with no
    #    explicit applies_to (incl. free-form plan-level strings) is plan-wide
    #    and threads into every subtask rather than being silently dropped.
    fallback: list[str | AcceptanceCriterion] = []
    for item in plan_ac:
        if isinstance(item, AcceptanceCriterion):
            if not item.applies_to or subtask.id in item.applies_to:
                fallback.append(item)
        else:
            fallback.append(item)
    return _bullets(fallback)


def taskgraph_hints_to_children(
    hints: "TaskgraphHints | dict[str, Any]",
    *,
    plan_ac: "list[str | AcceptanceCriterion] | None" = None,
    planspec_source: "str | None" = None,
) -> list[dict[str, Any]]:
    """Translate binding frontmatter hints into kanban ``children``.

    The output shape is accepted by :func:`kanban_db.decompose_triage_task`.
    It is deterministic and LLM-free: dependency ids become parent indices in
    the same order the PlanSpec lists subtasks.

    Args:
        hints: The binding taskgraph hints (as a model or raw dict).
        plan_ac: Plan-level acceptance criteria list (``AcceptanceCriterion``
            or free-form strings).  Used as fallback AC for subtasks that carry
            no per-subtask ``acceptance_criteria``.  Pass ``None`` or ``[]`` to
            disable AC threading (backward-compatible).
        planspec_source: Absolute path of the originating ``.md`` file.
            Stored in each child dict as ``planspec_source`` so
            :func:`kanban_db.decompose_triage_task` can persist it.
    """
    model = hints if isinstance(hints, TaskgraphHints) else TaskgraphHints.model_validate(hints)
    if not model.binding:
        raise CompileBlocked(["taskgraph_hints.binding must be true for ingest"])
    effective_plan_ac: list[str | AcceptanceCriterion] = list(plan_ac) if plan_ac else []
    index_by_id = {task.id: index for index, task in enumerate(model.subtasks)}
    children: list[dict[str, Any]] = []
    for task in model.subtasks:
        deps = [index_by_id[dep] for dep in task.deps]
        body_parts = []
        if task.body.strip():
            body_parts.append(task.body.strip())
        body_parts.append(f"PlanSpec subtask: {task.id}")
        body_parts.append(f"Lane: {task.lane}")
        if task.deps:
            body_parts.append("Depends on: " + ", ".join(task.deps))
        # Thread AC bullets into the body for backwards-readable task bodies,
        # and pass the structured items separately for the DB store.
        ac_items = _ac_items_for_subtask(task, effective_plan_ac)
        ac_bullets = _ac_bullets_for_subtask(task, effective_plan_ac)
        if ac_bullets:
            body_parts.append("\n".join(ac_bullets))
        # A1-classaware: a subtask may opt into the read-only analysis class via
        # ``kind: analysis``; that threads into tasks.kind so the verifier emits
        # its read-only class header. Any other/absent value stays lane-derived
        # (default-strict), so non-analysis plans render byte-identically.
        child_kind = (
            _VERIFIER_ANALYSIS_CLASS
            if (task.kind or "").strip().lower() == _VERIFIER_ANALYSIS_CLASS
            else _kind_for_planspec_lane(task.lane)
        )
        child: dict[str, Any] = {
            "title": task.title,
            "body": "\n\n".join(body_parts),
            "assignee": task.lane,
            "kind": child_kind,
            "parents": deps,
            "planspec_lane": task.lane,
            "planspec_deps": list(task.deps),
            "planspec_subtask_id": task.id,
        }
        if ac_items:
            child["acceptance_criteria_struct"] = [
                item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else {"statement": str(item)}
                for item in ac_items
            ]
        if planspec_source is not None:
            child["planspec_source"] = planspec_source
        children.append(child)
    return children


def build_taskgraph_draft(contract: PlanContract) -> dict[str, Any]:
    """Build a non-binding taskgraph draft from optional taskgraph hints."""

    hints = contract.taskgraph_hints
    if hints.binding:
        children = taskgraph_hints_to_children(hints)
        return {
            "schema_version": "taskgraph.binding.v1",
            "non_binding": False,
            "binding": True,
            "source": "contract.taskgraph_hints",
            "contract_goal": contract.goal,
            "children": children,
            "subtasks": [task.model_dump(mode="json") for task in hints.subtasks],
        }

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
        "acceptance_criteria": _normalize_acceptance_criteria(
            _as_acceptance_criteria(frontmatter, "acceptance_criteria"),
            findings,
        ),
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
