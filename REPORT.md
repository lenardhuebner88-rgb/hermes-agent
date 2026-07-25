# Affected-test mapper nested-package fix

## Result

- Both mapper copies now scan the existing parent chain from
  `tests/<source package>/` up to `tests/`, while retaining
  `_imports_changed_module` as the per-file filter.
- `hermes_cli/dashboard_auth/middleware.py` selects these real importers in
  both mappers:
  - `tests/hermes_cli/test_dashboard_auth_401_reauth.py`
  - `tests/hermes_cli/test_dashboard_auth_middleware.py`
  - `tests/hermes_cli/test_web_server.py`
- The integration mapper now carries the standalone mapper's maintained
  monolith mappings, so the two gates also agree for `hermes_cli/kanban_db.py`.
- The package fallback cap is 200 in both Python mappers.
  `scripts/affected-tests.sh` reads the standalone Python constant through
  `--fallback-max-test-files` and no longer contains a second numeric literal.
- Fail-open behavior is unchanged. The existing docs/non-Python test remains
  green and still selects no pytest modules.

## Coverage measurement

The same population as the brief was measured from `git ls-files '*.py'`,
excluding paths under `tests/`. Each source was passed independently through
`scripts/affected_tests.py:affected_pytest_modules`.

```text
tracked_non_test_py=1175
select_zero=393
```

The zero-selection count dropped from 417 to 393.

## Mandatory control probe

Method: temporarily restore the original one-directory
`_feature_named_sibling_tests` body in both mapper files, run the new acceptance
file, capture the failure, then restore both fixed parent-chain bodies.

Command:

```bash
$VENV/python -m pytest tests/scripts/test_affected_mapper_equivalence.py -q
```

Exit code: 1

Red output:

```text
F....                                                                    [100%]
=================================== FAILURES ===================================
_ test_dashboard_auth_middleware_selects_real_dedicated_tests_for_both_mappers _

    def test_dashboard_auth_middleware_selects_real_dedicated_tests_for_both_mappers():
        expected = [
            "tests/hermes_cli/test_dashboard_auth_401_reauth.py",
            "tests/hermes_cli/test_dashboard_auth_middleware.py",
            "tests/hermes_cli/test_web_server.py",
        ]

        standalone, integration = _selections(
            ["hermes_cli/dashboard_auth/middleware.py"]
        )

>       assert standalone == expected
E       AssertionError: assert [] == ['tests/herme...eb_server.py']
E
E         Right contains 3 more items, first extra item: 'tests/hermes_cli/test_dashboard_auth_401_reauth.py'
E         Use -v to get more diff

tests/scripts/test_affected_mapper_equivalence.py:45: AssertionError
=========================== short test summary info ============================
FAILED tests/scripts/test_affected_mapper_equivalence.py::test_dashboard_auth_middleware_selects_real_dedicated_tests_for_both_mappers
1 failed, 4 passed in 0.56s
```

After restoring the fix, the same acceptance file passed: `5 passed`.

## Mandatory gates

Run from `/home/piet/.hermes/worktrees/claude-lever1-mapper` with:

```bash
export PYTHONPATH=$(pwd)
VENV=/home/piet/.hermes/hermes-agent/venv/bin
$VENV/python -m pytest tests/scripts/test_affected_tests.py -q
$VENV/python -m pytest tests/hermes_cli/test_chain_worktree_serialization.py -q
$VENV/python -m pytest tests/scripts/test_affected_mapper_equivalence.py -q
$VENV/ruff check hermes_cli/kanban_worktrees.py scripts/affected_tests.py tests/scripts/test_affected_mapper_equivalence.py
```

Final outputs and exit codes:

```text
13 passed in 4.25s
GATE_EXIT test_affected_tests.py=0

11 passed in 3.12s
GATE_EXIT test_chain_worktree_serialization.py=0

5 passed in 2.01s
GATE_EXIT test_affected_mapper_equivalence.py=0

All checks passed!
GATE_EXIT ruff=0
```

Each pytest file ran in its own invocation. No gate output was piped.

## Scope

No changes were made to `hermes_cli/kanban_db.py`, `docs/`, review-gate
configuration, live data, services, the live checkout, or `main`. No push,
merge, deploy, or restart was performed.
