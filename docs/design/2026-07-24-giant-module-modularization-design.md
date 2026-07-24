# Design: modularize the five giant modules

- **Date:** 2026-07-24
- **Status:** approved by operator (Piet), ready for implementation planning
- **Authors:** Claude Opus 5 session, with operator decisions recorded inline
- **Base commit:** `94872c586` (main)

## Problem

Five modules carry 112,912 lines between them. Two of them exceed 1 MiB, which is
CodeGraph's hard `MAX_FILE_SIZE`
(`~/.codegraph/versions/v1.5.0/lib/dist/extraction/index.js:113`), so those two are
**absent from the code index entirely**: `codegraph query dispatch_once` returns only
`fake_dispatch_once` from test files, never the real definition. The two invisible files
are the kanban core and the gateway core — precisely the subsystems under active
development.

| file | lines | size | CodeGraph |
|---|---|---|---|
| `hermes_cli/kanban_db.py` | 38,834 | 1.51 MiB | **invisible** |
| `gateway/run.py` | 21,875 | 1.01 MiB | **invisible** |
| `hermes_cli/web_server.py` | 20,314 | 0.79 MiB | indexed (1,248 symbols) |
| `cli.py` | 16,797 | 0.72 MiB | indexed |
| `hermes_cli/main.py` | 15,092 | 0.60 MiB | indexed |

Beyond tooling, no cheap model can hold a 38k-line file in context, so every model we
route work to must rediscover structure by grep. The goal is **more structure so other
models understand this repo more easily** — CodeGraph visibility is the measurable proxy.

## Goal and success criteria

1. Every resulting source file is **under 1 MiB** and appears in the CodeGraph index with
   its symbols queryable (`codegraph query dispatch_once` returns the real definition).
2. The **public API of every split module is byte-identical** before and after, proven
   mechanically, not by review.
3. **Zero call-site edits.** 308 files reference `kanban_db`; 275 import it (244 + 17 by
   module handle, 14 symbol-level). None of them changes.
4. Affected tests stay green; each file's split is independently revertible.

## Why this is safe: the import evidence

Measured on `94872c586`:

| import style | occurrences | files |
|---|---|---|
| `from hermes_cli import … kanban_db` (module handle) | 436 | 244 |
| `import hermes_cli.kanban_db` (module handle) | 96 | 17 |
| `from hermes_cli.kanban_db import X` (symbol-level) | 20 | 14 |
| `from .kanban_db import X` (relative) | 0 | 0 |

~97% of call sites use the module handle and call `kanban_db.foo()`. Converting a module
into a package whose `__init__.py` re-exports its submodules keeps every one of those
working untouched — and the 14 symbol-level importers keep working too, because
`from hermes_cli.kanban_db import X` resolves against the package's `__init__`.

This is what makes the operation cheap. A naive split that rewrote call sites would be a
very different, much riskier change.

## Architecture

Each giant module becomes a package. Illustrative, for `kanban_db.py`:

```
hermes_cli/kanban_db/__init__.py      re-exports; public surface unchanged
hermes_cli/kanban_db/constants.py     VALID_STATUSES, limits, module-level state
hermes_cli/kanban_db/schema.py        SCHEMA_SQL, migrations
hermes_cli/kanban_db/connection.py    connect, write_txn, invariants
hermes_cli/kanban_db/tasks.py         create/mutate
hermes_cli/kanban_db/deps.py          dependency resolution, recompute_ready
hermes_cli/kanban_db/claim.py         claim/complete/block
hermes_cli/kanban_db/review_gate.py   review chain
hermes_cli/kanban_db/workspaces.py    workspace resolution + cleanup
hermes_cli/kanban_db/dispatch.py      dispatcher daemon, holds, respawn guard
hermes_cli/kanban_db/worker_ctx.py    worker context builder
hermes_cli/kanban_db/stats.py         stats, SLA, runs
hermes_cli/kanban_db/epics.py         epics, disposition ledger, lanes
...
```

**Boundary granularity (operator decision): ~12–16 modules of 2,000–4,000 lines each** for
`kanban_db.py`. The file's own 34 titled section banners are the boundary candidates;
related banners merge. Rationale: coherent units, and small enough that a cheap model can
hold one entire module in context. The other four files scale proportionally.

The 34 banners mean we are **formalizing structure the file already documents**, not
inventing a taxonomy.

## The mechanism

Three artifacts, built once, reused five times. **Models never retype code.**

| artifact | responsibility |
|---|---|
| `scripts/refactor/split_module.py` | Reads a boundary map; moves top-level defs/classes **byte-exact** via AST; generates the re-exporting `__init__.py`; converts intra-module references into imports |
| `scripts/refactor/api_snapshot.py` | Records a module's public symbols + signatures before; diffs after. **The equivalence gate.** |
| `boundary-map.<file>.yaml` (×5) | Which symbol lands in which submodule |

A model proposes the boundary map (a small YAML). A deterministic script performs the
move. This is the core safety property: across 112,912 moved lines, no line is
model-authored, so silent logic drift is structurally impossible rather than
review-dependent.

### Per-file sequence

```
1. api_snapshot.py --before          record public surface
2. model proposes boundary map       from section banners + call clustering
3. operator + Claude approve the map  <-- the human gate
4. split_module.py                   deterministic move
5. api_snapshot.py --after --diff    MUST be empty
6. scripts/run-affected.sh           MUST be green
7. python -c "import <module>"       import-time smoke
8. codegraph reindex                 symbols visible = success proof
```

Steps 5–8 are mechanical pass/fail, so a bad boundary map fails loudly.

### Pure-move discipline

The split commits contain **moves only**. No renames, no reformatting, no bug fixes, no
docstring corrections — including the five known defects listed under Follow-ups. Mixing
fixes into the move would invalidate the equivalence gate, which is the only thing making
a 113k-line change reviewable.

## Risks and mitigations

- **Circular imports between new submodules.** The real failure mode. The splitter must
  detect a cycle and *refuse with a report* rather than emit it; anything that would cycle
  stays in `_core.py` for that round.
- **Module-level state** (`VALID_STATUSES`, limits, caches) must have exactly one home —
  `constants.py`, imported everywhere else. Duplicating it would create two sources of truth.
- **`git blame` discontinuity.** A pure move breaks blame at the move commit. Mitigation:
  each split lands as its own commit containing *only* moves, so `blame --follow` and
  `log --follow` traverse it. This is a real, accepted cost.
- **Independent revertibility.** One branch and one commit per file, so a bad
  `web_server.py` split cannot force unwinding `kanban_db.py`.
- **Anchor breakage in `docs/kanban/LIFECYCLE.md`.** Its 95 anchors point into
  `kanban_db.py`. `--fix` handles line drift within a file, not symbols moving to new
  files. **Re-anchoring LIFECYCLE.md to the new module paths is part of the kanban_db
  slice's done-when.** The map improves from the split: anchors land in small files.

## Prerequisite: branch triage

Eleven branches still carry `kanban_db.py` deltas. After the split their diffs can no
longer auto-merge, because the file will not exist at that path. Operator decision:

- **Review and decide land-or-drop** (3 branches with substantial deltas):
  `kanban/t_c254b029` (626+/284−, 22.07), `codex/board-model-truth-20260713` (713+/47−,
  14.07), `kanban/t_610a9f84` (187+/34−, 14.07).
- **Archive as tag, then delete** (8 trivial or ≥1 week stale): `kanban/t_49c1e99b`,
  `kanban/t_57aaa085`, `kanban/t_69536fff`, `kanban/t_80809063`, `kanban/t_d2d25240`,
  `salvage/dirty-main-20260712T014834`, `backup/grok-kanban-block-kind-20260715-pre-rebase`,
  `worktree-bridge-cse_01HZiECqoEjuEdJuA5DWYFys`.

This must complete before the first split lands.

## Model routing

| work | lane | why |
|---|---|---|
| `split_module.py`, `api_snapshot.py` + tests | **GPT-5.6 / Codex (`sol`)** | precision-critical, existing-code fidelity; delivered Lever 2 cleanly |
| 5 boundary maps | **qwen 3.8** via `claude-qwen -p` | read-heavy, cheap, small gated YAML output |
| boundary-map approval, gate verification, merge judgment | **Claude Opus 5, high effort** | the only genuinely architectural judgment left |

**ToS constraint (binding):** `/usr/local/bin/claude-qwen` line 12 — *"Token Plan =
interactive coding/agent use only (no batch/cron workers)."* qwen may be used for
session-driven one-shots dispatched and supervised by an interactive session. It must
**not** be wired as a kanban lane or cron worker.

## Order of work

1. Branch triage (prerequisite, above).
2. Build and test `split_module.py` + `api_snapshot.py` (Codex).
3. `kanban_db.py` — the pilot. Proves the tooling and the gates. Includes re-anchoring
   `LIFECYCLE.md`.
4. `gateway/run.py` — completes the CodeGraph blind spot.
5. `web_server.py`, `cli.py`, `main.py` — same recipe, already-indexed files, lower urgency.

Each file is a separate branch, separate review, separate merge.

## Follow-ups (explicitly NOT fixed during the split)

Found by Codex while mapping the lifecycle; each needs its own change with its own test:

1. `dispatch_once` states the board lock is the single-writer boundary, but a DB-path
   resolution failure takes an explicitly unguarded `_dispatch_once_locked` path
   (`kanban_db.py:28799-28820`), weakening the invariant on an exceptional path.
2. The status vocabulary is nine values (`kanban_db.py:126-136`), but cleanup/status
   filters still reference `failed`, `canceled`, `cancelled` (e.g. lines 3279, 6860,
   25005) — unclear whether defensive legacy compatibility or dead vocabulary.
3. `block_task`'s docstring claims `running -> blocked`; the real behaviour is broader
   (`ready` accepted, waits land in `todo`, repeated non-review blocks land in `triage`).
4. The review-dispatch comment (`kanban_db.py:29894-29898`) says rejection goes "back to
   running"; the implementation lands in `blocked` and needs a retry/unblock.
5. Inconsistent banner layout around scheduling/dispatch (orphan divider at line 20120).

## Out of scope

- Any behaviour change, bug fix, or API change.
- Rewriting call sites.
- Splitting modules below ~15k lines.
- Raising CodeGraph's `MAX_FILE_SIZE` (a vendored-dist patch that reverts on tool upgrade;
  superseded by this work, which fixes the cause rather than the symptom).
