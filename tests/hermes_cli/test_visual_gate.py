from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hermes_cli import kanban_worktrees as kwt


def test_visual_gate_enabled_default_config_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_VISUAL_GATE", raising=False)

    assert kwt.visual_gate_enabled() is False

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("kanban:\n  visual_gate: true\n", encoding="utf-8")
    assert kwt.visual_gate_enabled() is True

    cfg_path.write_text("kanban:\n  visual_gate: false\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_VISUAL_GATE", "1")
    assert kwt.visual_gate_enabled() is True


def test_visual_gate_max_retries_default_clamp_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_VISUAL_GATE_MAX_RETRIES", raising=False)

    assert kwt.visual_gate_max_retries() == 3

    (tmp_path / "config.yaml").write_text(
        "kanban:\n  visual_gate_max_retries: 9\n",
        encoding="utf-8",
    )
    assert kwt.visual_gate_max_retries() == 5

    monkeypatch.setenv("HERMES_KANBAN_VISUAL_GATE_MAX_RETRIES", "2")
    assert kwt.visual_gate_max_retries() == 2


def test_default_quick_gate_visual_gate_control_only_failure_notes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "web").mkdir()
    calls: list[Path] = []

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_visual_gate(root, screenshots_dir):
        calls.append(Path(root))
        assert Path(screenshots_dir) == kwt._VISUAL_GATE_SCREENSHOTS_ROOT
        return "visual-gate: overflow after focus"

    monkeypatch.setattr(kwt, "visual_gate_enabled", lambda: True)
    monkeypatch.setattr(kwt, "_affected_pytest_modules", lambda root, changed: [])
    monkeypatch.setattr(kwt, "_resolve_node_bin", lambda root, name: Path(f"/bin/{name}"))
    monkeypatch.setattr(kwt.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)
    monkeypatch.setattr(kwt, "_run_visual_gate", fake_visual_gate)

    ok, detail = kwt.default_quick_gate(repo, ["web/vite.config.ts"])
    assert ok is True
    assert calls == []

    ok, detail = kwt.default_quick_gate(repo, ["web/src/control/App.tsx"])
    assert ok is False
    assert calls == [repo]
    assert "overflow after focus" in detail
    assert "mobile-IME physically unverified" in detail
