#!/usr/bin/env python3
"""Read-only evaluator for local Hermes SKILL.md files.

No external packages. Reports structural errors and warnings. Exits 1 only for
real structure problems such as unreadable, empty, or malformed frontmatter.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

SKILLS_ROOT = Path(os.environ.get("HERMES_SKILLS_ROOT", str(Path.home() / ".hermes" / "skills"))).expanduser()
SECTION_GROUPS = [
    ("When to Use / Wann verwenden", ("when to use", "wann verwenden", "trigger", "aktivierung", "use when")),
    ("Safety / Sicherheit", ("safety", "sicherheit", "stop", "forbidden", "niemals", "never", "approval", "secret", "credential")),
    ("Procedure / Vorgehen", ("procedure", "vorgehen", "workflow", "steps", "schritte", "prozess")),
    ("Output / Ergebnis", ("output", "ergebnis", "deliverable", "report", "format", "contract", "vertrag")),
]
FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*\n", re.S)
FIELD_RE = re.compile(r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(?P<value>.*)$")


def find_skills(root: Path) -> list[Path]:
    if not root.exists():
        return []
    ignored = {".git", ".venv", "node_modules", "__pycache__", ".next", "dist", "build"}
    out: list[Path] = []
    for path in root.rglob("SKILL.md"):
        if any(part in ignored for part in path.parts):
            continue
        out.append(path)
    return sorted(out)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str | None]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, None
    fields: dict[str, str] = {}
    for line in match.group("body").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parsed = FIELD_RE.match(line)
        if parsed:
            fields[parsed.group("key")] = parsed.group("value").strip().strip("'\"")
    return fields, match.group("body")


def check_skill(path: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [f"unreadable: {exc}"], warnings
    if not raw.strip():
        return ["empty file"], warnings
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        errors.append(f"not valid UTF-8: {exc}")
        text = raw.decode("utf-8", errors="replace")
    if "\x00" in text:
        errors.append("contains NUL bytes")

    fields, frontmatter = parse_frontmatter(text)
    if text.startswith("---") and frontmatter is None:
        errors.append("frontmatter starts but does not close with ---")
    elif not text.startswith("---"):
        warnings.append("frontmatter missing")
    else:
        for key in ("name", "description"):
            if not fields.get(key):
                errors.append(f"frontmatter missing {key}")
        if fields.get("description") and len(fields["description"]) > 1024:
            errors.append("description longer than 1024 chars")

    low = text.lower()
    for label, needles in SECTION_GROUPS:
        if not any(n in low for n in needles):
            warnings.append(f"recommended section missing: {label}")
    if len(text.strip()) < 80:
        warnings.append("very short skill body")
    return errors, warnings


def main() -> int:
    skills = find_skills(SKILLS_ROOT)
    print("Hermes local skill eval")
    print(f"Root: {SKILLS_ROOT}")
    print(f"SKILL.md files: {len(skills)}")
    if not skills:
        print("ERROR: no SKILL.md files found")
        return 1

    total_errors = 0
    total_warnings = 0
    with_warnings: list[tuple[Path, list[str]]] = []
    with_errors: list[tuple[Path, list[str]]] = []
    missing_by_section = {label: 0 for label, _ in SECTION_GROUPS}

    for path in skills:
        errors, warnings = check_skill(path)
        total_errors += len(errors)
        total_warnings += len(warnings)
        if errors:
            with_errors.append((path, errors))
        if warnings:
            with_warnings.append((path, warnings))
        for warning in warnings:
            for label in missing_by_section:
                if warning.endswith(label):
                    missing_by_section[label] += 1

    print("\nSummary")
    print(f"Errors: {total_errors}")
    print(f"Warnings: {total_warnings}")
    for label, count in missing_by_section.items():
        print(f"Missing {label}: {count}")

    if with_errors:
        print("\nErrors by file")
        for path, errors in with_errors[:50]:
            print(f"- {path}")
            for error in errors:
                print(f"  - {error}")
        if len(with_errors) > 50:
            print(f"... {len(with_errors) - 50} more files with errors")

    if with_warnings:
        print("\nWarnings by file (first 80)")
        for path, warnings in with_warnings[:80]:
            print(f"- {path}")
            for warning in warnings[:6]:
                print(f"  - {warning}")
        if len(with_warnings) > 80:
            print(f"... {len(with_warnings) - 80} more files with warnings")

    if total_errors:
        print("\nRESULT: FAIL structural errors found")
        return 1
    print("\nRESULT: PASS no structural errors; warnings are improvement candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
