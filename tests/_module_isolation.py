"""Helpers that keep ``sys.modules`` byte-identical across a test.

The hermes-agent suite runs per-file in its own subprocess
(``scripts/run_tests.sh`` -> ``run_tests_parallel.py``) *precisely because*
some tests force a config/module re-read by deleting entries from
``sys.modules`` (``del sys.modules["hermes_cli.config"]``). Done without a
restore, that re-import leaks a **module-identity split**: every later test
file that bound the module at import time (``import hermes_cli.kanban_db as
kb``) keeps a reference to the now-orphaned pre-purge object while lazy
imports inside it resolve to the freshly re-imported copy — silently pointing
later tests at the wrong HERMES_HOME / kanban DB. A bisect once traced 12
spurious ``test_kanban_db.py`` failures straight back to one missing restore.

Wrap the purge in :func:`preserve_sys_modules` (or request the
``restore_sys_modules`` fixture in ``tests/conftest.py``) so the original
module objects are put back on the way out — making the test order-independent
even under a single-process run.
"""

from __future__ import annotations

import contextlib
import sys
from typing import Iterator


@contextlib.contextmanager
def preserve_sys_modules() -> Iterator[None]:
    """Snapshot ``sys.modules`` and restore it exactly on exit.

    Modules imported (or re-imported) inside the ``with`` block are dropped,
    and the original objects for any name that was deleted are put back, so the
    table is identical to what it was before the block ran — no orphaned or
    duplicated module objects leak out to later tests.
    """
    snapshot = dict(sys.modules)
    try:
        yield
    finally:
        # Drop anything imported/re-imported during the block...
        for name in [n for n in sys.modules if n not in snapshot]:
            del sys.modules[name]
        # ...then restore the original objects for names purged inside it.
        sys.modules.update(snapshot)
