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

    Restoring the ``sys.modules`` dict entry isn't the whole story for a
    dotted name though: importing (or re-importing) ``pkg.leaf`` also sets
    the attribute ``leaf`` on the live ``pkg`` module object as a side
    effect. That attribute isn't touched by the dict-level restore above, so
    a purge+reimport inside the block leaves it pointing at the orphaned
    reimport — ``import pkg.leaf as m`` (attribute lookup) and
    ``sys.modules["pkg.leaf"]`` (dict lookup) would then silently resolve to
    two different module objects. Snapshot and restore those parent-package
    attributes too.
    """
    snapshot = dict(sys.modules)
    parent_attrs = {}
    for name in snapshot:
        parent_name, sep, leaf = name.rpartition(".")
        if sep and parent_name in snapshot:
            parent = snapshot[parent_name]
            parent_attrs[name] = (parent, leaf, hasattr(parent, leaf), getattr(parent, leaf, None))
    try:
        yield
    finally:
        # Drop anything imported/re-imported during the block...
        purged = [n for n in sys.modules if n not in snapshot]
        for name in purged:
            del sys.modules[name]
        # ...then restore the original objects for names purged inside it.
        sys.modules.update(snapshot)
        # ...and undo the parent-package attribute the block's imports left
        # behind, for every dotted name whose sys.modules entry above was
        # either dropped or put back.
        for name in set(parent_attrs) | {n for n in purged if "." in n}:
            parent_name, _, leaf = name.rpartition(".")
            parent = snapshot.get(parent_name)
            if parent is None:
                continue
            if name in parent_attrs:
                _, _, had, value = parent_attrs[name]
            else:
                had, value = False, None
            if had:
                setattr(parent, leaf, value)
            elif hasattr(parent, leaf):
                delattr(parent, leaf)
