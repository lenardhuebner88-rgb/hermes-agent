"""``hermes plan`` PlanSpec subcommands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hermes_cli import planspecs
from hermes_cli.plan_prose import compile_prose_plan, parse_prose_plan


def _has_binding_taskgraph_hints(path: str) -> bool:
    text = Path(path).read_text(encoding="utf-8")
    return text.lstrip().startswith("---") and "\ntaskgraph_hints:" in text


def build_plan_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "plan",
        help="Ingest Vault PlanSpecs into the kanban board",
        description="Validate binding taskgraph_hints and turn PlanSpecs into held Kanban chains.",
    )
    parser.set_defaults(func=plan_command, _plan_parser=parser)
    sub = parser.add_subparsers(dest="plan_action")

    ingest = sub.add_parser("ingest", help="Create a held Kanban chain from a binding PlanSpec")
    ingest.add_argument("path", help="Path to a Vault PlanSpec markdown file")
    ingest.add_argument("--board", default=None, help="Kanban board slug (defaults to current board)")
    ingest.add_argument("--author", default="planspec-ingest", help="Audit author for created tasks")
    ingest.add_argument("--json", action="store_true", help="Emit JSON output")
    ingest.add_argument(
        "--force",
        action="store_true",
        help="Bypass the deterministic spec rubric (logs a WARNING with the skipped reasons)",
    )
    ingest.add_argument(
        "--supersede",
        action="store_true",
        help=(
            "When the PlanSpec changed since its last ingest, archive the stale "
            "chain and ingest the new version (refused if the stale chain still "
            "has running children)"
        ),
    )

    compile_parser = sub.add_parser("compile", help="Preview deterministic children from a prose Plan")
    compile_parser.add_argument("path", help="Path to a prose Plan markdown file")
    compile_parser.add_argument("--json", action="store_true", help="Emit JSON output")

    validate = sub.add_parser(
        "validate",
        help="Validate a PlanSpec read-only (no DB write): preview rubric findings + signed status",
        description=(
            "Dry-run a PlanSpec: report whether an ingest (without --force) would "
            "be clean / warn (operator-signed) / block (unsigned) / invalid "
            "(structural or YAML error). Creates nothing, opens no DB connection."
        ),
    )
    validate.add_argument("path", help="Path to a Vault PlanSpec markdown file")
    validate.add_argument(
        "--board",
        default=None,
        help="Kanban board slug (overrides PlanSpec frontmatter)",
    )
    validate.add_argument("--json", action="store_true", help="Emit JSON output")

    prompt = sub.add_parser("sprint-prompt", help="Generate a copy-paste sprint prompt from a PlanSpec")
    prompt.add_argument("path", help="Path to a Vault PlanSpec markdown file")
    prompt.add_argument("--json", action="store_true", help="Emit JSON output")

    list_parser = sub.add_parser("list", aliases=["ls"], help="List Vault PlanSpecs visible to Hermes")
    list_parser.add_argument("--json", action="store_true", help="Emit JSON output")
    list_parser.add_argument("--all", action="store_true", help="Include closed, invalid, and diagnostic PlanSpecs")

    shipped = sub.add_parser("shipped", help="Mark a completed PlanSpec as shipped")
    shipped.add_argument("path", help="PlanSpec path or filename")
    shipped.add_argument("--author", default="cli", help="closed_by value (default: cli)")
    shipped.add_argument(
        "--kanban-state",
        help="Terminal Kanban root state evidence (e.g. completed or archived)",
    )
    shipped.add_argument("--receipt", help="Receipt path/URL proving the shipped closeout")
    shipped.add_argument("--release-evidence", help="Release or deployment evidence")
    shipped.add_argument("--kanban-root-task-id", help="Root Kanban task id to persist")
    shipped.add_argument("--json", action="store_true", help="Emit JSON output")


def plan_command(args: argparse.Namespace) -> int:
    action = getattr(args, "plan_action", None)
    if not action:
        parser = getattr(args, "_plan_parser", None)
        if parser is not None:
            parser.print_help()
        return 0
    try:
        if action in ("list", "ls"):
            records = planspecs.list_planspecs(scope="all" if getattr(args, "all", False) else "open")
            if getattr(args, "json", False):
                print(json.dumps({"planspecs": records}, ensure_ascii=False))
            else:
                for item in records:
                    marker = "OK" if item["valid"] else "BLOCKED"
                    suffix = f" · {item['closed_reason']}" if item.get("closed_reason") else ""
                    print(f"{marker} {item['path']} · {item['freigabe'] or '-'} · {item['subtask_count']} subtasks{suffix}")
            return 0
        if action == "compile":
            text = Path(args.path).read_text(encoding="utf-8")
            result = compile_prose_plan(parse_prose_plan(text))
            payload = {
                "ok": True,
                "children": result.children,
                "repairs": result.repairs,
                "warnings": result.warnings,
            }
            if getattr(args, "json", False):
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print("Proposed children:")
                for index, child in enumerate(result.children):
                    parents = ", ".join(str(item) for item in child.get("parents") or [])
                    assignee = child.get("assignee") or "-"
                    print(f"- [{index}] {child.get('title')} · assignee={assignee} · parents=[{parents}]")
                print("repairs:")
                for repair in result.repairs or ["(none)"]:
                    print(f"- {repair}")
                print("warnings:")
                for warning in result.warnings or ["(none)"]:
                    print(f"- {warning}")
            return 0
        if action == "ingest":
            if _has_binding_taskgraph_hints(args.path):
                result = planspecs.ingest_planspec(
                    args.path,
                    board=args.board,
                    author=args.author,
                    force=getattr(args, "force", False),
                    supersede=getattr(args, "supersede", False),
                )
            else:
                result = planspecs.ingest_prose_plan(
                    args.path,
                    board=args.board,
                    author=args.author,
                    supersede=getattr(args, "supersede", False),
                )
            if getattr(args, "json", False):
                print(json.dumps(result, ensure_ascii=False))
            elif result.get("already_ingested"):
                print(
                    f"Already ingested {result['path']} → existing root "
                    f"{result['root_task_id']} with {len(result['child_ids'])} subtasks "
                    f"(no new chain created)"
                )
            else:
                superseded = result.get("superseded") or []
                supersede_note = (
                    f" (superseded {len(superseded)} stale chain: {', '.join(superseded)})"
                    if superseded
                    else ""
                )
                child_status = result.get("initial_child_status") or "scheduled"
                child_note = (
                    "scheduled children (held for operator)"
                    if child_status == "scheduled"
                    else "dispatchable children"
                )
                print(
                    f"Ingested {result['path']} → root {result['root_task_id']} "
                    f"with {len(result['child_ids'])} {child_note}{supersede_note}"
                )
            return 0
        if action == "validate":
            result = planspecs.validate_planspec(
                args.path, board=getattr(args, "board", None)
            )
            if getattr(args, "json", False):
                print(json.dumps(result, ensure_ascii=False))
            else:
                disposition = result["disposition"]
                findings = result.get("findings") or []
                if disposition == "clean":
                    print(
                        f"plan validate: CLEAN {result['path']} · signed={result['signed']} "
                        f"· board={result['board']} · no rubric findings "
                        f"(deterministic preview; judge not run)"
                    )
                elif disposition == "warn":
                    print(
                        f"plan validate: WARN {result['path']} · operator-signed "
                        f"(approved_by={result['approved_by']}) · board={result['board']} "
                        "→ would ingest with warnings:"
                    )
                    for finding in findings:
                        print(f"- {finding}")
                elif disposition == "block":
                    print(f"plan validate: BLOCK {result['path']} · unsigned → ingest would block:", file=sys.stderr)
                    for finding in findings:
                        print(f"- {finding}", file=sys.stderr)
                else:  # invalid
                    print(f"plan validate: INVALID {result['path']} · structural / YAML error:", file=sys.stderr)
                    for finding in findings:
                        print(f"- {finding}", file=sys.stderr)
            return 0 if result["ok"] else 2
        if action == "sprint-prompt":
            result = planspecs.sprint_prompt_for_planspec(args.path)
            if getattr(args, "json", False):
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(result["prompt"])
            return 0
        if action == "shipped":
            result = planspecs.mark_planspec_shipped(
                args.path,
                plans_root=getattr(args, "plans_root", planspecs.DEFAULT_PLANS_ROOT),
                author=args.author,
                kanban_state=getattr(args, "kanban_state", None),
                receipt=getattr(args, "receipt", None),
                release_evidence=getattr(args, "release_evidence", None),
                kanban_root_task_id=getattr(args, "kanban_root_task_id", None),
            )
            if getattr(args, "json", False):
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(f"marked shipped: {result['path']}")
            return 0
    except planspecs.PlanSpecBlocked as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "findings": exc.findings}, ensure_ascii=False))
        else:
            print("plan: BLOCKED", file=sys.stderr)
            for finding in exc.findings:
                print(f"- {finding}", file=sys.stderr)
        return 2
    print(f"plan: unknown action {action!r}", file=sys.stderr)
    return 2
