"""``hermes plan`` PlanSpec subcommands."""

from __future__ import annotations

import argparse
import json
import sys

from hermes_cli import planspecs


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
    validate.add_argument("--json", action="store_true", help="Emit JSON output")

    prompt = sub.add_parser("sprint-prompt", help="Generate a copy-paste sprint prompt from a PlanSpec")
    prompt.add_argument("path", help="Path to a Vault PlanSpec markdown file")
    prompt.add_argument("--json", action="store_true", help="Emit JSON output")

    list_parser = sub.add_parser("list", aliases=["ls"], help="List Vault PlanSpecs visible to Hermes")
    list_parser.add_argument("--json", action="store_true", help="Emit JSON output")
    list_parser.add_argument("--all", action="store_true", help="Include closed, invalid, and diagnostic PlanSpecs")


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
        if action == "ingest":
            result = planspecs.ingest_planspec(
                args.path,
                board=args.board,
                author=args.author,
                force=getattr(args, "force", False),
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
                print(
                    f"Ingested {result['path']} → root {result['root_task_id']} "
                    f"with {len(result['child_ids'])} scheduled children{supersede_note}"
                )
            return 0
        if action == "validate":
            result = planspecs.validate_planspec(args.path)
            if getattr(args, "json", False):
                print(json.dumps(result, ensure_ascii=False))
            else:
                disposition = result["disposition"]
                findings = result.get("findings") or []
                if disposition == "clean":
                    print(
                        f"plan validate: CLEAN {result['path']} · signed={result['signed']} "
                        f"· no rubric findings (deterministic preview; judge not run)"
                    )
                elif disposition == "warn":
                    print(
                        f"plan validate: WARN {result['path']} · operator-signed "
                        f"(approved_by={result['approved_by']}) → would ingest with warnings:"
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
