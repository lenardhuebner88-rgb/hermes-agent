# Delta to the giant-module modularization plan — re-aimed at merge-capability

- **Date:** 2026-07-24
- **Amends:** `docs/design/2026-07-24-giant-module-modularization-design.md` and
  `docs/refactor/2026-07-24-giant-module-modularization-plan.md`
- **Author:** Claude Code session `work:5` (grill-with-docs with operator)
- **Status:** operator-decided (goal + cut), branch-triage gate still open
- **Nature:** this does **not** discard the plan. The tooling is unchanged and still correct.
  What changes is the *scope* and the *cut*.

## Why

The operator set a new overriding goal on 2026-07-24:

> merge-fähig bleiben und Hermes-Updates einfach bekommen — alles an Upstream sinnvollen Sachen
> reinholen, aber auch CodeGraph nutzbar machen **wenn wirklich sinnvoll**.

The existing plan cuts by source order and treats all five files alike. Measured against that
goal, the five files are in three different situations, and two of the five changes must be
inverted.

## Measured ground truth (verify, don't trust — commands included)

Merge-base with upstream: `3bfa6001f` (**2026-07-15**, 9 days old — the base is *fresh*).
Note: the "1962 commits / 3.5 months behind" figure is misleading. The oldest *author date* in
`HEAD..origin/main` is 2026-04-10, but those are long-lived upstream branches merged later; the
actual divergence point is 9 days ago.

```
MB=$(git merge-base HEAD origin/main)
git diff --stat $MB HEAD        -- <file>   # fork delta
git diff --stat $MB origin/main -- <file>   # upstream delta
```

| file | lines @ base | fork today | upstream today | fork delta | upstream delta |
|---|---:|---:|---:|---:|---:|
| `hermes_cli/kanban_db.py` | 9,135 | **38,834** | 9,788 | +40,883 / −6,163 | +693 |
| `gateway/run.py` | 21,574 | **21,875** | 24,287 | +519 | +3,887 |
| `hermes_cli/web_server.py` | — | — | — | +3,667 | +3,681 |

Byte sizes vs. the CodeGraph skip limit (`MAX_FILE_SIZE` = 1 MiB = 1,048,576 B):

| file | fork today | CodeGraph | upstream's own copy |
|---|---:|---|---:|
| `hermes_cli/kanban_db.py` | 1,589,066 | **blind** | 406,482 |
| `gateway/run.py` | 1,062,191 | **blind** | **1,178,861** |
| `hermes_cli/web_server.py` | 803,509 | visible | — |
| `cli.py` | 772,748 | visible | — |
| `hermes_cli/main.py` | 618,546 | visible | — |

Two conclusions follow directly and neither is in the current plan:

1. **Three of the five "giants" are already CodeGraph-visible.** The premise that five modules are
   invisible does not survive measurement. Splitting them buys nothing toward either goal.
2. **`gateway/run.py` is 97.6% upstream's file, and upstream's own copy is *already* 1.18 MB** —
   i.e. blind at upstream too, and growing. CodeGraph visibility there is purchasable only with
   permanent divergence against the largest incoming change stream in the repo (+3,887 lines since
   the merge-base). It is not worth it.

That leaves **exactly one file where both goals are reachable at once**: `kanban_db.py`, which the
fork grew 9,135 → 38,834 lines while upstream contributed 693. Extracting the fork-owned portion
drops it to roughly upstream's 406 KB — CodeGraph-visible *and* cleanly mergeable.

## The three changes

### Change 1 — scope reduces to `hermes_cli/kanban_db.py`

Out of scope, with reasons:

- `gateway/run.py` — **must not be split.** Instead, shrink the 519-line fork delta toward a hook
  (see Change 3). Accept CodeGraph blindness here; `rg` + `docs/kanban/LIFECYCLE.md` remain the
  documented navigation route for this file.
- `hermes_cli/web_server.py` — genuinely contested (+3,667 fork vs. +3,681 upstream) and already
  CodeGraph-visible. Needs its own ownership analysis later; not part of this pass.
- `cli.py`, `hermes_cli/main.py` — already visible, no merge pressure. Leave them.

### Change 2 — cut by ownership, not by source order

The plan's `_section_owner_from_banners` path is not used for this file. Build the boundary map
from ownership instead and feed it via the `--boundary-map` interface the plan already supports:

> *"Ordering rule: submodules are emitted in the order the boundary map lists them, which **for
> banner-derived maps** is the source file's own order."*

**Ownership test:** a top-level symbol is `UPSTREAM` if a symbol of that name exists in
`git show origin/main:hermes_cli/kanban_db.py`; otherwise `FORK`.

Use `origin/main` (today's upstream), **not** the merge-base. This is the forward-looking test:
what matters is which symbols upstream still carries, because those are the ones future merges
will touch.

**Three cases the analyzer must report separately, not silently bucket:**

| case | disposition |
|---|---|
| symbol only in fork | → moves out to the fork package |
| symbol in both, body byte-identical | → stays in `kanban_db.py`, untouched |
| symbol in both, **body diverged** | → stays in `kanban_db.py` **and is listed in the report** — each one is a standing conflict site and a candidate for later hook-reduction |

The third bucket is the one that decides whether this exercise actually succeeds. It must be
counted and printed, not folded into "upstream".

### Change 3 — the target shape is *not* a package at the same path

This is the load-bearing correction. The plan turns each giant into a package whose `__init__.py`
re-exports, keeping the same import path. That preserves *Python* compatibility but destroys
*merge* compatibility: upstream's future diffs are addressed to the literal path
`hermes_cli/kanban_db.py`, and if that path is a directory, every one of them fails to apply.

Target shape instead:

- **`hermes_cli/kanban_db.py` stays a plain module file** and keeps only `UPSTREAM` symbols, in
  upstream's order, as textually close to upstream's copy as the fork's own edits allow.
- **Fork-added symbols move to a new fork-owned package** (`hermes_cli/kanban_ext/`, name open),
  split into submodules by the existing tooling.
- `kanban_db.py` ends with **one** explicit re-export block from that package, so the ~275
  importing files and the tests that monkeypatch private symbols are unchanged. Explicit names,
  no `import *` — the plan's existing rule.

That trailing block is the *only* fork-owned hunk left in an upstream-owned file: one small,
stable conflict site per merge instead of hundreds spread through 29,700 lines.

## What is unchanged

Everything that makes the plan good survives untouched:

- **Task 1 `api_snapshot.py`** — the equivalence gate. Unchanged and more important than before,
  since symbols now cross a package boundary.
- **Task 2 `layering.py`** — unchanged. The import-time/runtime reference distinction is
  orthogonal to how the boundary map was derived.
- **Tasks 3–4 `split_module.py --analyze/--apply`** — unchanged; they consume a boundary map and
  do not care where it came from.
- Global constraints, test scope (`scripts/run-affected.sh` while building; one collection sweep
  plus affected tests before merge), one branch per file, AST-deterministic moves, no model
  retyping code.
- **Task 0 branch triage** — still required and still blocking, but now only for the branches
  carrying `kanban_db.py` deltas. **The operator decision is still open as of this writing.**

## One new acceptance criterion

The whole point is mergeability, so measure it directly rather than assuming it:

1. **Before** the split, record the conflict surface against upstream:
   `git merge-tree --write-tree HEAD origin/main` → count conflict hunks in
   `hermes_cli/kanban_db.py`.
2. **After** the split, the same command must report **strictly fewer** conflict hunks for that
   path. If it does not, the ownership map was wrong and the split must not land.
3. `hermes_cli/kanban_db.py` must end below 1,048,576 bytes (expected ≈ 406 KB + the re-export
   block), verified with `stat -c%s`.

Criterion 2 is the ground truth for this entire piece of work. Criterion 3 is the CodeGraph payoff.

## Standing rule that follows from this

New fork code never goes into an upstream-owned file. It goes into a fork-owned module with a
minimal hook. Without this rule `kanban_db.py` simply re-accumulates and the work is undone within
months — the file went 9,135 → 38,834 lines in the time the rule was absent.
