"""Agent-facing CLI for the Design Board. Direct disk + kanban write; no HTTP."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

from hermes_cli import design_board_store as store
from hermes_cli.design_board_tailwind import inline_tailwind_cdn_mockup_html
from hermes_cli import kanban_db

_CHROMIUM_SHOT = os.path.expanduser("~/bin/chromium-shot")


def build_brief(card: dict) -> str:
    lines = [f"# Design Board card: {card['title']} ({card['kind']})"]
    target = card.get("target") or {}
    if target.get("view"):
        lines.append(f"Target view: {target['view']}")
    for entry in card.get("entries", []):
        if entry.get("note"):
            lines.append(f"- {entry['author']}: {entry['note']}")
        if entry.get("asset"):
            lines.append(f"  asset: {entry['asset']}")
        for pin in entry.get("pins", []):
            lines.append(
                f"  pin {pin['id']} @ ({pin['x']},{pin['y']}): {pin.get('note', '')}"
            )
    lines.append(
        "\n## Design-DoD (acceptance gates for UI changes from this card)\n"
        "- Dark skin, theme tokens only (web/src/control/theme.css) — no raw hex (gate ratchet enforces)\n"
        "- Verified at mobile 390px AND desktop 1440px viewport\n"
        "- States handled and shown: empty, loading, overflow (long words, 3x item count)\n"
        "- Text contrast readable on dark surfaces\n"
        "- Evidence: after-screenshot via scripts/control_shot.py attached back to this card\n"
        "  (see ~/.hermes/skills/design-board/SKILL.md)"
    )
    lines.append("\n(Assets on disk under ~/.hermes/design-board/cards/<id>/assets/)")
    return "\n".join(lines)


def promote(card_id: str, *, assignee: str | None = None) -> str:
    card = store.get_card(card_id)
    if card is None:
        raise KeyError(card_id)
    brief = build_brief(card)
    with kanban_db.connect_closing() as conn:
        task_id = kanban_db.create_task(
            conn, title=card["title"], body=brief, assignee=assignee,
            created_by="design-board",
            idempotency_key=f"design-board:{card_id}",
        )
    store.link_task(card_id, task_id)
    return task_id


def render_html_to_png(html_path: str, png_path: str, *, width: int = 1280,
                       height: int = 900) -> None:
    cmd = [_CHROMIUM_SHOT, f"--screenshot={png_path}",
           f"--window-size={width},{height}", f"file://{os.path.abspath(html_path)}"]
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    if result.returncode != 0 or not os.path.isfile(png_path):
        raise RuntimeError(f"chromium render failed: {result.stderr[:500]!r}")


def add_mockup(card_id: str, html_file: str, *, note: str = "") -> str:
    """Write an HTML mockup asset, render a sibling PNG, add a mockup_html entry."""
    if store.get_card(card_id) is None:
        raise KeyError(card_id)
    with open(html_file, "rb") as fh:
        html_bytes = fh.read()
    html_text = html_bytes.decode("utf-8")
    html_bytes = inline_tailwind_cdn_mockup_html(html_text).encode("utf-8")
    html_name = store.write_asset(card_id, os.path.basename(html_file), html_bytes)
    png_stem = html_name.rsplit(".", 1)[0] or html_name
    with tempfile.TemporaryDirectory() as td:
        html_path = os.path.join(td, "mockup.html")
        with open(html_path, "wb") as fh:
            fh.write(html_bytes)
        png_path = os.path.join(td, "mockup.png")
        render_html_to_png(html_path, png_path)
        with open(png_path, "rb") as fh:
            png_bytes = fh.read()
    png_name = store.write_asset(card_id, f"{png_stem}.png", png_bytes)
    return store.add_entry(
        card_id, author="claude", kind="mockup_html", note=note,
        asset_name=png_name, html_name=html_name,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="design-board")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    s_show = sub.add_parser("show"); s_show.add_argument("card_id")
    s_pr = sub.add_parser("promote")
    s_pr.add_argument("card_id"); s_pr.add_argument("--assignee")
    s_ae = sub.add_parser("add-entry")
    s_ae.add_argument("card_id"); s_ae.add_argument("--author", default="claude")
    s_ae.add_argument("--kind", default="mockup_png"); s_ae.add_argument("--note", default="")
    s_ae.add_argument("--asset-name"); s_ae.add_argument("--html-name")
    s_mk = sub.add_parser("add-mockup")
    s_mk.add_argument("card_id"); s_mk.add_argument("--html-file", required=True)
    s_mk.add_argument("--note", default="")
    args = p.parse_args(argv)

    if args.cmd == "list":
        print(json.dumps(store.list_cards(), indent=2))
    elif args.cmd == "show":
        print(json.dumps(store.get_card(args.card_id), indent=2))
    elif args.cmd == "promote":
        print(promote(args.card_id, assignee=args.assignee))
    elif args.cmd == "add-entry":
        print(store.add_entry(args.card_id, author=args.author, kind=args.kind,
                              note=args.note, asset_name=args.asset_name,
                              html_name=args.html_name))
    elif args.cmd == "add-mockup":
        print(add_mockup(args.card_id, args.html_file, note=args.note))
    return 0


if __name__ == "__main__":
    sys.exit(main())
