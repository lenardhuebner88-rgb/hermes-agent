#!/usr/bin/env python3
"""Replace Tailwind Play CDN script with generated CSS for Design Board mockups."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hermes_cli.design_board_tailwind import inline_tailwind_cdn_mockup_html


def make_self_contained(input_path: Path, output_path: Path) -> None:
    html = input_path.read_text(encoding="utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(inline_tailwind_cdn_mockup_html(html), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    args = parser.parse_args(argv)
    make_self_contained(Path(args.input), Path(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
