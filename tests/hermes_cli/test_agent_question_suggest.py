from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import agent_question_suggest as suggest
from hermes_cli import agent_questions as aq


def _question(qdb: Path) -> int:
    event_id = aq.insert_question_event(
        session="work",
        window="claude",
        pane_id="%42",
        fingerprint="fp-suggest",
        question_text="Which rollout strategy should we use?",
        options=[
            {"nr": 1, "label": "Rolling", "recommended": False},
            {"nr": 2, "label": "Blue-green", "recommended": True},
        ],
        kind="claude",
        cwd="/tmp/project",
        db_path=qdb,
    )
    assert event_id is not None
    return int(event_id)


def _response(content: str, *, model: str = "gpt-5.6-terra") -> SimpleNamespace:
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
    )


def test_precompute_persists_real_ranked_json_and_latency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qdb = tmp_path / "question_events.db"
    event_id = _question(qdb)
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(suggest, "_build_context", lambda *_args, **_kwargs: "bounded context")
    monkeypatch.setattr(
        suggest,
        "load_config",
        lambda: {"agent_questions": {"suggest": {"model": "gpt-5.6-terra"}}},
    )

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _response(
            json.dumps(
                {
                    "ranked": [
                        {"nr": 2, "rationale": "Safer zero-downtime cutover."},
                        {"nr": 1, "rationale": "Simpler rollback path."},
                    ],
                    "confidence": "high",
                }
            )
        )

    monkeypatch.setattr(suggest, "call_llm", fake_call_llm)

    assert suggest.precompute_question_suggestion(event_id, db_path=qdb) is True

    with aq.connect_closing(db_path=qdb) as conn:
        row = conn.execute("SELECT * FROM question_events WHERE id = ?", (event_id,)).fetchone()
    assert row is not None
    assert json.loads(row["suggestions_json"]) == [
        {"nr": 2, "rationale": "Safer zero-downtime cutover."},
        {"nr": 1, "rationale": "Simpler rollback path."},
    ]
    assert row["suggested_by"] == "gpt-5.6-terra"
    assert row["suggest_confidence"] == "high"
    assert row["suggested_ts"] is not None
    assert row["suggest_latency_ms"] >= 0
    assert calls[0]["task"] == "agent_question_suggest"
    assert calls[0]["model"] == "gpt-5.6-terra"
    assert calls[0]["extra_body"] == {"response_format": {"type": "json_object"}}


def test_precompute_llm_exception_leaves_event_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qdb = tmp_path / "question_events.db"
    event_id = _question(qdb)
    monkeypatch.setattr(suggest, "_build_context", lambda *_args, **_kwargs: "bounded context")
    monkeypatch.setattr(suggest, "load_config", lambda: {})

    def explode(**_kwargs):
        raise TimeoutError("model timeout")

    monkeypatch.setattr(suggest, "call_llm", explode)

    assert suggest.precompute_question_suggestion(event_id, db_path=qdb) is False

    with aq.connect_closing(db_path=qdb) as conn:
        row = conn.execute("SELECT * FROM question_events WHERE id = ?", (event_id,)).fetchone()
    assert row is not None
    assert row["suggestions_json"] is None
    assert row["suggested_by"] is None
    assert row["suggested_ts"] is None
    assert row["suggest_latency_ms"] is None
    assert row["suggest_confidence"] is None


def test_context_cap_preserves_required_section_order() -> None:
    context = suggest._bounded_context(
        question_text="Q" * 8_000,
        options=[{"nr": 1, "label": "Option one", "recommended": True}],
        task={"title": "Task", "body": "Body", "acceptance_criteria": "AC"},
        task_events=[{"kind": "created", "payload": "event"}],
        receipts=[{"title": "Receipt", "excerpt": "done"}],
        cwd="/tmp/project",
        kind="claude",
    )

    assert len(context) <= suggest._CONTEXT_CHAR_CAP
    assert context.index("QUESTION") < context.index("OPTIONS")
    assert context.index("OPTIONS") < context.index("OWNING TASK")
    assert context.index("OWNING TASK") < context.index("LAST TASK EVENTS")
    assert context.index("LAST TASK EVENTS") < context.index("PROJECT RECEIPTS")
    assert context.index("PROJECT RECEIPTS") < context.index("RUNTIME")
