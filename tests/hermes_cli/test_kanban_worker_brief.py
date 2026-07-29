from __future__ import annotations

import json
import stat
from contextlib import contextmanager

from hermes_cli import kanban_context as context
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose


def _input(*, body: str = "implement AC", relative: str = "2m ago") -> context.WorkerBriefInput:
    return context.WorkerBriefInput(
        task_id="t_brief",
        title="brief",
        header="# task",
        sections={
            "assignment": [context.BriefRecord(body, key="body")],
            "parent_evidence": [context.BriefRecord(f"parent completed {relative}", key="parent")],
        },
    )


def test_runtime_audience_does_not_change_shared_payload_fingerprint():
    spec = context.WorkerBriefInput(
        task_id="t_shared",
        title="Shared payload",
        header="# Shared",
        sections={"assignment": [context.BriefRecord("same assignment")]},
    )

    hermes = context.render_worker_brief(
        spec, phase="execute", audience="hermes", profile="worker_slim"
    )
    claude = context.render_worker_brief(
        spec, phase="execute", audience="claude-cli", profile="worker_slim"
    )

    assert hermes.payload == claude.payload
    assert hermes.manifest["payload_fingerprint"] == claude.manifest["payload_fingerprint"]
    assert hermes.manifest["audience"] == "hermes"
    assert claude.manifest["audience"] == "claude-cli"


def test_review_lane_phase_takes_precedence_over_continuation_counter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="verify retry", assignee="verifier")
        conn.execute(
            "UPDATE tasks SET continuation_count = 1 WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        task = kb.get_task(conn, task_id)
        assert task is not None

        assert kb._worker_brief_phase(conn, task) == "verify"
    finally:
        conn.close()


def test_render_worker_brief_prioritizes_assignment_and_omits_at_record_boundaries():
    task = context.WorkerBriefInput(
        task_id="t_brief",
        title="brief",
        header="# task",
        sections={
            "assignment": [context.BriefRecord("MANDATORY AC and scope", key="ac")],
            "parent_evidence": [
                context.BriefRecord("small parent", key="small"),
                context.BriefRecord("x" * 11_000, key="large"),
            ],
            "comments": [context.BriefRecord("operator comment", key="comment")],
        },
    )

    rendered = context.render_worker_brief(
        task, phase="execute", audience="hermes", profile="worker_slim"
    )

    assert rendered.payload.index("MANDATORY AC") < rendered.payload.index("small parent")
    assert "1 record(s) omitted at record boundaries" in rendered.payload
    assert rendered.manifest["section_counts"]["parent_evidence"] == {
        "available": 2,
        "included": 1,
        "omitted": 1,
        "included_chars": len("small parent"),
    }
    assert rendered.overflows["parent_evidence"].endswith("x" * 11_000 + "\n")


def test_worker_brief_fingerprint_is_canonical_but_phase_profile_and_ac_sensitive():
    first = context.render_worker_brief(
        _input(relative="2m ago"), phase="execute", audience="hermes", profile="worker_slim"
    )
    relative_only = context.render_worker_brief(
        _input(relative="3h ago"), phase="execute", audience="hermes", profile="worker_slim"
    )
    retry = context.render_worker_brief(
        _input(relative="2m ago"), phase="retry", audience="hermes", profile="retry"
    )
    changed_ac = context.render_worker_brief(
        _input(body="implement changed AC"), phase="execute", audience="hermes", profile="worker_slim"
    )

    assert first.manifest["payload_fingerprint"] == relative_only.manifest["payload_fingerprint"]
    assert first.manifest["payload_fingerprint"] != retry.manifest["payload_fingerprint"]
    assert first.manifest["payload_fingerprint"] != changed_ac.manifest["payload_fingerprint"]


def test_operator_directives_are_never_byte_or_count_capped_in_any_profile():
    longest_comment_cap = max(
        int(caps["comment_bytes"])
        for caps in context._CTX_CAP_PROFILES.values()
    )
    regular_count = max(
        int(caps["comments"])
        for caps in context._CTX_CAP_PROFILES.values()
    ) + 1
    directive = "DIRECTIVE-" + ("x" * (longest_comment_cap + 1))
    comments = [
        *[
            type(
                "Comment",
                (),
                {"body": f"regular {index}", "author": "worker", "created_at": 0, "kind": "comment"},
            )()
            for index in range(regular_count)
        ],
        type(
            "Comment",
            (),
            {"body": directive, "author": "operator", "created_at": 0, "kind": "directive"},
        )(),
    ]

    for profile, caps in context._CTX_CAP_PROFILES.items():
        rendered = "\n".join(
            context.render_comment_thread(
                comments,
                max_comments=int(caps["comments"]),
                comment_bytes=int(caps["comment_bytes"]),
            )
        )

        assert directive in rendered, profile
        assert "DIRECTIVE-… [truncated" not in rendered, profile
        assert f"regular {regular_count - 1}" in rendered, profile
        assert "regular 0" not in rendered, profile


def test_large_operator_directive_survives_worker_brief_section_budget(kanban_home):
    directive = "DIRECTIVE-" + ("x" * 20_000)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="directive target", assignee="coder")
        kb.add_comment(conn, task_id, "operator", directive, kind="directive")
        rendered = kb.render_worker_brief_for_task(conn, task_id)

    assert directive in rendered.payload
    assert rendered.manifest["section_counts"]["comments"]["omitted"] == 0


def test_review_diff_overflow_is_atomic_0600_artifact_and_not_logged(
    kanban_home, monkeypatch
):
    full_diff = (
        "diff --git a/first.py b/first.py\n"
        + ("+line\n" * 4_000)
        + "diff --git a/last.py b/last.py\n+late-file\n"
    )
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="review", assignee="reviewer")
        kb.claim_task(conn, task_id)
        task = kb.get_task(conn, task_id)

    def fake_input(conn, task, *, phase, profile):
        return context.WorkerBriefInput(
            task_id=task.id,
            title=task.title,
            header="# review task",
            sections={
                "assignment": [context.BriefRecord("Review the candidate")],
                "review_diff": [
                    context.BriefRecord(
                        "```diff\n" + full_diff + "```",
                        canonical_text=full_diff,
                        key="immutable-review-diff",
                    )
                ],
            },
        )

    monkeypatch.setattr(kb, "_worker_brief_input", fake_input)
    real_render = kb.render_worker_brief_for_task
    render_calls = 0

    def tracked_render(*args, **kwargs):
        nonlocal render_calls
        render_calls += 1
        return real_render(*args, **kwargs)

    monkeypatch.setattr(kb, "render_worker_brief_for_task", tracked_render)
    launched = kb._prepare_worker_brief_launch(task, board=None, audience="claude-cli")

    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (task.current_run_id,)
        ).fetchone()
        metadata = json.loads(row["metadata"])
        events = kb.list_events(conn, task_id)

    artifact = metadata["brief_artifacts"][0]
    artifact_path = kanban_home / "reports" / "by-run" / artifact["path"].rsplit("/", 1)[-1]
    assert artifact_path.read_text().endswith(
        "diff --git a/last.py b/last.py\n+late-file\n```\n"
    )
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    assert str(artifact_path) in launched.payload
    assert "1 record(s) omitted at record boundaries" in launched.payload
    assert "late-file" not in json.dumps(metadata)
    assert "late-file" not in json.dumps([event.payload for event in events])
    assert metadata["brief"]["phase"] == "review"
    assert metadata["brief"]["profile"] == "reviewer_review"
    assert metadata["brief"]["payload_fingerprint"]
    assert "requested_provider" in metadata
    assert "actual_model" in metadata
    assert render_calls == 1
    brief_event = next(event for event in events if event.kind == "brief_rendered")
    assert brief_event.payload is not None
    assert brief_event.payload["payload_fingerprint"] == metadata["brief"]["payload_fingerprint"]
    assert brief_event.payload["comment_id_watermark"] == metadata["brief"]["comment_id_watermark"]


def test_operator_context_is_bounded_operator_profile(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="operator", body="body")
        rendered = kb.render_worker_brief_for_task(conn, task_id, audience="operator")
    assert rendered.manifest["profile"] == "operator_detail"
    assert rendered.manifest["phase"] == "execute"


def test_worker_brief_frontloads_stale_evidence_freshness(kanban_home, monkeypatch, tmp_path):
    stamped_sha = "0123456789abcdef"
    current_sha = "fedcba9876543210"
    monkeypatch.setattr(kb, "_git_head_sha_for_workspace", lambda _path: current_sha)
    monkeypatch.setattr(kb, "_git_commit_distance", lambda _older, _newer, _path: 3)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="stale evidence",
            body="Reproduce the recorded failure before changing code.",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            scope_contract={"evidence_freshness": {
                "commit": stamped_sha,
                "recorded_at": "2026-07-28T01:00:00Z",
                "test_files": ["tests/unit/test_evidence.py"],
            }},
        )
        rendered = kb.render_worker_brief_for_task(conn, task_id)

    assert "## Evidence freshness: reproduce first" in rendered.payload
    assert stamped_sha in rendered.payload
    assert current_sha in rendered.payload
    assert "3 commit(s)" in rendered.payload
    assert rendered.payload.index("Evidence freshness") < rendered.payload.index(
        "Reproduce the recorded failure"
    )


def test_lane_descriptions_use_active_runtime_provider_and_model(monkeypatch):
    monkeypatch.setattr(
        kb,
        "_active_lane_entry_for_profile",
        lambda profile: {
            "worker_runtime": "configured-runtime",
            "provider": "configured-provider",
            "model": "configured-model",
        },
    )
    reason = kb._reason_for_lane("premium")
    assert "configured-runtime / configured-provider / configured-model" in reason
    assert "Claude" not in reason and "Opus" not in reason

    @contextmanager
    def fake_connect():
        yield object()

    monkeypatch.setattr(kanban_decompose.kb, "connect_closing", fake_connect)
    monkeypatch.setattr(
        kanban_decompose.kb,
        "get_active_lane",
        lambda conn: {
            "profiles": {
                "coder": {"worker_runtime": "rt-c", "provider": "p-c", "model": "m-c"},
                "premium": {"worker_runtime": "rt-p", "provider": "p-p", "model": "m-p"},
            }
        },
    )
    prompt = kanban_decompose._system_prompt()
    assert "rt-c / p-c / m-c" in prompt
    assert "rt-p / p-p / m-p" in prompt
    assert "OpenAI-Codex/GPT" not in prompt
    assert "claude-cli on the Claude Max" not in prompt
