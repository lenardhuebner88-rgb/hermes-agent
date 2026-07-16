from __future__ import annotations

import shlex
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_worktrees as kwt


@pytest.fixture
def validation_worktree(tmp_path):
    """Validation worktree with production-shaped shared dependency links."""
    source_root = Path(__file__).resolve().parents[2]
    live_root = tmp_path / "live-checkout"
    validation_root = (
        live_root / kwt.WORKTREES_DIRNAME / "kanban-validation" / "release-1"
    )
    for root in (live_root, validation_root):
        (root / "web").mkdir(parents=True)

    # Use the repository's real npm workspace manifests/lockfile. The test
    # never invokes npm, but the validation fixture matches its live format.
    shutil.copy2(source_root / "package.json", validation_root / "package.json")
    shutil.copy2(
        source_root / "package-lock.json",
        validation_root / "package-lock.json",
    )
    shutil.copy2(
        source_root / "web" / "package.json",
        validation_root / "web" / "package.json",
    )

    shared_root_modules = live_root / "node_modules"
    shared_web_modules = live_root / "web" / "node_modules"
    (shared_root_modules / "hermes-agent").mkdir(parents=True)
    (shared_web_modules / "web").mkdir(parents=True)
    shutil.copy2(
        source_root / "package.json",
        shared_root_modules / "hermes-agent" / "package.json",
    )
    shutil.copy2(
        source_root / "web" / "package.json",
        shared_web_modules / "web" / "package.json",
    )

    kwt._link_shared_dependencies(live_root, validation_root)
    assert (validation_root / "node_modules").is_symlink()
    assert (validation_root / "web" / "node_modules").is_symlink()
    return validation_root, shared_root_modules, shared_web_modules


def test_release_runner_keeps_healthy_toolchain_command_byte_identical(
    validation_worktree, monkeypatch,
):
    validation_root, _shared_root, shared_web = validation_worktree
    tsc = shared_web / ".bin" / "tsc"
    tsc.parent.mkdir()
    tsc.write_text("#!/bin/sh\n", encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return SimpleNamespace(returncode=0, stdout="build ok", stderr="")

    monkeypatch.setattr(kwt.subprocess, "run", fake_run)
    monkeypatch.setattr(kwt, "visual_gate_enabled", lambda: False)

    ok, detail = kwt._default_release_gate_runner(repo_root=validation_root)

    quoted_root = shlex.quote(str(validation_root))
    assert ok is True
    assert detail == "build ok"
    assert calls == [
        (
            [
                "bash",
                "-c",
                f"cd {quoted_root}/web && npm run build && "
                f"test -f {quoted_root}/hermes_cli/web_dist/index.html",
            ],
            {
                "cwd": str(validation_root),
                "capture_output": True,
                "text": True,
                "timeout": kwt.RELEASE_GATE_COMMAND_TIMEOUT,
            },
        ),
    ]


def test_release_runner_replaces_shared_links_before_private_npm_ci(
    validation_worktree, monkeypatch,
):
    validation_root, shared_root, shared_web = validation_worktree
    shared_root_manifest = (shared_root / "hermes-agent" / "package.json").read_bytes()
    shared_web_manifest = (shared_web / "web" / "package.json").read_bytes()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs["cwd"]))
        if argv == ["/usr/bin/npm", "ci"]:
            assert Path(kwargs["cwd"]).resolve() == validation_root.resolve()
            for rel, foreign_target in (
                ("node_modules", shared_root),
                ("web/node_modules", shared_web),
            ):
                private_modules = validation_root / rel
                assert not private_modules.is_symlink()
                assert private_modules.is_dir()
                assert private_modules.resolve() != foreign_target.resolve()
            assert (
                shared_root / "hermes-agent" / "package.json"
            ).read_bytes() == shared_root_manifest
            assert (
                shared_web / "web" / "package.json"
            ).read_bytes() == shared_web_manifest
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(kwt.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)
    monkeypatch.setattr(kwt, "visual_gate_enabled", lambda: False)

    ok, _detail = kwt._default_release_gate_runner(repo_root=validation_root)

    assert ok is True
    assert len(calls) == 2
    assert calls[0][0] == ["/usr/bin/npm", "ci"]
    assert calls[1][0][:2] == ["bash", "-c"]
    assert "npm run build" in calls[1][0][2]
    assert calls[0][1] == str(validation_root)
    assert calls[1][1] == str(validation_root)
    assert (shared_root / "hermes-agent" / "package.json").read_bytes() == (
        shared_root_manifest
    )
    assert (shared_web / "web" / "package.json").read_bytes() == (
        shared_web_manifest
    )


def test_release_runner_fails_closed_when_private_npm_ci_fails(
    validation_worktree, monkeypatch,
):
    validation_root, _shared_root, _shared_web = validation_worktree
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs["cwd"]))
        assert argv == ["/usr/bin/npm", "ci"]
        return SimpleNamespace(
            returncode=37,
            stdout="",
            stderr="npm error lockfile install failed",
        )

    monkeypatch.setattr(kwt.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    ok, detail = kwt._default_release_gate_runner(repo_root=validation_root)

    assert ok is False
    assert detail.startswith("release-toolchain:")
    assert "npm ci" in detail
    assert "exit 37" in detail
    assert calls == [(["/usr/bin/npm", "ci"], str(validation_root))]


def test_release_runner_never_repairs_live_checkout(
    validation_worktree, monkeypatch,
):
    validation_root, _shared_root, _shared_web = validation_worktree
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs["cwd"]))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(kwt, "LIVE_CHECKOUT_ROOT", validation_root)
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    ok, detail = kwt._default_release_gate_runner(repo_root=validation_root)

    assert ok is False
    assert detail.startswith("release-toolchain:")
    assert "live checkout" in detail
    assert calls == []
