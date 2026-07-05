"""Focused tests for the ``hermes_cli.kanban`` module."""

from __future__ import annotations

import argparse

from hermes_cli import kanban as kc


def test_respec_parser_accepts_body_file_and_title():
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    args = parser.parse_args(
        ["kanban", "respec", "t_old", "--body-file", "new.md", "--title", "New"]
    )

    assert args.kanban_action == "respec"
    assert args.task_id == "t_old"
    assert args.body_file == "new.md"
    assert args.title == "New"
