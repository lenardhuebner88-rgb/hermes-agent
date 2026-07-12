"""Regression: NUL bytes in task body/comments must not fail the worker spawn.

Root cause (t_881ddc6b, comment 4540): a reviewer-authored comment stored
mangled German umlauts as embedded NUL (``\\x00fc`` instead of ``ü``). That
comment flows into the assembled ``claude -p <prompt>`` argv in
``_spawn_claude_worker``, and ``subprocess.Popen`` raises ``ValueError:
embedded null byte`` on any argv string containing ``\\x00`` — failing the
spawn (``most_recent_outcome=spawn_failed | last_error=embedded null byte``)
and eventually tripping the failure-limit block. ANY corrupted comment or
context string can trigger this fleet-wide.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _import_kanban_db():
    try:
        from hermes_cli import kanban_db as kb

        return kb
    except Exception as exc:  # pragma: no cover - import guard
        pytest.skip(f"Cannot import hermes_cli.kanban_db: {exc}")


# ---------------------------------------------------------------------------
# Unit tests: the pure sanitizer helper
# ---------------------------------------------------------------------------


class TestSanitizeSpawnText:
    def test_strips_embedded_nul_and_c0_controls(self):
        kb = _import_kanban_db()
        raw = "Fix the \x00fc umlaut and \x01 stray control char"
        cleaned = kb._sanitize_spawn_text(raw)

        assert "\x00" not in cleaned
        assert "\x01" not in cleaned
        # NUL is stripped (not replaced), so the surrounding text still reads:
        assert cleaned == "Fix the fc umlaut and  stray control char"

    def test_preserves_tab_newline_carriage_return(self):
        kb = _import_kanban_db()
        raw = "line1\tindented\nline2\r\nline3"
        assert kb._sanitize_spawn_text(raw) == raw

    def test_preserves_normal_unicode(self):
        kb = _import_kanban_db()
        raw = "Der Preis ist 100€, Grüße, café"
        assert kb._sanitize_spawn_text(raw) == raw

    def test_empty_and_none_safe(self):
        kb = _import_kanban_db()
        assert kb._sanitize_spawn_text("") == ""
        assert kb._sanitize_spawn_text(None) is None

    def test_sanitized_argv_does_not_raise_embedded_null_byte(self):
        """The exact failure mode from t_881ddc6b: a NUL in an argv string
        makes subprocess.Popen raise ValueError. Sanitizing first avoids it.
        """
        import subprocess

        kb = _import_kanban_db()
        raw = "Task body with a corrupted comment: \x00fc and \x01 noise, € ok"

        with pytest.raises(ValueError, match="embedded null byte"):
            subprocess.Popen(["true", raw], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        cleaned = kb._sanitize_spawn_text(raw)
        proc = subprocess.Popen(["true", cleaned], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait(timeout=5)
        assert "€" in cleaned  # normal unicode untouched


# ---------------------------------------------------------------------------
# Integration: a NUL-carrying task body must not fail the claude-cli spawn
# ---------------------------------------------------------------------------


class TestSpawnClaudeWorkerToleratesCorruptedBody:
    def _make_task(self, tmp_path, kb, *, body):
        return kb.Task(
            id="t_881ddc6b",
            title="reviewer left a mangled comment",
            body=body,
            assignee="coder",
            status="ready",
            priority=0,
            created_by=None,
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "ws"),
            claim_lock=None,
            claim_expires=None,
            tenant=None,
            branch_name="wt/t_881ddc6b",
            model_override=None,
        )

    def test_nul_byte_in_task_body_does_not_break_spawn_argv(
        self, tmp_path, monkeypatch
    ):
        kb = _import_kanban_db()

        default_home = tmp_path / ".hermes"
        default_home.mkdir(exist_ok=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(default_home))
        monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)

        # Mimic real subprocess.Popen's fail mode instead of a no-op stub, so
        # this test actually exercises the embedded-null-byte failure path.
        def _real_popen_semantics(cmd, **kwargs):
            for arg in cmd:
                if isinstance(arg, str) and "\x00" in arg:
                    raise ValueError("embedded null byte")

            class _FakeProc:
                pid = 9911

            return _FakeProc()

        monkeypatch.setattr("subprocess.Popen", _real_popen_semantics)
        monkeypatch.setenv("HERMES_CLAUDE_BIN", "/usr/local/bin/claude-test")

        task = self._make_task(
            tmp_path, kb, body="Fix the \x00fc umlaut, \x01 stray control, €100 ok"
        )
        env = {"HERMES_HOME": str(default_home)}

        pid = kb._spawn_claude_worker(task, str(tmp_path / "ws"), env=env, board=None)

        assert pid == 9911
