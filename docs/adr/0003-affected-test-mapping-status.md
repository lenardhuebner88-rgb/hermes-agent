---
status: accepted
date: 2026-07-27
---

# Make affected-test selection a fail-closed classifier

The affected-test gate classifies every changed path instead of treating an
empty list as sufficient evidence. Its path states are:

- `selected`: at least one existing test target was selected;
- `not_applicable`: the path is outside the core Python gate, is non-Python,
  is a stress scenario or Python test-support file, or is a code-free package
  marker;
- `allowlisted`: an exact, current, audited exception applies;
- `unmapped`: in-scope production Python has no selected test and no exception.

`unmapped` stops before pytest with exit 4. Exit 3 remains exclusively the
branch-age preflight (`not_run`), exit 1 remains a reproduced test failure, and
exit 5 means the complete selection exceeded its affected-test time budget.
All three pre-test holds preserve their distinct meaning; `loops/gate.sh`
continues to map exit 5 through its ordinary `GATE_FAIL` path.
exit 0 without pytest is allowed only when every changed path is
`not_applicable` or `allowlisted`. Mapper/configuration errors also stop before
pytest, but are reported as errors rather than as `unmapped`.

One pure-stdlib classifier is shared by the standalone script and the
post-merge integrator. Package fallbacks retain their existing caps: 200 in the
interactive worker mode and 800 in integration. An oversized fallback that
leaves no focused test is `unmapped` instead of being discarded after a
successful classification.

Focused `direct ∪ explicit ∪ import` unions are never truncated. Their complete,
deduplicated selection is estimated against the execution path's time budget.
The default is 1200 seconds, matching the narrower post-merge integrator gate;
`scripts/run-affected.sh` supplies 3600 seconds for the worker/loop path.
`HERMES_AFFECTED_TIME_BUDGET` can override either value.

The estimator reads `test_durations.json` but accepts only finite,
non-negative, repository-relative `tests/…` entries. Cache values are treated
as serial per-file subprocess seconds. The loaded wall estimate is
`max(serial sum / HERMES_TEST_WORKERS, slowest file) × 3.5`, where the default
worker count is 8 and 3.5 represents the measured 3.3–3.6× shared-host
dilation. A selected file without a forecast receives a conservative 60-second
estimate and is counted explicitly. A missing or corrupt cache does not block
the gate: the full selection remains intact and the standalone mapper prints a
visible note that the budget check was skipped.

An over-budget selection stops fail-closed before pytest with exit 5. Its
message includes predicted loaded wall time, budget, selected-file count,
missing-forecast count, and the five most expensive estimated files. The
former 217-file cap was removed because it was backward-derived from one
measured selection and then alphabetically discarded real coverage. After A2,
the fixed file count was also meaningless as a runtime metric—even though a
real `gateway/config.py` case still exceeded the numeric cap—because the
remaining selections vary materially in per-file cost.

Explicit patterns are additive precision hints, not coverage filters. For a
production Python path their existing targets are unioned with mirrored direct
tests and imports discovered from the test suite's Python AST. Imports written
only inside docstrings or fixture strings are not test evidence. Deleted
production paths still select surviving direct or importing tests, but become
`not_applicable` rather than `unmapped` when no such test survives.

Stress-registry scenarios are excluded from the normal pytest import index in
both directions. **Accepted limit — test-support-only diffs.** Python support
files under `tests/` that are not themselves `test_*.py` are deliberately
`not_applicable` in both modes, so a diff containing only `tests/conftest.py` or
a shared test helper can exit 0 without running pytest. The nightly full suite
is the fallback for this named boundary of the affected gate.

A fail-closed test-support fallback was tried and rejected. The global
`tests/conftest.py` scope exceeded both caps with no legal exception path;
package-level support scopes produced different worker and integration states;
and test-free support directories selected a directory that the parallel runner
reported as "No test files to run" with exit 1. Those outcomes made ordinary
test-infrastructure changes permanently unlandable or falsely red. The nightly
full suite is the explicit backstop for this accepted limitation. A future
replacement needs its own escape and execution contract; it must not be
reintroduced as another directory fallback inside this classifier.

The accepted repository census covers tracked Hermes core/runtime, plugins,
gateways, tools, loops, operational scripts and nested test files. Untracked
slice work is classified when it is part of the current ref-less diff, but is
not allowed to leak into the repository-wide census. Existing untracked tests
may still provide path-local evidence for the slice that is actively creating
them. Procedural skill
catalog payloads (`skills/`, `optional-skills/`), documentation render helpers
and website-generation helpers are `not_applicable` to this core pytest gate;
their own workflows remain separate. Empty or docstring-only `__init__.py`
files are content-sensitive package markers: adding executable code makes them
in-scope immediately. Nested `tests/` directories are test scope, not
production scope.

Exceptions live in `config/affected-test-exceptions.json`. Entries are exact
paths only—no globs or directory prefixes—and require a reason, owner, area,
disposition and review date. Invalid or duplicate entries are configuration
errors. Stale and expired entries are ignored with a path-local warning, so
the affected path becomes `unmapped` instead of making every repository path
unclassifiable. An active exception applies before the mode-dependent package
fallback in both modes; explicit, direct, or import-index coverage still makes
that exception a configuration conflict. The initial production exception list
is empty; current gaps are resolved by real mappings or by tested
`not_applicable` rules, not by bulk-grandfathering the old blind set.

The repository contract is behavioral:

- the accepted inventory has zero `unmapped` paths in both modes;
- a synthetic new production path without coverage becomes `unmapped`;
- gate-control files select their own contract tests;
- worker and integration callers return the same classification for the same
  mode;
- all automatic consumers preserve the distinct `unmapped` state.

This trades false-green convenience for an explicit hold that names the path
and the missing evidence. The nightly full suite remains a backstop, not a
substitute for evidence on the changed production path.
