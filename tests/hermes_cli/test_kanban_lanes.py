"""Tests for lane presets (night-sprint F1).

A lane is a named profile→(worker_runtime, model) preset stored in the board
DB; the dispatcher hot-reads the ACTIVE lane at every spawn. Precedence:

    task.model_override > active lane > profile config.yaml default
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_lane_fixer as lane_fixer
from hermes_cli import kanban_worktrees as kwt
from tests.hermes_cli._kanban_test_helpers import _commit_in, _events, _git, _ok_gate


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
        # Phase 2 (AC-1): the verdict-lane allowlist fail-closed gate probes the
        # Claude CLI version. The test binary is a stub, so pin the gate ON to
        # reach the cmd-capture (the dedicated fail-closed test pins it OFF).
        monkeypatch.setenv("HERMES_VERDICT_ALLOWLIST_ENFORCEABLE", "1")
        # Hermeticity: the verdict allowlist appends the operator-configured
        # HERMES_REVIEW_BASH_ALLOWLIST verbatim. The dispatcher/integrator
        # environment sets it (e.g. "Bash(git diff:*),..."), so neutralize it
        # here to assert the default read-only cage (Read,Grep,Glob). Mirrors
        # test_kanban_verdict_cage_e2e.py's delenv isolation.
        monkeypatch.delenv("HERMES_REVIEW_BASH_ALLOWLIST", raising=False)

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

    def _claude_allowed_tools(self, cmd):
        """Phase 2: verdict lanes use --allowedTools (allowlist fail-closed)."""
        assert cmd[0] == "/usr/local/bin/claude-test", f"not the claude path: {cmd[:3]}"
        assert "--allowedTools" in cmd, f"--allowedTools missing: {cmd}"
        return set(cmd[cmd.index("--allowedTools") + 1].split(","))

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

        # Phase 2 (AC-1): verdict lanes use --allowedTools (allowlist), NOT
        # --disallowedTools (denylist). The allowlist is the minimal read-only
        # set: Read, Grep, Glob. No Bash (no native read-only Bash tool).
        # --dangerously-skip-permissions must NOT appear in the verdict cmd.
        expected_allow = {"Read", "Grep", "Glob"}
        assert self._claude_allowed_tools(reviewer_cmd) == expected_allow
        assert self._claude_allowed_tools(critic_cmd) == expected_allow
        assert "--dangerously-skip-permissions" not in reviewer_cmd
        assert "--dangerously-skip-permissions" not in critic_cmd
        assert "--disallowedTools" not in reviewer_cmd
        assert "--disallowedTools" not in critic_cmd

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

    def test_explicit_empty_lane_fallback_overrides_profile_fallback_chain(
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
                    "fallback_providers": [],
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


# ---------------------------------------------------------------------------
# Lane-scope enforcement vs. merge-commit receipts (MR-S1).
#
# A worker receipt pointing at a MERGE commit yields no paths from
# ``diff-tree`` (no ``-m``/``--cc``). Read as an authoritative empty set it
# filtered every candidate path away and waved an out-of-lane file through;
# the shared helper now reports Unknown instead, so the guard keeps judging
# the reviewed diff. Real git + real board DB, no mocks for the file set.
# ---------------------------------------------------------------------------


@pytest.fixture
def lane_scope_home(tmp_path, monkeypatch):
    """Isolated board DB (asserted off the live board) for git-real cases."""
    home = tmp_path / ".hermes"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    assert db_path.resolve() != Path("/home/piet/.hermes/kanban.db").resolve()
    assert home.resolve() in db_path.resolve().parents
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def lane_repo(tmp_path):
    """Real git repo on ``main`` with one base commit."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "lane-scope@test.invalid")
    _git(r, "config", "user.name", "lane scope test")
    (r / "a.txt").write_text("base\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "base")
    return r


def _evil_merge_receipt(lane_repo: Path, task_root: str) -> tuple[Path, str, str]:
    """Branch with an in-lane commit plus a merge that smuggles a web/ file."""
    info = kwt.ensure_worktree(lane_repo, task_root)
    worktree = info["path"]
    base_commit = _git(worktree, "rev-parse", "HEAD")
    _commit_in(
        worktree,
        "hermes_cli/backend_slice.py",
        "SLICE = 1\n",
        msg=f"kanban({task_root}): backend slice",
    )
    _commit_in(
        lane_repo,
        "hermes_cli/foreign_target.py",
        "FOREIGN = 1\n",
        msg="foreign target advance",
    )
    _git(worktree, "merge", "--no-commit", "main")
    evil = worktree / "web" / "src" / "control" / "Evil.tsx"
    evil.parent.mkdir(parents=True, exist_ok=True)
    evil.write_text("export const Evil = 1\n", encoding="utf-8")
    _git(worktree, "add", "web/src/control/Evil.tsx")
    _git(worktree, "commit", "-m", f"kanban({task_root}): merge live target")
    merge_commit = _git(worktree, "rev-parse", "HEAD")
    parents = _git(worktree, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert len(parents) == 3, "fixture must produce a real merge commit"
    return worktree, base_commit, merge_commit


def test_lane_scope_evil_merge_receipt_does_not_clear_snapshot_violations(
    lane_repo, lane_scope_home,
):
    with kb.connect() as conn:
        worktree, base_commit, merge_commit = _evil_merge_receipt(
            lane_repo, "t_ls_evil_merge"
        )
        # The receipt itself is unattributable: no path may be charged to it,
        # and none may be cleared by it either.
        assert kwt._lane_scope_recorded_task_commit_paths(
            conn, "t_missing", worktree, {"commit": merge_commit},
        ) is None
        task_id = kb.create_task(
            conn,
            title="backend slice with evil merge receipt",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(worktree),
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                "review_snapshot_provisioned",
                {
                    "base_commit": base_commit,
                    "candidate_commit": merge_commit,
                },
            )

        out = kwt.maybe_integrate_on_complete(
            conn,
            task_id,
            completion_metadata={"commit": merge_commit},
            gate_runner=_ok_gate,
        )

        assert out is not None and out["action"] == "parked", (
            "an empty merge-commit receipt filtered every reviewed path away "
            "and waved the out-of-lane file through"
        )
        blocked = _events(conn, task_id, kwt.LANE_SCOPE_BLOCKED_EVENT)
        assert len(blocked) == 1
        assert blocked[0]["violating_paths"] == ["web/src/control/Evil.tsx"]
        assert blocked[0]["expected_lane"] == "coder-frontend"


# ---------------------------------------------------------------------------
# Declared-scope path matching — `/**` glob normalization
# ---------------------------------------------------------------------------
#
# Restored piece of 61b392d86c (lost with the revert 88377432aa). Scope
# declarations are written by humans and decomposers in glob form
# ("web/src/control/**"); without normalization every such entry matches
# nothing, so a scope-driven guard silently degrades to "no scope declared".


@pytest.mark.parametrize(
    "path,roots,expected",
    [
        # The glob form is what PlanSpecs and task bodies actually declare.
        ("web/src/control/App.tsx", ["web/src/control/**"], True),
        ("web/src/control", ["web/src/control/**"], True),
        ("hermes_cli/kanban.py", ["hermes_cli/**"], True),
        # Trailing slash is the same declaration, written differently.
        ("hermes_cli/kanban.py", ["hermes_cli/"], True),
        # Leading slash: absolute-looking declarations are repo-relative.
        ("hermes_cli/kanban.py", ["/hermes_cli/**"], True),
        # Plain (non-glob) roots keep working exactly as before.
        ("hermes_cli/kanban.py", ["hermes_cli"], True),
        ("hermes_cli/kanban.py", ["hermes_cli/kanban.py"], True),
        # No prefix-widening: the glob must not match a sibling directory
        # whose name merely starts with the declared one.
        ("web/src/controlx/App.tsx", ["web/src/control/**"], False),
        ("tests/hermes_cli/test_x.py", ["web/src/control/**"], False),
    ],
)
def test_path_is_under_normalizes_glob_scope_declarations(path, roots, expected):
    assert kwt._path_is_under(path, roots) is expected


def test_dirty_scope_overlap_sees_glob_declared_scope():
    """The overlap guard consumes `_path_is_under`; a glob scope must count."""
    overlap = kwt._dirty_scope_overlap(
        ["web/src/control/App.tsx", "docs/readme.md"],
        ["web/src/control/**"],
    )
    assert overlap == ["web/src/control/App.tsx"]


# ---------------------------------------------------------------------------
# Earned lane allowlists — restored piece of a0ead50fd3 (lost with 88377432aa)
# ---------------------------------------------------------------------------
#
# A *done* lane-scope fixer card is a claim, not evidence. Before this guard a
# fixer that closed without touching anything handed its parent permanent
# immunity over exactly the paths that parked it. Immunity must be earned per
# child and per path — either the fixer is attributable for the path, or an
# operator signed an explicit, tip-bound waiver — and every grant is audited.
#
# Attribution runs through the fork-owned
# ``kanban_fixer_scope._attributed_task_paths`` instead of a commit-subject
# grep: ``git log --name-only`` prints no path for a MERGE commit, so fixer
# work that lands by merge would otherwise earn nothing and re-park its parent
# forever. Real git + real board DB, no mocks for the file set.


def _stamp_materialized_fixer_run(conn, child_id, pre_run_sha):
    """Record the materialized pre-run stamp a dispatched fixer run leaves."""
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_runs "
            "(task_id, status, started_at, pre_run_commit_sha, "
            "workspace_materialized) VALUES (?, 'done', 0, ?, 1)",
            (child_id, pre_run_sha),
        )


def _park_unscoped_parent_with_lane_fixer(conn, lane_repo, root_id):
    """Park an out-of-lane ``coder`` parent and dispatch its lane fixer."""
    info = kwt.ensure_worktree(lane_repo, root_id)
    path = "web/src/control/LandingPack.tsx"
    _commit_in(
        info["path"],
        path,
        "export const LandingPack = 1\n",
        msg=f"kanban({root_id}): work",
    )
    parent_id = kb.create_task(
        conn,
        title="unscoped backend task",
        assignee="coder",
        workspace_kind="dir",
        workspace_path=str(info["path"]),
    )
    result = kwt.maybe_integrate_on_complete(
        conn, parent_id, gate_runner=_ok_gate,
    )
    assert result is not None and result["action"] == "parked"
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'blocked', block_kind = 'integration' "
            "WHERE id = ?",
            (parent_id,),
        )
        kb._append_event(
            conn,
            parent_id,
            "blocked",
            {"reason": result["reason"], "kind": "integration"},
        )
    dispatched = _events(
        conn, parent_id, lane_fixer.LANE_FIXER_DISPATCHED_EVENT,
    )
    assert len(dispatched) == 1
    return parent_id, dispatched[0]["child_id"], info, path, dispatched[0]


def test_commitless_lane_fixer_does_not_allowlist_violation(
    lane_repo, lane_scope_home,
):
    """A fixer that changed nothing must not clear its parent's violation."""
    with kb.connect() as conn:
        parent_id, child_id, _info, path, _dispatch = (
            _park_unscoped_parent_with_lane_fixer(
                conn, lane_repo, "t_commitless_lane_fixer",
            )
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?", (child_id,),
            )
        assert lane_fixer.resume_parent_for_completed_lane_fixer(conn, child_id)

        result = kwt.maybe_integrate_on_complete(
            conn, parent_id, gate_runner=_ok_gate,
        )

        assert result is not None and result["action"] == "parked", (
            "a done fixer card with no attributable work bought the parent "
            "immunity over the exact path that parked it"
        )
        blocked = _events(conn, parent_id, kwt.LANE_SCOPE_BLOCKED_EVENT)
        assert blocked[-1]["violating_paths"] == [path]
        assert _events(
            conn, parent_id, lane_fixer.LANE_SCOPE_ALLOWLISTED_EVENT,
        ) == []


def test_merge_landed_lane_fixer_earns_audited_allowlist(
    lane_repo, lane_scope_home, caplog,
):
    """Fixer work that lands as a MERGE is attributed and stops the re-park.

    Both subject-based routes are blind to this shape: the fixer's own commit
    carries no ``kanban(<child>):`` marker (nothing enforces that convention
    on a worker), and ``diff-tree`` on the merge receipt prints no path at
    all. Only the SHA-range stamp attributes it — which is why attribution
    goes through ``kanban_fixer_scope._attributed_task_paths``.
    """
    with kb.connect() as conn:
        parent_id, child_id, info, path, dispatch = (
            _park_unscoped_parent_with_lane_fixer(
                conn, lane_repo, "t_merge_landed_lane_fixer",
            )
        )
        worktree = info["path"]
        chain = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
        park_tip = _git(worktree, "rev-parse", "HEAD")
        _stamp_materialized_fixer_run(conn, child_id, park_tip)

        # The fixer adopts the frontend path on its own branch and merges it
        # in. Its subject deliberately carries no `kanban(<child>):` marker —
        # a real worker's commit message is not a machine contract.
        _git(worktree, "checkout", "-b", f"lane-fixer/{child_id}")
        _commit_in(
            worktree,
            path,
            "export const LandingPack = 2\n",
            msg="adopt frontend path",
        )
        _git(worktree, "checkout", chain)
        _git(
            worktree, "merge", "--no-ff", "-m",
            f"kanban: merge lane-fixer/{child_id}", f"lane-fixer/{child_id}",
        )
        merge_commit = _git(worktree, "rev-parse", "HEAD")
        parents = _git(worktree, "rev-list", "--parents", "-n", "1", "HEAD")
        assert len(parents.split()) == 3, "fixture must produce a real merge"
        # Neither subject-based route can see this work: the receipt route
        # reads a merge as Unknown, and a `--grep=kanban(<child>):` log walk
        # matches nothing. Only the pre-run..candidate stamp attributes it.
        assert kwt._lane_scope_recorded_task_commit_paths(
            conn, child_id, worktree, {"commit": merge_commit},
        ) is None
        assert _git(
            worktree, "log", "--format=", "--name-only", "--fixed-strings",
            f"--grep=kanban({child_id}):", f"{park_tip}..{merge_commit}",
            "--", path,
        ) == ""

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?", (child_id,),
            )
        assert lane_fixer.resume_parent_for_completed_lane_fixer(conn, child_id)

        with caplog.at_level(logging.WARNING, logger=lane_fixer.__name__):
            result = kwt.maybe_integrate_on_complete(
                conn, parent_id, gate_runner=_ok_gate,
            )

        assert result is not None and result["action"] == "merged", result
        audited = _events(
            conn, parent_id, lane_fixer.LANE_SCOPE_ALLOWLISTED_EVENT,
        )
        assert len(audited) == 1
        assert audited[0] == {
            "child_id": child_id,
            "fingerprint": dispatch["fingerprint"],
            "paths": [path],
            "source": "fixer_commit",
        }
        assert (
            f"lane-scope allowlist parent={parent_id} child={child_id} "
            f"fingerprint={dispatch['fingerprint']} paths={path}"
        ) in caplog.text


def test_explicit_operator_waiver_can_allow_commitless_fixer(
    lane_repo, lane_scope_home,
):
    """The escape hatch stays open, but only signed and bound to the tip."""
    with kb.connect() as conn:
        parent_id, child_id, info, path, dispatch = (
            _park_unscoped_parent_with_lane_fixer(
                conn, lane_repo, "t_operator_lane_waiver",
            )
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                parent_id,
                lane_fixer.LANE_SCOPE_ALLOWLIST_WAIVER_EVENT,
                {
                    "branch_tip": _git(info["path"], "rev-parse", "HEAD"),
                    "child_id": child_id,
                    "fingerprint": dispatch["fingerprint"],
                    "operator_id": "operator:test",
                    "paths": [path],
                },
            )
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?", (child_id,),
            )
        assert lane_fixer.resume_parent_for_completed_lane_fixer(conn, child_id)

        result = kwt.maybe_integrate_on_complete(
            conn, parent_id, gate_runner=_ok_gate,
        )

        assert result is not None and result["action"] == "merged", result
        assert _events(
            conn, parent_id, lane_fixer.LANE_SCOPE_ALLOWLISTED_EVENT,
        )[-1] == {
            "child_id": child_id,
            "fingerprint": dispatch["fingerprint"],
            "paths": [path],
            "source": "operator_waiver",
        }


def test_unsigned_waiver_does_not_allowlist_violation(
    lane_repo, lane_scope_home,
):
    """A waiver without an operator id is not an operator decision."""
    with kb.connect() as conn:
        parent_id, child_id, info, path, dispatch = (
            _park_unscoped_parent_with_lane_fixer(
                conn, lane_repo, "t_unsigned_lane_waiver",
            )
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                parent_id,
                lane_fixer.LANE_SCOPE_ALLOWLIST_WAIVER_EVENT,
                {
                    "branch_tip": _git(info["path"], "rev-parse", "HEAD"),
                    "child_id": child_id,
                    "fingerprint": dispatch["fingerprint"],
                    "operator_id": "",
                    "paths": [path],
                },
            )
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?", (child_id,),
            )
        assert lane_fixer.resume_parent_for_completed_lane_fixer(conn, child_id)

        result = kwt.maybe_integrate_on_complete(
            conn, parent_id, gate_runner=_ok_gate,
        )

        assert result is not None and result["action"] == "parked", result
        assert _events(
            conn, parent_id, lane_fixer.LANE_SCOPE_ALLOWLISTED_EVENT,
        ) == []


# ---------------------------------------------------------------------------
# Operator-unblock branch: earned immunity without a resume event
# ---------------------------------------------------------------------------
#
# The documented live failure is "conflict fixer resolves but leaves parent
# blocked": the fixer lands its corrective work, closes, and the completion
# hook never wakes the parent — so no ``lane_scope_fixer_parent_resumed``
# event is written and an operator unblocks the parent by hand. Tying fixer
# evidence to that event makes the park→fix→resume loop re-open for exactly
# this branch: the parent re-parks on a path its fixer demonstrably adopted.
#
# The event was only ever a *proxy* for the real boundary — "where does the
# fixer's work stop and the parent's later work begin". The parent's own
# materialized claim stamps answer that directly, so the evidence stays
# tip-bound without needing the hook to have fired.


def _operator_unblock(conn, parent_id):
    """The paved-road handgrip: operator clears the park without a hook."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'ready', block_kind = NULL, "
            "block_recurrences = 0 WHERE id = ?",
            (parent_id,),
        )
        kb._append_event(
            conn, parent_id, "unblocked", {"status": "ready", "source": "operator"},
        )


def _stamp_materialized_parent_run(conn, parent_id, pre_run_sha):
    """Record the claim stamp a re-dispatched parent run leaves behind."""
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_runs "
            "(task_id, status, started_at, pre_run_commit_sha, "
            "workspace_materialized) VALUES (?, 'running', 0, ?, 1)",
            (parent_id, pre_run_sha),
        )


def test_operator_unblocked_parent_keeps_earned_fixer_allowlist(
    lane_repo, lane_scope_home,
):
    """Fixer work stays evidence when the completion hook never resumed.

    Fixer commits the lane path, closes, no resume event is written, the
    operator unblocks by hand — the parent must merge, not re-park on the
    very path its fixer adopted.
    """
    with kb.connect() as conn:
        parent_id, child_id, info, path, dispatch = (
            _park_unscoped_parent_with_lane_fixer(
                conn, lane_repo, "t_operator_unblock_lane_fixer",
            )
        )
        worktree = info["path"]
        # The parked run of the parent left its own claim stamp behind — it
        # predates the fixer and must not be read as a post-fixer boundary,
        # or the fixer's work would be charged to the parent.
        _stamp_materialized_parent_run(
            conn, parent_id, _git(worktree, "rev-parse", "HEAD~1"),
        )
        _stamp_materialized_fixer_run(
            conn, child_id, _git(worktree, "rev-parse", "HEAD"),
        )
        _commit_in(
            worktree,
            path,
            "export const LandingPack = 2\n",
            msg="adopt frontend path",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?", (child_id,),
            )
        # The completion hook did NOT wake the parent.
        assert _events(
            conn, parent_id, lane_fixer.LANE_FIXER_PARENT_RESUMED_EVENT,
        ) == []
        _operator_unblock(conn, parent_id)

        result = kwt.maybe_integrate_on_complete(
            conn, parent_id, gate_runner=_ok_gate,
        )

        assert result is not None and result["action"] == "merged", result
        assert _events(
            conn, parent_id, lane_fixer.LANE_SCOPE_ALLOWLISTED_EVENT,
        )[-1] == {
            "child_id": child_id,
            "fingerprint": dispatch["fingerprint"],
            "paths": [path],
            "source": "fixer_commit",
        }


def test_parent_work_after_operator_unblock_is_not_fixer_evidence(
    lane_repo, lane_scope_home,
):
    """The missing handoff tip must not widen the fixer's attribution.

    Without a resume event the fixer's range runs to the branch tip, so a
    parent commit landing after the fixer would ride along as "fixer work".
    The parent's own post-fixer claim stamp bounds it: everything from there
    on is the parent's, and the gate re-parks.
    """
    with kb.connect() as conn:
        parent_id, child_id, info, path, _dispatch = (
            _park_unscoped_parent_with_lane_fixer(
                conn, lane_repo, "t_parent_work_after_unblock",
            )
        )
        worktree = info["path"]
        _stamp_materialized_fixer_run(
            conn, child_id, _git(worktree, "rev-parse", "HEAD"),
        )
        _commit_in(
            worktree,
            path,
            "export const LandingPack = 2\n",
            msg="adopt frontend path",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?", (child_id,),
            )
        _operator_unblock(conn, parent_id)

        # Re-dispatched parent stamps its own claim, then touches the path
        # the fixer had adopted — that is new violating work, not the fixer's.
        _stamp_materialized_parent_run(
            conn, parent_id, _git(worktree, "rev-parse", "HEAD"),
        )
        _commit_in(
            worktree,
            path,
            "export const LandingPack = 3\n",
            msg=f"kanban({parent_id}): touch it again",
        )

        result = kwt.maybe_integrate_on_complete(
            conn, parent_id, gate_runner=_ok_gate,
        )

        assert result is not None and result["action"] == "parked", result
        assert _events(
            conn, parent_id, lane_fixer.LANE_SCOPE_ALLOWLISTED_EVENT,
        ) == []
