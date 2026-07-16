"""Test that skills subparser doesn't conflict (regression test for #898)."""

import argparse
import sys

import pytest


@pytest.fixture(autouse=True)
def _restore_sys_modules_after_purge(restore_sys_modules):
    """Keep the hermes_cli.main purge from leaking into later files.

    The test below deletes ``hermes_cli.main`` and re-imports it so argparse
    subparser registration runs fresh. Without a restore, later files in the
    same worker keep import-time bindings to the orphaned reimport
    (module-identity split) — same class as the empty-tool-name /
    verification-stop-caching leaks (see ``tests/_module_isolation.py``).
    ``restore_sys_modules`` snapshots before the test body and puts the table
    back on teardown.
    """
    yield


def test_no_duplicate_skills_subparser():
    """Ensure 'skills' subparser is only registered once to avoid Python 3.11+ crash.

    Python 3.11 changed argparse to raise an exception on duplicate subparser
    names instead of silently overwriting (see CPython #94331).

    This test will fail with:
        argparse.ArgumentError: argument command: conflicting subparser: skills

    if the duplicate 'skills' registration is reintroduced.
    """
    # Force fresh import of the module where parser is constructed
    # If there are duplicate 'skills' subparsers, this import will raise
    # argparse.ArgumentError at module load time

    # Remove cached module if present
    if 'hermes_cli.main' in sys.modules:
        del sys.modules['hermes_cli.main']

    try:
        import hermes_cli.main  # noqa: F401
    except argparse.ArgumentError as e:
        if "conflicting subparser" in str(e):
            raise AssertionError(
                f"Duplicate subparser detected: {e}. "
                "See issue #898 for details."
            ) from e
        raise
