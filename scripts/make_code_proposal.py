#!/usr/bin/env python3
"""Author a ``mode='code'`` Autoresearch proposal from a concrete file rewrite.

This is the minimal code-proposal generator (A3): an agent or the operator
supplies the full new text for one repo file; it lands as a previewable,
test-gated proposal that the operator reviews and one-click-applies in the
Hermes Control dashboard. The apply runs the full test suite and keeps only on
green — so authoring a proposal here is safe by construction.

Examples:
    # New content from a file you prepared:
    scripts/make_code_proposal.py \
        --target agent/foo.py \
        --after-file /tmp/foo_new.py \
        --title "foo: handle empty input" \
        --rationale "Crashes on empty list; guard added."

    # New content from stdin:
    cat new_foo.py | scripts/make_code_proposal.py --target agent/foo.py \
        --title "..." --rationale "..." --after-file -
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hermes_cli import autoresearch_proposals as proposals  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Author a code-mode Autoresearch proposal.")
    parser.add_argument("--target", required=True,
                        help="Path to the repo file the proposal rewrites.")
    parser.add_argument("--after-file", required=True,
                        help="Path to a file holding the full NEW content (use '-' for stdin).")
    parser.add_argument("--title", required=True, help="Short plain-language title.")
    parser.add_argument("--rationale", required=True,
                        help="Why this change — shown as the card's 'Warum'.")
    parser.add_argument("--id", default=None, help="Explicit proposal id (default: derived).")
    parser.add_argument("--section", default=None, help="Optional section/subtitle label.")
    args = parser.parse_args(argv)

    if args.after_file == "-":
        after_text = sys.stdin.read()
    else:
        after_text = Path(args.after_file).read_text(encoding="utf-8")

    target = Path(args.target)
    if not target.is_absolute():
        target = (_REPO / target).resolve()
    if not target.exists():
        print(f"warning: target does not exist yet: {target}", file=sys.stderr)

    proposal = proposals.build_code_proposal(
        target, after_text,
        title=args.title, rationale=args.rationale,
        pid=args.id, section=args.section,
    )
    diff_lines = (proposal["diff_before_after"] or "").splitlines()
    print(f"created code proposal: {proposal['id']}")
    print(f"  target: {proposal['target']}")
    print(f"  diff:   {len(diff_lines)} lines")
    print("Review and apply it in Hermes Control → Autoresearch (full test-suite gate).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
