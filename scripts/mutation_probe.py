#!/usr/bin/env python3
"""Measure how much of a module's behaviour its tests actually pin down.

Generates deterministic single-edit mutants of ``MODULE`` with
:func:`hermes_cli._ast_mutator.generate_mutants`, applies them one at a time,
and runs ``TESTFILE`` against each one.

    KILLED    the tests fail  -> that behaviour is pinned
    SURVIVED  the tests pass  -> that behaviour is NOT pinned (a real gap)
    TIMEOUT   the run hangs   -> counted as killed, listed separately

The module is restored from an in-memory copy in a ``finally`` block and on
SIGINT/SIGTERM, so an interrupted run never leaves a mutant on disk. The run
refuses to start when the module already has uncommitted changes.

    scripts/mutation_probe.py hermes_cli/foo.py tests/test_foo.py
    scripts/mutation_probe.py hermes_cli/foo.py tests/test_foo.py --max 30
    scripts/mutation_probe.py hermes_cli/foo.py tests/test_foo.py --check 7

``--check K`` applies exactly mutant K, runs the tests once, restores, and
exits 0 when that mutant is killed / 1 when it survives — the red-proof for a
newly written test. Mutant indices are stable for a given module source; the
source sha1 is printed so a ledger entry can record which revision they refer to.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import signal
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hermes_cli._ast_mutator import generate_mutants  # noqa: E402


def _run_tests(testfile: Path, timeout: float) -> tuple[str, float]:
    """Run one pytest file. Returns (outcome, seconds).

    outcome is "pass", "fail" or "timeout".
    """
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "-q", "--no-header", str(testfile)],
            cwd=_REPO,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "timeout", time.monotonic() - started
    return ("pass" if proc.returncode == 0 else "fail"), time.monotonic() - started


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("module", help="source file to mutate, e.g. hermes_cli/foo.py")
    ap.add_argument("testfile", help="pytest file that is supposed to pin it")
    ap.add_argument("--max", type=int, default=20, help="max mutants to generate (default 20)")
    ap.add_argument("--check", type=int, metavar="K", help="verify a single mutant index and exit")
    args = ap.parse_args()

    module = (_REPO / args.module).resolve()
    testfile = (_REPO / args.testfile).resolve()
    for path in (module, testfile):
        if not path.is_file():
            print(f"mutation_probe: no such file: {path}", file=sys.stderr)
            return 2

    dirty = subprocess.run(
        ["git", "diff", "--quiet", "--", str(module)], cwd=_REPO
    ).returncode
    if dirty:
        print(
            f"mutation_probe: {args.module} has uncommitted changes — commit or revert "
            "them first, otherwise a crash could not restore a clean original.",
            file=sys.stderr,
        )
        return 2

    original = module.read_text(encoding="utf-8")
    sha = hashlib.sha1(original.encode("utf-8")).hexdigest()[:12]
    # A UTF-8 BOM survives read_text(encoding="utf-8") and then makes ast.parse
    # raise "invalid non-printable character U+FEFF" — the import machinery hides
    # this because it decodes with utf-8-sig. Strip it for the mutator only;
    # `original` stays byte-identical so the restore path never rewrites the file.
    parseable = original.lstrip("﻿")

    restore_done = False

    def _restore() -> None:
        nonlocal restore_done
        if not restore_done:
            module.write_text(original, encoding="utf-8")
            restore_done = True

    def _on_signal(signum, _frame):  # pragma: no cover - interactive safety net
        _restore()
        print(f"\nmutation_probe: interrupted ({signum}) — {args.module} restored.", file=sys.stderr)
        sys.exit(130)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    skipped: list[str] = []
    mutants = generate_mutants(
        parseable,
        max_mutants=args.max,
        source_name=args.module,
        on_skip=skipped.append,
    )
    if not mutants:
        # "0 mutants" has three different causes and they are not equally
        # interesting. Name the real one — a silent "nothing to measure" is
        # exactly the failure mode this whole run exists to hunt.
        if skipped:
            reason = skipped[0]
        else:
            try:
                tree = ast.parse(parseable)
            except SyntaxError as exc:
                reason = f"the module does not parse: {exc}"
            else:
                nodes = sum(1 for _ in ast.walk(tree))
                reason = (
                    f"parses fine ({nodes} AST nodes) but the mutator found no "
                    "mutable site — no if/comparison/boolop/return/constant to edit"
                )
        print(f"mutation_probe: no mutants for {args.module} — {reason}", file=sys.stderr)
        return 2

    try:
        print(f"# module   {args.module}  (sha1 {sha}, {len(mutants)} mutants)")
        print(f"# tests    {args.testfile}")

        outcome, baseline_s = _run_tests(testfile, timeout=900)
        if outcome != "pass":
            print(
                f"mutation_probe: baseline is {outcome.upper()} on the unmutated module "
                "— fix or pick another test file; mutation numbers are meaningless "
                "against a red baseline.",
                file=sys.stderr,
            )
            return 3
        print(f"# baseline GREEN in {baseline_s:.1f}s")
        # A mutant that hangs must not stall the whole sweep. Generous but bounded.
        per_mutant_timeout = max(60.0, baseline_s * 4 + 30.0)

        if args.check is not None:
            if not 0 <= args.check < len(mutants):
                print(f"mutation_probe: --check {args.check} out of range 0..{len(mutants) - 1}", file=sys.stderr)
                return 2
            m = mutants[args.check]
            module.write_text(m.mutated_source, encoding="utf-8")
            restore_done = False
            outcome, secs = _run_tests(testfile, per_mutant_timeout)
            _restore()
            killed = outcome in ("fail", "timeout")
            print(f"[{args.check:3d}] {'KILLED  ' if killed else 'SURVIVED'} {m.operator} L{m.lineno}: {m.description} ({secs:.1f}s)")
            return 0 if killed else 1

        killed = survived = timed_out = 0
        survivors: list[tuple[int, object]] = []
        for i, m in enumerate(mutants):
            module.write_text(m.mutated_source, encoding="utf-8")
            restore_done = False
            outcome, secs = _run_tests(testfile, per_mutant_timeout)
            _restore()
            if outcome == "pass":
                survived += 1
                survivors.append((i, m))
                label = "SURVIVED"
            elif outcome == "timeout":
                timed_out += 1
                killed += 1
                label = "TIMEOUT "
            else:
                killed += 1
                label = "KILLED  "
            print(f"[{i:3d}] {label} {m.operator} L{m.lineno}: {m.description} ({secs:.1f}s)", flush=True)

        total = len(mutants)
        score = 100.0 * killed / total
        print(f"\n# SCORE {killed}/{total} killed = {score:.1f}%  (survived {survived}, of which timeouts counted killed: {timed_out})")
        if skipped:
            print(f"# generator skipped {len(skipped)} site(s): {skipped[0]}")
        if survivors:
            print("# SURVIVORS — each one is an unpinned behaviour:")
            for i, m in survivors:
                print(f"#   [{i}] {m.operator} L{m.lineno}: {m.description}")
        return 0
    finally:
        _restore()


if __name__ == "__main__":
    raise SystemExit(main())
