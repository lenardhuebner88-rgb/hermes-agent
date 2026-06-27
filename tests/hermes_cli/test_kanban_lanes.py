"""Tests for lane presets (night-sprint F1).

A lane is a named profile→(worker_runtime, model) preset stored in the board
DB; the dispatcher hot-reads the ACTIVE lane at every spawn. Precedence:

    task.model_override > active lane > profile config.yaml default
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_CLAUDE_CLI_PROFILES", raising=False)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Seeding + CRUD
# ---------------------------------------------------------------------------


class TestLaneCrud:
    def test_first_list_seeds_two_builtin_presets(self, kanban_home):
        with kb.connect() as conn:
            lanes = kb.list_lanes(conn)
        names = {l["name"] for l in lanes}
        assert names == {"api-standard", "max-abo"}
        assert all(l["builtin"] for l in lanes)
        # Exactly one active, and it is api-standard (behavior-neutral seed).
        active = [l for l in lanes if l["active"]]
        assert len(active) == 1
        assert active[0]["name"] == "api-standard"
        # max-abo maps its profiles onto the claude CLI runtime.
        max_abo = next(l for l in lanes if l["name"] == "max-abo")
        assert max_abo["profiles"]["premium"]["worker_runtime"] == "claude-cli"
        assert max_abo["profiles"]["premium"]["model"] == "claude-opus-4-8"
        assert max_abo["profiles"]["coder-claude"]["model"] == "claude-opus-4-8"

    def test_seeding_is_idempotent(self, kanban_home):
        with kb.connect() as conn:
            first = kb.list_lanes(conn)
            second = kb.list_lanes(conn)
        assert [l["id"] for l in first] == [l["id"] for l in second]

    def test_create_update_delete_roundtrip(self, kanban_home):
        with kb.connect() as conn:
            kb.list_lanes(conn)
            lane = kb.create_lane(
                conn,
                name="nacht",
                profiles={
                    "coder": {
                        "worker_runtime": "hermes",
                        "provider": "openrouter",
                        "model": "qwen/qwen3.7-max",
                        "fallback_providers": [
                            {"provider": "openai-codex", "model": "gpt-5.5"},
                        ],
                    },
                },
            )
            assert lane["active"] is False
            assert lane["profiles"]["coder"]["provider"] == "openrouter"
            assert lane["profiles"]["coder"]["model"] == "qwen/qwen3.7-max"
            assert lane["profiles"]["coder"]["fallback_providers"] == [
                {"provider": "openai-codex", "model": "gpt-5.5"},
            ]

            lane = kb.update_lane(
                conn, lane["id"],
                name="nacht-2",
                profiles={"coder": {"worker_runtime": "hermes", "model": None}},
            )
            assert lane["name"] == "nacht-2"
            assert lane["profiles"]["coder"]["worker_runtime"] == "hermes"
            assert lane["profiles"]["coder"]["model"] is None

            assert kb.delete_lane(conn, lane["id"]) is True
            assert kb.delete_lane(conn, lane["id"]) is False  # already gone

    def test_create_duplicate_name_raises(self, kanban_home):
        with kb.connect() as conn:
            kb.create_lane(conn, name="dup", profiles={})
            with pytest.raises(ValueError, match="already exists"):
                kb.create_lane(conn, name="dup", profiles={})

    def test_invalid_runtime_raises(self, kanban_home):
        with kb.connect() as conn:
            with pytest.raises(ValueError, match="worker_runtime"):
                kb.create_lane(
                    conn, name="bad",
                    profiles={"coder": {"worker_runtime": "warp-drive"}},
                )

    def test_invalid_fallback_entry_raises(self, kanban_home):
        with kb.connect() as conn:
            with pytest.raises(ValueError, match="fallback_providers\\[0\\]\\.provider"):
                kb.create_lane(
                    conn,
                    name="bad-fallback",
                    profiles={
                        "coder": {
                            "worker_runtime": "hermes",
                            "provider": "openrouter",
                            "model": "qwen/qwen3.7-max",
                            "fallback_providers": [{"model": "gpt-5.5"}],
                        },
                    },
                )

    def test_claude_cli_fallback_edit_rejected(self, kanban_home):
        with kb.connect() as conn:
            with pytest.raises(ValueError, match="claude-cli"):
                kb.create_lane(
                    conn,
                    name="bad-claude-fallback",
                    profiles={
                        "premium": {
                            "worker_runtime": "claude-cli",
                            "model": "claude-opus-4-8",
                            "fallback_providers": [{"provider": "openai-codex", "model": "gpt-5.5"}],
                        },
                    },
                )

    def test_active_lane_not_deletable(self, kanban_home):
        with kb.connect() as conn:
            lanes = kb.list_lanes(conn)
            active = next(l for l in lanes if l["active"])
            with pytest.raises(ValueError, match="active lane"):
                kb.delete_lane(conn, active["id"])

    def test_activate_switches_exactly_one_active(self, kanban_home):
        with kb.connect() as conn:
            lanes = kb.list_lanes(conn)
            max_abo = next(l for l in lanes if l["name"] == "max-abo")
            out = kb.activate_lane(conn, max_abo["id"])
            assert out["active"] is True
            lanes = kb.list_lanes(conn)
            active = [l for l in lanes if l["active"]]
            assert [l["name"] for l in active] == ["max-abo"]
            assert kb.get_active_lane(conn)["name"] == "max-abo"

    def test_activate_unknown_lane_returns_none(self, kanban_home):
        with kb.connect() as conn:
            assert kb.activate_lane(conn, "lane_nope") is None


# ---------------------------------------------------------------------------
# Spawn-time lane resolution (the 5+ contract scenarios)
# ---------------------------------------------------------------------------


class TestLaneSpawnResolution:
    """End-to-end through ``_default_spawn`` with a faked Popen: asserts the
    actual worker argv under every precedence combination."""

    def _make_task(self, tmp_path, *, assignee="coder", model_override=None):
        return kb.Task(
            id="t_lane",
            title="ship the widget",
            body="implement the widget",
            assignee=assignee,
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
            branch_name="wt/t_lane",
            model_override=model_override,
        )

    def _spawn(self, kanban_home, tmp_path, monkeypatch, *,
               assignee="coder", model_override=None,
               profile_config=None, lane_profiles=None):
        """Run _default_spawn with optional profile config + active lane.

        Returns the captured argv.
        """
        if profile_config is not None:
            (kanban_home / "config.yaml").write_text(profile_config, encoding="utf-8")
        if lane_profiles is not None:
            with kb.connect() as conn:
                lane = kb.create_lane(conn, name=f"test-lane-{assignee}", profiles=lane_profiles)
                kb.activate_lane(conn, lane["id"])

        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                self.pid = 4242

        monkeypatch.setattr("subprocess.Popen", _FakePopen)
        monkeypatch.setenv("HERMES_CLAUDE_BIN", "/usr/local/bin/claude-test")

        task = self._make_task(tmp_path, assignee=assignee, model_override=model_override)
        kb._default_spawn(task, str(tmp_path / "ws"))
        return captured["cmd"]

    def _claude_model_of(self, cmd):
        assert cmd[0] == "/usr/local/bin/claude-test", f"not the claude path: {cmd[:3]}"
        if "--model" not in cmd:
            return None
        return cmd[cmd.index("--model") + 1]

    def _claude_disallowed_tools(self, cmd):
        assert cmd[0] == "/usr/local/bin/claude-test", f"not the claude path: {cmd[:3]}"
        assert "--disallowedTools" in cmd, f"--disallowedTools missing: {cmd}"
        return set(cmd[cmd.index("--disallowedTools") + 1].split(","))

    def test_claude_verdict_lanes_get_read_only_cage(
        self, kanban_home, tmp_path, monkeypatch,
    ):
        lane_profiles = {
            "reviewer": {"worker_runtime": "claude-cli", "model": "claude-fable-5"},
            "critic": {"worker_runtime": "claude-cli", "model": "claude-fable-5"},
        }

        reviewer_cmd = self._spawn(
            kanban_home, tmp_path, monkeypatch,
            assignee="reviewer",
            lane_profiles=lane_profiles,
        )
        critic_cmd = self._spawn(
            kanban_home, tmp_path, monkeypatch,
            assignee="critic",
            lane_profiles=lane_profiles,
        )

        expected = {
            "WebFetch", "WebSearch", "Edit", "Write", "MultiEdit",
            "NotebookEdit", "Task", "Agent",
        }
        assert expected <= self._claude_disallowed_tools(reviewer_cmd)
        assert expected <= self._claude_disallowed_tools(critic_cmd)

    def test_claude_coder_and_premium_lanes_keep_full_tools(
        self, kanban_home, tmp_path, monkeypatch,
    ):
        lane_profiles = {
            "coder": {"worker_runtime": "claude-cli", "model": "claude-fable-5"},
            "premium": {"worker_runtime": "claude-cli", "model": "claude-fable-5"},
        }

        coder_cmd = self._spawn(
            kanban_home, tmp_path, monkeypatch,
            assignee="coder",
            lane_profiles=lane_profiles,
        )
        premium_cmd = self._spawn(
            kanban_home, tmp_path, monkeypatch,
            assignee="premium",
            lane_profiles=lane_profiles,
        )

        read_only_only = {
            "Edit", "Write", "MultiEdit", "NotebookEdit", "Task", "Agent",
        }
        assert self._claude_disallowed_tools(coder_cmd) == {"WebFetch", "WebSearch"}
        assert self._claude_disallowed_tools(premium_cmd) == {"WebFetch", "WebSearch"}
        assert self._claude_disallowed_tools(coder_cmd).isdisjoint(read_only_only)
        assert self._claude_disallowed_tools(premium_cmd).isdisjoint(read_only_only)

    # 1. No lane at all → profile config default (pre-lane behavior).
    def test_no_lane_falls_back_to_profile_claude_model(
        self, kanban_home, tmp_path, monkeypatch,
    ):
        cmd = self._spawn(
            kanban_home, tmp_path, monkeypatch,
            profile_config="worker_runtime: claude-cli\nclaude_model: claude-opus-4-8\n",
        )
        assert self._claude_model_of(cmd) == "claude-opus-4-8"

    # 2. Active lane pins runtime+model for a profile with NO config flag.
    def test_lane_routes_profile_to_claude_cli_with_model(
        self, kanban_home, tmp_path, monkeypatch,
    ):
        cmd = self._spawn(
            kanban_home, tmp_path, monkeypatch,
            lane_profiles={"coder": {"worker_runtime": "claude-cli", "model": "claude-fable-5"}},
        )
        assert self._claude_model_of(cmd) == "claude-fable-5"

    # 3. task.model_override beats the lane model.
    def test_task_override_beats_lane_model(
        self, kanban_home, tmp_path, monkeypatch,
    ):
        cmd = self._spawn(
            kanban_home, tmp_path, monkeypatch,
            model_override="claude-opus-4-8",
            lane_profiles={"coder": {"worker_runtime": "claude-cli", "model": "claude-fable-5"}},
        )
        assert self._claude_model_of(cmd) == "claude-opus-4-8"

    # 4. Lane model beats the profile's claude_model (middle tier order).
    def test_lane_model_beats_profile_claude_model(
        self, kanban_home, tmp_path, monkeypatch,
    ):
        cmd = self._spawn(
            kanban_home, tmp_path, monkeypatch,
            profile_config="worker_runtime: claude-cli\nclaude_model: claude-opus-4-8\n",
            lane_profiles={"coder": {"worker_runtime": "claude-cli", "model": "claude-fable-5"}},
        )
        assert self._claude_model_of(cmd) == "claude-fable-5"

    # 5. Lane runtime 'hermes' overrides a claude-cli profile config (both
    #    directions of the runtime switch work).
    def test_lane_runtime_hermes_overrides_claude_cli_config(
        self, kanban_home, tmp_path, monkeypatch,
    ):
        cmd = self._spawn(
            kanban_home, tmp_path, monkeypatch,
            profile_config="worker_runtime: claude-cli\nclaude_model: claude-opus-4-8\n",
            lane_profiles={"coder": {"worker_runtime": "hermes", "model": "gpt-5.5"}},
        )
        assert "chat" in cmd  # hermes path, not claude
        # Search AFTER "chat": _resolve_hermes_argv() may itself expand to
        # ["python", "-m", "hermes_cli.main", ...] (dev/venv installs), so a
        # bare cmd.index("-m") can hit the python -m flag instead of the
        # model flag (argparse requires the model -m to follow chat).
        m_idx = cmd.index("-m", cmd.index("chat"))
        assert cmd[m_idx + 1] == "gpt-5.5"

    def test_lane_provider_and_fallback_chain_reach_hermes_spawn(
        self, kanban_home, tmp_path, monkeypatch,
    ):
        cmd = self._spawn(
            kanban_home, tmp_path, monkeypatch,
            lane_profiles={
                "coder": {
                    "worker_runtime": "hermes",
                    "provider": "openrouter",
                    "model": "qwen/qwen3.7-max",
                    "fallback_providers": [
                        {"provider": "openai-codex", "model": "gpt-5.5"},
                    ],
                },
            },
        )
        chat_idx = cmd.index("chat")
        assert cmd[cmd.index("-m", chat_idx) + 1] == "qwen/qwen3.7-max"
        assert cmd[cmd.index("--provider", chat_idx) + 1] == "openrouter"
        assert cmd[cmd.index("--fallback-provider", chat_idx) + 1] == "openai-codex:gpt-5.5"

    def test_lane_fallback_chain_wins_over_profile_fallback_chain(
        self, kanban_home, tmp_path, monkeypatch,
    ):
        cmd = self._spawn(
            kanban_home, tmp_path, monkeypatch,
            profile_config=(
                "model:\n  provider: openrouter\n  default: qwen/qwen3.7-max\n"
                "fallback_providers:\n"
                "  - provider: kimi-coding\n    model: kimi-for-coding\n"
            ),
            lane_profiles={
                "coder": {
                    "worker_runtime": "hermes",
                    "provider": "openrouter",
                    "model": "qwen/qwen3.7-max",
                    "fallback_providers": [
                        {"provider": "openai-codex", "model": "gpt-5.5"},
                    ],
                },
            },
        )
        fallback_values = [
            cmd[i + 1] for i, value in enumerate(cmd) if value == "--fallback-provider"
        ]
        assert fallback_values == ["openai-codex:gpt-5.5"]

    def test_no_lane_fallback_preserves_profile_fallback_behavior(
        self, kanban_home, tmp_path, monkeypatch,
    ):
        cmd = self._spawn(
            kanban_home, tmp_path, monkeypatch,
            profile_config=(
                "model:\n  provider: openrouter\n  default: qwen/qwen3.7-max\n"
                "fallback_providers:\n"
                "  - provider: openai-codex\n    model: gpt-5.5\n"
            ),
            lane_profiles={
                "coder": {
                    "worker_runtime": "hermes",
                    "provider": "openrouter",
                    "model": "qwen/qwen3.7-max",
                },
            },
        )
        assert "--fallback-provider" not in cmd

    # 6. Profile NOT mapped in the active lane → untouched config fallback.
    def test_unmapped_profile_falls_back_to_config(
        self, kanban_home, tmp_path, monkeypatch,
    ):
        cmd = self._spawn(
            kanban_home, tmp_path, monkeypatch,
            profile_config="worker_runtime: claude-cli\nclaude_model: claude-opus-4-8\n",
            lane_profiles={"reviewer": {"worker_runtime": "hermes", "model": "gpt-5.5"}},
        )
        assert self._claude_model_of(cmd) == "claude-opus-4-8"

    # 7. Hermes profile without lane mapping spawns WITHOUT -m (byte-identical
    #    pre-lane argv).
    def test_hermes_profile_without_lane_has_no_model_flag(
        self, kanban_home, tmp_path, monkeypatch,
    ):
        cmd = self._spawn(kanban_home, tmp_path, monkeypatch)
        assert "chat" in cmd
        # Only the argv AFTER "chat" is lane/model territory — the hermes
        # launcher itself may legitimately be ["python", "-m", "hermes_cli.main"].
        assert "-m" not in cmd[cmd.index("chat"):]

    # 8. Corrupt lane JSON is fail-soft: config behavior wins, no crash.
    def test_corrupt_lane_json_fails_soft(
        self, kanban_home, tmp_path, monkeypatch,
    ):
        with kb.connect() as conn:
            lane = kb.create_lane(conn, name="broken", profiles={})
            kb.activate_lane(conn, lane["id"])
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE lanes SET profiles = ? WHERE id = ?",
                    ("{not json", lane["id"]),
                )
        cmd = self._spawn(
            kanban_home, tmp_path, monkeypatch,
            profile_config="worker_runtime: claude-cli\nclaude_model: claude-opus-4-8\n",
        )
        assert self._claude_model_of(cmd) == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# Review gate under lanes
# ---------------------------------------------------------------------------


class TestReviewGateUnderLane:
    """The review gate's verifier spawn flows through the same _default_spawn,
    so a lane mapping for the verifier profile must apply — and the gate
    config itself must be lane-agnostic."""

    def test_review_gate_config_is_lane_agnostic(self, kanban_home):
        # Lanes never touch _review_gate_config (root config.yaml seam).
        with kb.connect() as conn:
            lanes = kb.list_lanes(conn)
            max_abo = next(l for l in lanes if l["name"] == "max-abo")
            kb.activate_lane(conn, max_abo["id"])
        cfg = kb._review_gate_config()
        assert "code_roles" in cfg and "verifier_profile" in cfg
        assert isinstance(cfg["verifier_profile"], str)

    def test_verifier_profile_spawn_honors_lane(self, kanban_home, tmp_path, monkeypatch):
        with kb.connect() as conn:
            lane = kb.create_lane(
                conn, name="verifier-lane",
                profiles={"verifier": {"worker_runtime": "claude-cli", "model": "claude-fable-5"}},
            )
            kb.activate_lane(conn, lane["id"])

        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                self.pid = 555

        monkeypatch.setattr("subprocess.Popen", _FakePopen)
        monkeypatch.setenv("HERMES_CLAUDE_BIN", "/usr/local/bin/claude-test")

        task = kb.Task(
            id="t_review",
            title="review the widget",
            body="verify",
            assignee="verifier",
            status="review",
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
            branch_name="wt/t_review",
            model_override=None,
        )
        kb._default_spawn(task, str(tmp_path / "ws"))
        cmd = captured["cmd"]
        assert cmd[0] == "/usr/local/bin/claude-test"
        assert cmd[cmd.index("--model") + 1] == "claude-fable-5"


# ---------------------------------------------------------------------------
# _active_lane_entry_for_profile unit seams
# ---------------------------------------------------------------------------


class TestActiveLaneEntry:
    def test_returns_none_without_active_lane(self, kanban_home):
        assert kb._active_lane_entry_for_profile("coder") is None

    def test_returns_entry_for_mapped_profile(self, kanban_home):
        with kb.connect() as conn:
            lane = kb.create_lane(
                conn, name="l1",
                profiles={"coder": {"worker_runtime": "claude-cli", "model": "claude-fable-5"}},
            )
            kb.activate_lane(conn, lane["id"])
        entry = kb._active_lane_entry_for_profile("coder")
        assert entry == {
            "worker_runtime": "claude-cli",
            "provider": None,
            "model": "claude-fable-5",
            "fallback_providers": [],
        }

    def test_old_lane_shape_reads_with_new_defaults(self, kanban_home):
        with kb.connect() as conn:
            lane = kb.create_lane(
                conn,
                name="old-shape",
                profiles={"coder": {"worker_runtime": "hermes", "model": "gpt-5.5"}},
            )
            kb.activate_lane(conn, lane["id"])
        entry = kb._active_lane_entry_for_profile("coder")
        assert entry == {
            "worker_runtime": "hermes",
            "provider": None,
            "model": "gpt-5.5",
            "fallback_providers": [],
        }

    def test_blank_entry_normalizes_to_none(self, kanban_home):
        with kb.connect() as conn:
            lane = kb.create_lane(
                conn, name="l2", profiles={"coder": {"model": "   "}},
            )
            kb.activate_lane(conn, lane["id"])
        assert kb._active_lane_entry_for_profile("coder") is None

    def test_seed_profiles_json_is_valid(self, kanban_home):
        with kb.connect() as conn:
            kb.ensure_lane_seeds(conn)
            for row in conn.execute("SELECT profiles FROM lanes").fetchall():
                parsed = json.loads(row["profiles"])
                assert isinstance(parsed, dict) and parsed
