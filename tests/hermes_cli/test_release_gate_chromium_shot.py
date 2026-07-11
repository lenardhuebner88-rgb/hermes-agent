"""Release-gate visual-gate tool resolution.

Regression cover for the 2026-07-12 finding: the visual gate called the bare
``chromium-shot`` binary, but the release-gate activation runs in a transient
systemd unit whose PATH allowlist omits ``~/bin`` (where chromium-shot lives).
The lookup raised FileNotFoundError, the gate reported RED, and a bounded Opus
fixer was spawned to chase a phantom mobile-CSS bug. The fix resolves the tool
via PATH-then-``~/bin`` and adds ``~/bin`` to the activation unit's PATH.

Kept in a standalone module (not test_kanban_worktrees.py) so it does not
entangle with that file's concurrent foreign edits in the shared checkout.
"""

import os
from types import SimpleNamespace

from hermes_cli import kanban_worktrees as kwt


def test_resolve_chromium_shot_prefers_path(monkeypatch):
    """When chromium-shot is on PATH, use that (no assumption about ~/bin)."""
    monkeypatch.setattr(kwt.shutil, "which", lambda name: "/usr/local/bin/chromium-shot")
    assert kwt._resolve_chromium_shot() == "/usr/local/bin/chromium-shot"


def test_resolve_chromium_shot_falls_back_to_home_bin(monkeypatch):
    """When chromium-shot is NOT on PATH (the transient-unit case), fall back to
    the known ~/bin location instead of raising FileNotFoundError."""
    monkeypatch.setattr(kwt.shutil, "which", lambda name: None)
    resolved = kwt._resolve_chromium_shot()
    assert resolved == os.path.expanduser("~/bin/chromium-shot")


def test_spawn_release_gate_activation_path_includes_home_bin():
    """The transient activation unit's PATH must include ~/bin so the visual
    gate's chromium-shot (and other operator tools there) resolve inside it."""
    captured = {}

    def fake_runner(argv, **kwargs):
        captured["argv"] = list(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    kwt.spawn_release_gate_activation(
        "t_gate", runner=fake_runner, hermes_bin="/opt/hermes",
    )

    path_setenv = next(
        a for a in captured["argv"] if a.startswith("--setenv=PATH=")
    )
    assert os.path.expanduser("~/bin") in path_setenv.split("=", 2)[2].split(":")
