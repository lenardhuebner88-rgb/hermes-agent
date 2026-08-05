"""Pack prompts must not name a *relative* interpreter path.

Loop phases run inside the pack's worktree under ``~/.hermes/loops/<pack>/wt``,
and that worktree carries no ``.venv``/``venv`` of its own. A prompt that says
``.venv/bin/python …`` therefore resolves to nothing there. The failure is
quiet in the worst way: the agent reports BLOCKED for a missing interpreter and
the round ends rc=0, so the pack looks like it merely had nothing to do.

Measured 2026-08-05: ``dead-code-scout`` blocked on exactly this in its first
run ever (``BLOCKED missing .venv/bin/python in worktree``), while
``silent-failure-sweep`` carried the same relative path and only survived
because its snippet needs nothing but the stdlib.

Absolute interpreter paths are fine — they point at the live checkout on
purpose.
"""

from __future__ import annotations

import re

from loops.runner import PACKS_DIR

# `.venv/bin/python` or `venv/bin/python` NOT preceded by `/` (i.e. relative).
RELATIVE_INTERPRETER = re.compile(r"(?<![\w/.])\.?venv/bin/python")


def _prompt_files():
    for pack_dir in sorted(PACKS_DIR.iterdir()):
        if not pack_dir.is_dir() or pack_dir.name.startswith("_"):
            continue
        yield from sorted(pack_dir.glob("*PROMPT*.md"))


def test_no_pack_prompt_names_a_relative_interpreter_path():
    offenders = []
    for prompt in _prompt_files():
        for number, line in enumerate(
            prompt.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if RELATIVE_INTERPRETER.search(line):
                offenders.append(
                    f"{prompt.relative_to(PACKS_DIR.parent)}:{number}: {line.strip()}"
                )
    assert not offenders, (
        "Pack-Prompts nennen einen relativen Interpreter-Pfad; im Loop-Worktree "
        "gibt es kein .venv/venv. Absoluten Pfad verwenden:\n  "
        + "\n  ".join(offenders)
    )


def test_guard_still_recognises_the_pattern_it_is_meant_to_catch():
    """Control: an all-green run above must not be the regex failing to match."""
    assert RELATIVE_INTERPRETER.search(".venv/bin/python scripts/x.py")
    assert RELATIVE_INTERPRETER.search("cd {{WT}} && venv/bin/python -c 'x'")
    # Absolute paths stay allowed — they resolve outside the worktree.
    assert not RELATIVE_INTERPRETER.search(
        "/home/piet/.hermes/hermes-agent/.venv/bin/python scripts/x.py"
    )
