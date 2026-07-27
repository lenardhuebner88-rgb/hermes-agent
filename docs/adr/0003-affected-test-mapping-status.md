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
exit 0 without pytest is allowed only when every changed path is
`not_applicable` or `allowlisted`. Mapper/configuration errors also stop before
pytest, but are reported as errors rather than as `unmapped`.

One pure-stdlib classifier is shared by the standalone script and the
post-merge integrator. The interactive worker mode applies a package-fallback
cap of 200; integration applies 800. The cap is part of classification, so an
oversized fallback that leaves no focused test is `unmapped` instead of being
discarded after a successful classification. These are deliberately fallback
caps, not caps on direct, explicit, or import-index evidence; truncating real
evidence would recreate a false-green coverage filter.

Explicit patterns are additive precision hints, not coverage filters. For a
production Python path their existing targets are unioned with mirrored direct
tests and imports discovered from the test suite's Python AST. Imports written
only inside docstrings or fixture strings are not test evidence. Deleted
production paths still select surviving direct or importing tests, but become
`not_applicable` rather than `unmapped` when no such test survives.

Stress-registry scenarios are excluded from the normal pytest import index in
both directions. Python support files under `tests/` that are not themselves
`test_*.py` are deliberately `not_applicable` in both modes. This is a named
boundary of the affected gate: a diff containing only `tests/conftest.py` or a
shared test helper can therefore exit 0 without running pytest.

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
