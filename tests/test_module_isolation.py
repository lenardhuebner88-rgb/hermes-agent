"""Regression test for the parent-package-attribute leak in
``preserve_sys_modules()`` (see ``tests/_module_isolation.py``).

Purging + re-importing a dotted submodule inside the ``with`` block leaves
two traces on ``sys.modules``: the dict entry ``sys.modules["pkg.sub"]``
*and*, as an import-machinery side effect, the attribute ``pkg.sub`` on the
live parent-package object. The original implementation only restored the
former. ``import pkg.sub as m`` (attribute lookup) then kept resolving to the
orphaned reimport while ``sys.modules["pkg.sub"]`` (dict lookup) resolved to
the restored original -- a module-identity split identical in shape to the
one this helper exists to prevent in the first place.

Uses a real on-disk two-module package via ``tmp_path`` + ``sys.path`` so the
regular import machinery does the reimporting -- nothing here is mocked.
"""

from __future__ import annotations

import sys

import pytest

from tests._module_isolation import preserve_sys_modules


@pytest.fixture()
def tmp_package(tmp_path, monkeypatch):
    """A real, importable ``pkgiso.sub`` package on disk, purged from
    ``sys.modules`` again on teardown so it never leaks into later tests."""
    pkg_dir = tmp_path / "pkgiso"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "sub.py").write_text("VALUE = 'original'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield "pkgiso", "pkgiso.sub"
    for name in ("pkgiso", "pkgiso.sub"):
        sys.modules.pop(name, None)


def test_preserve_sys_modules_restores_parent_package_attribute(tmp_package):
    """After the block, both the dict entry AND the parent package's
    attribute must point at the original submodule object -- not just one
    or the other."""
    pkg_name, sub_name = tmp_package
    import pkgiso.sub  # noqa: F401  establish the parent-attribute link

    original = sys.modules[sub_name]
    assert getattr(sys.modules[pkg_name], "sub") is original

    with preserve_sys_modules():
        del sys.modules[sub_name]
        import pkgiso.sub  # noqa: F401,F811  reimport -> new module object
        assert sys.modules[sub_name] is not original
        assert getattr(sys.modules[pkg_name], "sub") is not original

    restored = sys.modules[sub_name]
    assert restored is original
    assert getattr(sys.modules[pkg_name], "sub") is restored
