"""Tests for per-agent session facts (context, compactions, steering).

Every fixture here is a real excerpt copied out of a live Buzz agent's own CLI
session store (or, for the compaction pair — which never occurs in the short
Buzz transcripts on this host — a real compact_boundary/isCompactSummary
pair copied from this repo's own Claude Code history, session id remapped),
not a hand-written JSON blob. See ``tests/fixtures/agent_session_facts/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.agent_session_facts import (
    SessionFactsService,
    claude_dir_name,
    codex_workspace_cwd,
    find_latest_codex_session,
    find_latest_grok_session_dir,
    find_latest_kimi_session_dir,
    grok_session_root,
    parse_steering,
    read_claude_session,
    read_codex_session,
    read_grok_session,
    read_kimi_session,
    read_qwen_session,
)

FIXTURES = Path(__file__).parent / "fixtures" / "agent_session_facts"


class FakeResult:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class FakeRunner:
    """Answers every journalctl call with the same captured steering line(s)."""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def run(self, args, *, timeout=None):
        self.calls.append(args)
        return FakeResult(self.stdout, self.returncode)


# --- claude family ----------------------------------------------------


def test_claude_dir_name_is_an_exact_match_not_a_suffix() -> None:
    """`…workspaces-claude--scratch-a2-fable` must never resolve for `fable`."""
    assert claude_dir_name("fable") == "-mnt-data-services-buzz-agent-workspaces-fable"
    assert claude_dir_name("fable") != "-mnt-data-services-buzz-agent-workspaces-claude--scratch-a2-fable"


def test_claude_scratch_lookalike_directory_does_not_leak_into_fable(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    real = projects / "-mnt-data-services-buzz-agent-workspaces-fable"
    trap = projects / "-mnt-data-services-buzz-agent-workspaces-claude--scratch-a2-fable"
    real.mkdir(parents=True)
    trap.mkdir(parents=True)
    # Only the trap directory has a session — if directory resolution ever
    # degrades to endswith()/suffix matching, this makes it visible.
    (trap / "trap.jsonl").write_text(
        json.dumps({"type": "queue-operation", "timestamp": "2026-08-05T00:00:00.000Z", "sessionId": "trap"})
        + "\n",
        encoding="utf-8",
    )
    service = SessionFactsService(claude_projects_dir=projects, runner=FakeRunner(returncode=1))
    facts = service.facts_for_stem("fable")
    assert facts.source == "none"


def test_read_claude_session_parses_the_real_fixture() -> None:
    result = read_claude_session(FIXTURES / "claude_session.jsonl")
    assert result is not None
    facts, model = result
    assert model == "claude-opus-5"
    # 2 + 2617 + 528250 from the real transcript — sidechain line excluded
    # even though it carries an inflated (999999) usage that would dominate
    # if the isSidechain filter were broken.
    assert facts.prompt_tokens == 2 + 2617 + 528250
    assert facts.session_id == "fixture-session-0001"
    # UTC "…Z" timestamp parsed correctly, not shifted by the local offset.
    assert facts.started_at == pytest.approx(1785926965.05, abs=1.0)


def test_read_claude_session_counts_the_real_compaction_pair() -> None:
    result = read_claude_session(FIXTURES / "claude_session.jsonl")
    assert result is not None
    facts, _ = result
    # One compact_boundary + its paired isCompactSummary line (same
    # parentUuid) must count as ONE compaction, not two.
    assert facts.compacts == 1
    assert facts.last_compaction is not None
    assert facts.last_compaction.pre == 342799
    assert facts.last_compaction.post == 13860


def test_unknown_model_yields_no_context_window_and_no_percent(tmp_path: Path) -> None:
    directory = tmp_path / "projects" / "-mnt-data-services-buzz-agent-workspaces-claude"
    directory.mkdir(parents=True)
    (directory / "s.jsonl").write_text(
        FIXTURES.joinpath("claude_session.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    # A resolver standing in for models.dev that has never heard of this model.
    service = SessionFactsService(
        claude_projects_dir=tmp_path / "projects",
        runner=FakeRunner(returncode=1),
        context_window_resolver=lambda provider, model: None,
    )
    facts = service.facts_for_stem("claude")
    assert facts.context_window is None
    assert facts.percent is None
    assert facts.prompt_tokens is not None  # tokens are still known


def test_claude_context_window_resolver_receives_the_anthropic_provider(tmp_path: Path) -> None:
    directory = tmp_path / "projects" / "-mnt-data-services-buzz-agent-workspaces-claude"
    directory.mkdir(parents=True)
    (directory / "s.jsonl").write_text(
        FIXTURES.joinpath("claude_session.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    seen = []

    def resolver(provider, model):
        seen.append((provider, model))
        return 1_000_000

    service = SessionFactsService(
        claude_projects_dir=tmp_path / "projects",
        runner=FakeRunner(returncode=1),
        context_window_resolver=resolver,
    )
    facts = service.facts_for_stem("claude")
    assert seen == [("anthropic", "claude-opus-5")]
    assert facts.context_window == 1_000_000
    assert facts.percent == pytest.approx(53.09, abs=0.1)


# --- codex family -------------------------------------------------------


def test_codex_workspace_cwd() -> None:
    assert codex_workspace_cwd("codex") == "/mnt/data/services/buzz-agent-workspaces/codex"


def test_codex_cwd_filter_skips_the_wrong_workspace(tmp_path: Path) -> None:
    """The newest rollout on disk can belong to an unrelated loop worker; the
    reader must skip it and fall back to the real Buzz session."""
    day_dir = tmp_path / "2026" / "08" / "05"
    day_dir.mkdir(parents=True)
    real = FIXTURES / "codex_rollout_codex.jsonl"
    wrong = FIXTURES / "codex_rollout_wrong_cwd.jsonl"
    (day_dir / "rollout-real.jsonl").write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
    (day_dir / "rollout-wrong.jsonl").write_text(wrong.read_text(encoding="utf-8"), encoding="utf-8")
    import os
    import time

    now = time.time()
    # The wrong-cwd rollout is newer on disk than the real one.
    os.utime(day_dir / "rollout-real.jsonl", (now - 100, now - 100))
    os.utime(day_dir / "rollout-wrong.jsonl", (now, now))

    found = find_latest_codex_session(tmp_path, "codex")
    assert found == day_dir / "rollout-real.jsonl"


def test_read_codex_session_uses_last_not_total_token_usage() -> None:
    facts = read_codex_session(FIXTURES / "codex_rollout_codex.jsonl")
    assert facts is not None
    # last_token_usage.input_tokens in the real fixture, NOT
    # total_token_usage.input_tokens (10716929 in the same file — the
    # cumulative-vs-latest trap).
    assert facts.prompt_tokens == 164165
    assert facts.context_window == 258400
    assert facts.session_id == "fixture-codex-session-0001"


def test_codex_context_window_object_shape_is_not_mistaken_for_a_number() -> None:
    """`context_window` in a codex payload can be `{"window_id": …}` — an
    object, not a number. The reader must only ever read
    `model_context_window`, never a bare `context_window` key."""
    facts = read_codex_session(FIXTURES / "codex_rollout_codex.jsonl")
    assert facts is not None
    assert isinstance(facts.context_window, int)


# --- qwen -----------------------------------------------------------------


def test_read_qwen_session_reads_prompt_tokens_and_its_own_context_window() -> None:
    facts = read_qwen_session(FIXTURES / "qwen_session.jsonl")
    assert facts is not None
    assert facts.prompt_tokens == 125570
    assert facts.context_window == 1_000_000
    assert facts.session_id == "fixture-qwen-session-0001"
    assert facts.started_at == pytest.approx(1785957870.736, abs=1.0)


# --- kimi -------------------------------------------------------------


def test_find_latest_kimi_session_dir_filters_by_workdir(tmp_path: Path) -> None:
    index_src = FIXTURES / "kimi" / "session_index.jsonl"
    index_text = index_src.read_text(encoding="utf-8").replace("{FIXTURE_DIR}", str(FIXTURES / "kimi"))
    index_path = tmp_path / "session_index.jsonl"
    index_path.write_text(index_text, encoding="utf-8")

    found = find_latest_kimi_session_dir(index_path, "kimi")
    assert found == FIXTURES / "kimi" / "sessions" / "wd_fixture_kimi" / "session_fixture-0001"

    # terra's workDir entry must never resolve for the kimi stem.
    found_terra = find_latest_kimi_session_dir(index_path, "terra")
    assert found_terra is None


def test_read_kimi_session_only_counts_agents_main() -> None:
    session_dir = FIXTURES / "kimi" / "sessions" / "wd_fixture_kimi" / "session_fixture-0001"
    result = read_kimi_session(session_dir, session_id="session_fixture-0001")
    assert result is not None
    facts, model = result
    # Newest of the two agents/main usage.record lines: 1087 + 73728 + 0.
    # The agents/agent-0 line (9999999 + 9999999) must be ignored entirely.
    assert facts.prompt_tokens == 1087 + 73728
    assert model == "kimi-code/k3"
    assert facts.started_at == pytest.approx(1785955185.012, abs=1.0)


def test_kimi_model_id_strips_the_kimi_code_prefix_for_the_catalog_lookup(tmp_path: Path) -> None:
    seen = []

    def resolver(provider, model):
        seen.append((provider, model))
        return 1_048_576

    index_src = FIXTURES / "kimi" / "session_index.jsonl"
    index_text = index_src.read_text(encoding="utf-8").replace("{FIXTURE_DIR}", str(FIXTURES / "kimi"))
    index_path = tmp_path / "session_index.jsonl"
    index_path.write_text(index_text, encoding="utf-8")

    service = SessionFactsService(
        kimi_session_index=index_path,
        runner=FakeRunner(returncode=1),
        context_window_resolver=resolver,
    )
    facts = service.facts_for_stem("kimi")
    assert seen == [("kimi-for-coding", "k3")]
    assert facts.context_window == 1_048_576


# --- grok -------------------------------------------------------------


def test_grok_session_root_url_encodes_the_workspace_path() -> None:
    root = grok_session_root(Path("/home/x/.grok"), "grok")
    assert root == Path("/home/x/.grok/sessions/%2Fmnt%2Fdata%2Fservices%2Fbuzz-agent-workspaces%2Fgrok")


def test_find_latest_grok_session_dir_picks_the_newest_mtime() -> None:
    root = FIXTURES / "grok" / "sessions" / "%2Fmnt%2Fdata%2Fservices%2Fbuzz-agent-workspaces%2Fgrok"
    found = find_latest_grok_session_dir(root)
    assert found == root / "fixture-session-0001"


def test_read_grok_session_does_not_add_cached_prompt_tokens() -> None:
    session_dir = (
        FIXTURES / "grok" / "sessions" / "%2Fmnt%2Fdata%2Fservices%2Fbuzz-agent-workspaces%2Fgrok" / "fixture-session-0001"
    )
    unified_log = FIXTURES / "grok" / "unified.jsonl"
    facts = read_grok_session(session_dir, unified_log, session_id="fixture-session-0001")
    assert facts is not None
    # ctx.prompt_tokens is already the full context; cached_prompt_tokens
    # (75136) is a subset and must NOT be added on top.
    assert facts.prompt_tokens == 90902
    # Newest matching line wins over an older one for the same session
    # (50000) and a same-timestamp-window line for a DIFFERENT session
    # (12345, sid="other-session-9999") must never leak in.
    assert facts.prompt_tokens != 50000
    assert facts.prompt_tokens != 12345
    # Oldest of the three state files' mtimes (prompt_context.json).
    assert facts.started_at == pytest.approx(1785957871, abs=1.0)


# --- steering -----------------------------------------------------------


def test_parse_steering_reads_the_real_captured_journal_lines() -> None:
    lines = (FIXTURES / "steering_journal_lines.jsonl").read_text(encoding="utf-8")
    per_unit = {}
    for raw in lines.splitlines():
        entry = json.loads(raw)
        per_unit[entry["_SYSTEMD_USER_UNIT"]] = raw

    assert parse_steering(per_unit["buzz-agent@claude.service"]) is True
    assert parse_steering(per_unit["buzz-agent@qwen.service"]) is False
    assert parse_steering(per_unit["buzz-agent@kimi.service"]) is False


def test_parse_steering_is_none_when_nothing_matched() -> None:
    assert parse_steering("") is None


def test_stem_without_any_store_gets_none_never_zero(tmp_path: Path) -> None:
    """hermes has no CLI session store on disk at all."""
    service = SessionFactsService(
        claude_projects_dir=tmp_path / "claude-projects",
        codex_sessions_dir=tmp_path / "codex-sessions",
        qwen_projects_dir=tmp_path / "qwen-projects",
        kimi_session_index=tmp_path / "no-such-index.jsonl",
        grok_home=tmp_path / "grok-home",
        runner=FakeRunner(returncode=1),
    )
    facts = service.facts_for_stem("hermes")
    assert facts.source == "none"
    assert facts.session_id is None
    assert facts.started_at is None
    assert facts.prompt_tokens is None
    assert facts.context_window is None
    assert facts.percent is None
    # Never a 0 — a real 0 would look identical to "no data at all".
    assert facts.compacts == 0
    assert facts.last_compaction is None


def test_steerable_is_none_when_the_journal_is_unreadable() -> None:
    service = SessionFactsService(runner=FakeRunner(returncode=1))
    facts = service.facts_for_stem("hermes")
    assert facts.steerable is None


def test_steerable_true_false_from_a_fake_runner() -> None:
    lines = (FIXTURES / "steering_journal_lines.jsonl").read_text(encoding="utf-8")
    claude_line = next(
        raw for raw in lines.splitlines() if json.loads(raw)["_SYSTEMD_USER_UNIT"] == "buzz-agent@claude.service"
    )
    service = SessionFactsService(
        claude_projects_dir=Path("/nonexistent"),
        runner=FakeRunner(claude_line, returncode=0),
    )
    facts = service.facts_for_stem("hermes")
    assert facts.steerable is True


# --- caching --------------------------------------------------------------


def test_facts_are_cached_within_the_ttl() -> None:
    calls = []

    class CountingRunner(FakeRunner):
        def run(self, args, *, timeout=None):
            calls.append(1)
            return super().run(args, timeout=timeout)

    clock = {"t": 1000.0}
    service = SessionFactsService(
        claude_projects_dir=Path("/nonexistent"),
        runner=CountingRunner(returncode=1),
        clock=lambda: clock["t"],
        ttl_seconds=10.0,
    )
    service.facts_for_stem("hermes")
    service.facts_for_stem("hermes")
    assert len(calls) == 1
    clock["t"] += 11.0
    service.facts_for_stem("hermes")
    assert len(calls) == 2


# --- percent / payload shape ------------------------------------------


def test_as_payload_shape() -> None:
    from hermes_cli.agent_session_facts import LastCompaction, SessionFacts

    facts = SessionFacts(
        stem="claude",
        source="claude",
        session_id="sid-1",
        started_at=1_800_000_000.0,
        prompt_tokens=530869,
        context_window=1_000_000,
        compacts=1,
        last_compaction=LastCompaction(pre=342799, post=13860, at=1_800_000_010.0),
        steerable=True,
    )
    payload = facts.as_payload(now=1_800_000_100.0)
    assert payload["percent"] == pytest.approx(53.09, abs=0.01)
    assert payload["source"] == "claude"
    assert payload["compacts"] == 1
    assert payload["last_compaction"]["pre"] == 342799
    assert payload["steerable"] is True
