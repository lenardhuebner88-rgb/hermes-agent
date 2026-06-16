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

    prompt = sub.add_parser("sprint-prompt", help="Generate a copy-paste sprint prompt from a PlanSpec")
    prompt.add_argument("path", help="Path to a Vault PlanSpec markdown file")
    prompt.add_argument("--json", action="store_true", help="Emit JSON output")

    list_parser = sub.add_parser("list", aliases=["ls"], help="List Vault PlanSpecs visible to Hermes")
    list_parser.add_argument("--json", action="store_true", help="Emit JSON output")


def plan_command(args: argparse.Namespace) -> int:
    action = getattr(args, "plan_action", None)
    if not action:
        parser = getattr(args, "_plan_parser", None)
        if parser is not None:
            parser.print_help()
        return 0
    try:
        if action in ("list", "ls"):
            records = planspecs.list_planspecs()
            if getattr(args, "json", False):
                print(json.dumps({"planspecs": records}, ensure_ascii=False))
            else:
                for item in records:
                    marker = "OK" if item["valid"] else "BLOCKED"
                    print(f"{marker} {item['path']} · {item['freigabe'] or '-'} · {item['subtask_count']} subtasks")
            return 0
        if action == "ingest":
            result = planspecs.ingest_planspec(args.path, board=args.board, author=args.author)
            if getattr(args, "json", False):
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(
                    f"Ingested {result['path']} → root {result['root_task_id']} "
                    f"with {len(result['child_ids'])} scheduled children"
                )
            return 0
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
