#!/usr/bin/env python3
"""Mechanical hygiene gate for skills and loop-pack prompts.

Checks ``.claude/skills/**/SKILL.md`` and ``loops/packs/**/*PROMPT.md`` for:

1. Hardcoded release-venv interpreter used as a pytest runner
2. pytest ``--timeout`` flags (pytest-timeout is not installed)
3. Named repo-relative paths that do not exist (file or directory)
4. ``scripts/*.sh`` / ``scripts/*.py`` mentioned as commands that lack ``+x``

Precision over recall: prefer missing a mention to raising a false positive.
Modelled after ``scripts/check_kanban_lifecycle_anchors.py``.
"""

from __future__ import annotations

import argparse
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

SKILL_GLOB = ".claude/skills/**/SKILL.md"
PROMPT_GLOB = "loops/packs/**/*PROMPT.md"

# ---------------------------------------------------------------------------
# Rule 1 — no venv interpreter for pytest
# (same contract as tests/test_skill_hygiene_audit.py)
# ---------------------------------------------------------------------------

_VENV_PYTEST_INVOCATION = re.compile(
    r"(?<![.\w])venv/bin/python\b"
    r"(?:"
    r"[^\n\\]*-m\s+pytest\b"
    r"|"
    r"[^\n]*\\\s*\n[^\n]*-m\s+pytest\b"
    r")",
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Rule 2 — no pytest --timeout=<value>
# ---------------------------------------------------------------------------

_PYTEST_TIMEOUT_FLAG = re.compile(r"--timeout(?:=\S+|\s+\S+)")

# ---------------------------------------------------------------------------
# Rule 3 — repo-relative paths exist
# ---------------------------------------------------------------------------

# Only first segments that are real top-level repo trees. Relative crumbs
# (``lib/schemas.ts``, ``src/App.tsx``) are intentionally out of scope — they
# resolve against a package root, not the checkout root.
_REPO_ROOT_SEGMENTS = (
    r"\.claude",
    r"web",
    r"scripts",
    r"hermes_cli",
    r"loops",
    r"tests",
    r"docs",
    r"plugins",
    r"gateway",
    r"tools",
    r"config",
    r"apps",
    r"ui-tui",
    r"optional-skills",
    r"skills",
    r"website",
    r"tui_gateway",
    r"agent",
    r"packages",
)

# Path token: known root + one or more /segments. Segment charset is strict
# ASCII path chars; globs/placeholders are rejected in post-filters.
_PATH_TOKEN_RE = re.compile(
    r"(?<![\w@:/.-])"
    r"("
    r"(?:" + "|".join(_REPO_ROOT_SEGMENTS) + r")"
    r"(?:/[\w.@+*-]+)+"
    r")"
    r"(?![\w./@-])"
)

_PACKAGE_LOCK_DEAD = "web/package-lock.json"
_PACKAGE_LOCK_LIVE = "package-lock.json"


@dataclass(frozen=True)
class PathException:
    """Explicit allow-list entry for a real path that may be absent on disk.

    Do not soften the matcher to make a number green — put a reasoned entry
    here instead. ``review_by`` is a calendar reminder, not an auto-expiry.
    """

    path: str
    reason: str
    review_by: str  # YYYY-MM-DD


# Paths that legitimately appear in skills/prompts but are created at runtime
# (or live outside the git tree) and therefore cannot be required present.
PATH_EXCEPTIONS: tuple[PathException, ...] = (
    PathException(
        path="web/node_modules",
        reason=(
            "Runtime-provisioned dependency tree (worktree symlink via "
            "HERMES_WORKTREE_DEPS_ROOT / gate-frontend.sh); absent until first gate"
        ),
        review_by="2026-10-28",
    ),
)

_PATH_EXCEPTION_MAP: dict[str, PathException] = {
    entry.path: entry for entry in PATH_EXCEPTIONS
}

# File-ish token: has a short alphanumeric extension (package-lock.json, foo.sh).
_FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,15}$")

# ---------------------------------------------------------------------------
# Rule 4 — scripts mentioned as commands are executable
# ---------------------------------------------------------------------------

_SCRIPT_CMD_RE = re.compile(
    r"(?<![\w./-])(scripts/[\w./-]+\.(?:sh|py))\b"
)


@dataclass(frozen=True)
class Finding:
    path: Path  # file under scan, relative preferred
    line: int
    rule: str
    message: str

    def format(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.message}"


def iter_target_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    files.extend(sorted(repo_root.glob(SKILL_GLOB)))
    files.extend(sorted(repo_root.glob(PROMPT_GLOB)))
    return files


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _snippet(text: str, start: int, end: int, limit: int = 120) -> str:
    snippet = " ".join(text[start:end].split())
    if len(snippet) > limit:
        return snippet[: limit - 3] + "..."
    return snippet


# ---- rule 1 ----

def check_venv_pytest(rel: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for match in _VENV_PYTEST_INVOCATION.finditer(text):
        findings.append(
            Finding(
                rel,
                _line_of(text, match.start()),
                "no-venv-pytest",
                "hardcoded venv interpreter for pytest — use scripts/run_tests.sh "
                f"({_snippet(text, match.start(), match.end())})",
            )
        )
    return findings


# ---- rule 2 ----

def check_pytest_timeout(rel: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for match in _PYTEST_TIMEOUT_FLAG.finditer(text):
        findings.append(
            Finding(
                rel,
                _line_of(text, match.start()),
                "no-pytest-timeout",
                "pytest --timeout is not installed; use runner per-file timeout "
                f"({_snippet(text, match.start(), match.end())})",
            )
        )
    return findings


# ---- rule 3 helpers ----

def _is_url_prefix(text: str, start: int) -> bool:
    prefix = text[max(0, start - 12) : start]
    return bool(re.search(r"(?:https?://|://)$", prefix))


def _path_skip_reason(text: str, match: re.Match[str], path: str) -> str | None:
    """Return a skip reason, or None if the token should be existence-checked.

    Precision first: any doubt → skip (do not report).
    """
    if path.startswith(("/", "~")) or path.startswith("$"):
        return "abs-or-home"
    if _is_url_prefix(text, match.start(1)):
        return "url"
    if any(ch in path for ch in "<>{}…$"):
        return "placeholder"
    if "..." in path or "*" in path:
        return "glob-or-ellipsis"
    if re.search(r"\$[A-Za-z_{]", path):
        return "shell-var"

    segments = path.split("/")
    for seg in segments[1:]:
        if not seg:
            return "empty-segment"
        if seg.startswith("-"):
            return "segment-starts-with-hyphen"
        if seg.endswith("-"):
            return "incomplete-glob-prefix"
        if re.search(r"[äöüÄÖÜß]", seg):
            return "german-umlaut"
        # German compound glued inside a segment: control-Anteil
        if re.search(r"-[A-ZÄÖÜ][a-zäöü]", seg):
            return "hyphen-uppercase-compound"

    end = match.end(1)
    after = text[end : end + 24]
    # Boundary: path.json-Abhängigkeiten / path-Anteil
    if re.match(r"-[A-Za-zÄÖÜäöüß]", after):
        return "boundary-hyphen-letter"
    # Boundary continues a German word without separator (rare with our charset)
    if after and re.match(r"[äöüÄÖÜß]", after):
        return "boundary-german"

    return None


def _normalize_path_token(path: str) -> str:
    return path.rstrip("/")


def check_repo_paths(repo_root: Path, rel: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[int, str]] = set()

    for match in _PATH_TOKEN_RE.finditer(text):
        raw = match.group(1)
        skip = _path_skip_reason(text, match, raw)
        if skip is not None:
            continue
        path = _normalize_path_token(raw)
        line = _line_of(text, match.start(1))
        key = (line, path)
        if key in seen:
            continue
        seen.add(key)

        if path in _PATH_EXCEPTION_MAP:
            continue

        target = repo_root / path
        if target.is_file() or target.is_dir():
            continue

        # Precision: two-segment tokens without a file extension are often
        # prose slash-joins ("restart gateway/dashboard"), not filesystem
        # paths. Existing dirs/files already passed above; missing ones are
        # skipped rather than raised (prefer miss over false positive).
        segments = path.split("/")
        if len(segments) < 3 and not _FILE_EXT_RE.search(path):
            continue

        findings.append(
            Finding(
                rel,
                line,
                "path-exists",
                f"repo-relative path does not exist: {path}",
            )
        )
    return findings


# ---- rule 4 ----

def _has_shebang(target: Path) -> bool:
    """True when the file starts with ``#!`` — i.e. it is meant to be executed.

    Files without one are sourced libraries; see the call site for why that
    distinction has to exist.
    """
    try:
        with target.open("rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


def check_script_executable(repo_root: Path, rel: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[int, str]] = set()

    for match in _SCRIPT_CMD_RE.finditer(text):
        script = match.group(1)
        if any(ch in script for ch in "<>{}*…$") or "..." in script:
            continue
        line = _line_of(text, match.start(1))
        key = (line, script)
        if key in seen:
            continue
        seen.add(key)

        target = repo_root / script
        if not target.is_file():
            # Missing script is already a path-exists finding when it matches
            # the path token regex; still report under rule 4 for clarity.
            findings.append(
                Finding(
                    rel,
                    line,
                    "script-executable",
                    f"script does not exist: {script}",
                )
            )
            continue
        if not _has_shebang(target):
            # A sourced library, not a command. `scripts/lib/select_test_python.sh`
            # is the live example: its own header says "Sourced, not executed", it
            # only defines a function, and callers reach it via `. <path>`. Demanding
            # +x there invites someone to run it, which silently does nothing — and
            # it churns the file mode of a tracked file for no functional reason.
            # A shebang is the discriminator: commands have one, libraries do not.
            continue
        mode = target.stat().st_mode
        if not (mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)):
            findings.append(
                Finding(
                    rel,
                    line,
                    "script-executable",
                    f"script is not executable (+x missing): {script}",
                )
            )
    return findings


# ---- scan / fix ----

def scan_file(repo_root: Path, file_path: Path) -> list[Finding]:
    rel = file_path.relative_to(repo_root)
    text = file_path.read_text(encoding="utf-8")
    findings: list[Finding] = []
    findings.extend(check_venv_pytest(rel, text))
    findings.extend(check_pytest_timeout(rel, text))
    findings.extend(check_repo_paths(repo_root, rel, text))
    findings.extend(check_script_executable(repo_root, rel, text))
    return findings


def scan_tree(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in iter_target_files(repo_root):
        findings.extend(scan_file(repo_root, path))
    return findings


def fix_package_lock_refs(repo_root: Path) -> list[str]:
    """Rewrite dead ``web/package-lock.json`` → root ``package-lock.json``.

    Only this mechanical rename is auto-fixed; everything else stays manual.
    """
    reports: list[str] = []
    for path in iter_target_files(repo_root):
        text = path.read_text(encoding="utf-8")
        if _PACKAGE_LOCK_DEAD not in text:
            continue
        count = text.count(_PACKAGE_LOCK_DEAD)
        new_text = text.replace(_PACKAGE_LOCK_DEAD, _PACKAGE_LOCK_LIVE)
        path.write_text(new_text, encoding="utf-8")
        rel = path.relative_to(repo_root)
        reports.append(f"fixed {rel}: {count}× {_PACKAGE_LOCK_DEAD} -> {_PACKAGE_LOCK_LIVE}")
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repository root (default: cwd)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help=(
            "apply mechanical fixes only (web/package-lock.json → "
            "package-lock.json), then re-check"
        ),
    )
    args = parser.parse_args(argv)
    repo_root = (args.root or Path.cwd()).resolve()

    if args.fix:
        for report in fix_package_lock_refs(repo_root):
            print(report)

    findings = scan_tree(repo_root)
    for finding in findings:
        print(finding.format())

    if findings:
        print(f"FAIL: {len(findings)} finding(s)", file=sys.stderr)
        return 1

    n_files = len(iter_target_files(repo_root))
    print(f"OK: {n_files} skill/prompt files clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
