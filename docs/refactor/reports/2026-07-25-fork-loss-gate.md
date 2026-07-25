# fork_loss_check — a fork-loss measurement that survives the worktree

Status: **complete**, on branch `claude/fork-loss-gate`. Tool + tests + gates.
No repair of any finding (the tool measures; judging intent is a human call),
no CI/cron/dashboard wiring, no upstream sync, `hermes_cli/kanban_db.py`
untouched.

## Why this exists

On 2026-07-24 a 1962-commit upstream merge landed against a receipt that said
`status: blocked`, because a "blob-based fork-loss selftest" reported **56
files**. The merge landed nine minutes later; 42 tests went red. The worktree
was deleted, `MERGE-REPORT.md` was never committed, and the measuring tool
disappeared with them. The number 56 cannot be recomputed by anyone today.

The deliverable is therefore not a number — it is the *recomputability* of the
number. The tool lives in the repo, with tests, and it can be re-run against any
past or future sync.

## Files

| File | Lines |
|---|---:|
| `scripts/refactor/fork_loss_check.py` | 504 |
| `tests/refactor/test_fork_loss_check.py` | 441 |

## Definition (the thing that produces the number)

1. **Risk surface** = files changed by BOTH the fork and upstream against the
   old merge base. Files only the fork touched cannot lose a conflict and only
   get an existence check. Files the **fork itself deleted** are excluded from
   both sets — absence the fork asked for is not loss.
2. **Stage 1 (broad):** every line the fork added to a risk-surface file whose
   whitespace-normalised form appears nowhere in the current file. Brackets,
   bare keywords, imports and lines under 12 characters are noise.
   Two false-positive guards: the comparison is *set-based* (a moved line is not
   a candidate), and a candidate that appears in the current file collapsed to a
   single whitespace-normalised string is a **reflow**, not a loss.
3. **Stage 2 (narrow):** each survivor is attributed via `ast` to its enclosing
   symbol in the fork tip; the verdict is `SYMBOL_GONE`, `SYMBOL_CHANGED`, or
   `LINE_ONLY` when no symbol is attributable (non-Python, or unparseable).
   `FILE_GONE` covers a file that is not in the tree at all.

Exit code: `0` no findings, `1` findings, `2` usage/ref error — callable as a
gate in a sync flow.

## Measurement of the 2026-07-24 sync

```
--old-base 3bfa6001f --fork-tip b20d0c8f3 --upstream-tip 306c9f766
```

| | at the merge commit `b5fb78eb3` | at current `HEAD` |
|---|---:|---:|
| fork changed / upstream changed | 1967 / 2597 | 1967 / 2597 |
| risk surface (scanned) | 277 (276) | 277 (276) |
| fork-added candidate lines | 59480 | 59480 |
| still present | 58818 | 58759 |
| moved / reflowed (not a loss) | 24 | 31 |
| missing | 638 | 690 |
| findings / files | 38 / 32 | 46 / 35 |
| FILE_GONE / SYMBOL_GONE / SYMBOL_CHANGED / LINE_ONLY | 0 / 3 / 18 / 17 | 0 / 3 / 26 / 17 |

The risk surface reproduces the brief's independently recorded 1967 / 2597 /
**277** exactly. The historical "56 files" is not reproduced and cannot be — the
tool that produced it no longer exists, so its definition is unknown. A run
over 277 files takes ~12s (`git show`/`git diff` only, no checkouts).

The largest `LINE_ONLY` block is `package-lock.json` (451 lines): true by the
definition, and a good illustration of why the tool does not decide what to do
about a finding.

## Ground truth

Nine synthetic git repos built in `tmp_path` (the real history is too big, too
slow and moves), plus a real-history smoke test that only asserts termination
and well-formed JSON — deliberately no number pinned to a moving tree.

| Case | Expectation |
|---|---|
| fork function kept by the merge | no finding |
| fork function dropped by the merge | exactly one `SYMBOL_GONE` |
| fork lines inside an upstream function, upstream's version taken | exactly one `SYMBOL_CHANGED` |
| **fork lines moved + re-indented into a class + call reflowed** | **no finding** |
| fork-only file | outside the risk surface, no finding |
| fork-only file deleted by the merge | one `FILE_GONE` (`scope: fork_only`) |
| file deleted upstream, changed by the fork | one `FILE_GONE`, no crash |
| file deleted by the fork itself | no finding |
| non-Python file | `LINE_ONLY`, no parser |

The fourth case is the load-bearing one: a tool that calls every reformatting a
loss produces exactly the unverifiable number this script replaces. Its
assertion checks the *reason* as well as the result (`moved_or_reflowed == 1`),
so it cannot pass vacuously — in that fixture the fork function even changes its
qualified name to `ForkPolicies.fork_policy`, which a naive symbol check would
report as `SYMBOL_GONE`.

## Two bugs the tests and the real run caught

- **Local assignments were treated as symbols.** `total = ...` inside a function
  became its own one-line span, so one lost function split into a finding per
  line. Assignments now count only outside function bodies.
- **Files the fork itself deleted were reported as `FILE_GONE`.** All three
  `FILE_GONE` findings of the first real run were phantoms
  (`tests/hermes_cli/test_kanban_core_functionality.py`,
  `web/src/components/SlashPopover.tsx`, `web/src/lib/slashExec.ts`). Found only
  by running against the real sync, not by the synthetic fixtures — now covered
  by a regression test.

## Gates

| Command | Exit | Summary |
|---|---:|---|
| `venv/bin/python -m ruff check scripts/refactor/ tests/refactor/` | 0 | `All checks passed!` |
| `venv/bin/python -m pytest tests/refactor/test_fork_loss_check.py -q` | 0 | `11 passed in 0.90s` |
| `bash scripts/run-affected.sh` | 0 | `=== Summary: 1 files, 11 tests passed, 0 failed (100% complete) in 1.4s (6 workers) ===` |
| `bash scripts/run-affected.sh main` | 0 | `=== Summary: 2 files, 193 tests passed, 0 failed (100% complete) in 10.3s (6 workers) ===` |

Note on the last two: with an explicit ref, `affected_tests.py` uses
`git diff --name-only <ref>` only and therefore does **not** see untracked
files. `run-affected.sh main` consequently selects the branch's other delta
(the loops tests), not this work; the ref-less form is the one that gates an
uncommitted slice.
