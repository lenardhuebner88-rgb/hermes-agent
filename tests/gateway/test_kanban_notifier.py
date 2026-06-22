import asyncio
from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter, platform=Platform.TELEGRAM):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {platform: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _create_completed_subscription(
    summary="done once",
    platform="telegram",
    chat_id="chat-1",
    *,
    title="notify once",
    result=None,
    metadata=None,
):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title=title, assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat_id)
        kb.complete_task(conn, tid, result=result, summary=summary, metadata=metadata)
        return tid
    finally:
        conn.close()


def _patch_kanban_reporting_config(
    monkeypatch,
    *,
    reporting_channel_id="1495737862522405088",
    orchestrator_channel_id="1500203113867378789",
):
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "reporting_channel_id": reporting_channel_id,
                "orchestrator_channel_id": orchestrator_channel_id,
            }
        },
    )


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Task:" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()
    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_discord_completed_notification_includes_result_summary_body(tmp_path, monkeypatch):
    """Discord Kanban done pings must surface research results, not only a one-line activity note."""
    db_path = tmp_path / "discord-result-summary.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    summary = """Ich habe drei Inferenz-Frameworks verglichen.

Ergebnisse:
- vLLM gewinnt beim Gesamtdurchsatz: 1.000 tok/s im Testfenster.
- SGLang ist nur bei Kurzprompts vorne: 85 ms median latency.
- Empfehlung: vLLM einsetzen; SGLang nur für Low-Latency-Sonderfälle prüfen.
"""
    tid = _create_completed_subscription(summary=summary, platform="discord")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter, platform=Platform.DISCORD)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert tid in text
    assert "Ergebnisse:" in text
    assert "vLLM gewinnt beim Gesamtdurchsatz" in text
    assert "Empfehlung: vLLM einsetzen" in text


def test_discord_completed_report_routes_to_reporting_channel_with_human_template(tmp_path, monkeypatch):
    db_path = tmp_path / "discord-reporting-channel.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _patch_kanban_reporting_config(monkeypatch)

    tid = _create_completed_subscription(
        summary="Kurzfazit: Reporting wurde vereinheitlicht.\nDetails: Tests decken Routing und Format ab.",
        platform="discord",
        chat_id="1500203113867378789",
        title="einheitliches Reporting",
        metadata={
            "artifacts": ["/tmp/reporting-result.md"],
            "changed_files": ["gateway/kanban_watchers.py"],
            "open_points": ["Gateway nach Deployment neu starten"],
        },
    )

    adapter = RecordingAdapter()
    runner = _make_runner(adapter, platform=Platform.DISCORD)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    sent = adapter.sent[0]
    assert sent["chat_id"] == "1495737862522405088"
    assert sent["chat_id"] != "1500203113867378789"
    text = sent["text"]
    lines = text.splitlines()
    # Compact K1 format: header / Kurzfazit / Task / Status / Ergebnis body / Dashboard link.
    assert lines[0] == "✅ Hermes Report — Task abgeschlossen"
    assert lines[1] == "Kurzfazit: Reporting wurde vereinheitlicht."
    assert f"Task: {tid} — einheitliches Reporting" in text
    assert "Status: done | Profil: worker" in text
    assert "Ergebnis:" in text
    # The result body must still surface in full.
    assert "Details: Tests decken Routing und Format ab." in text
    # A clickable dashboard deep-link replaces the legacy link section.
    assert f"Dashboard: http://127.0.0.1:9119/control/backlog?focus={tid}" in text
    assert "Laufzeit: 0s" not in text
    # K1 de-flood regression guard: the old multi-section bloat is gone. The
    # report must NOT grow per-metadata-list again (audit finding D4).
    assert "Wichtigste Ergebnisse/Änderungen:" not in text
    assert "Artefakte/Links:" not in text
    assert "Offene Punkte/Blocker:" not in text
    # Artifact paths are delivered as native uploads, not inlined as text.
    assert "/tmp/reporting-result.md" not in text


def test_discord_completed_reporting_channel_dedupes_multiple_subscriptions_to_same_target(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "discord-reporting-channel-dedup.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _patch_kanban_reporting_config(monkeypatch)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="single report", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="discord",
            chat_id="1500203113867378789",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="discord",
            chat_id="1495737862522405088",
        )
        kb.complete_task(conn, tid, summary="Abschluss erledigt.")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter, platform=Platform.DISCORD)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "1495737862522405088"
    assert adapter.sent[0]["text"].startswith("✅ Hermes Report — Task abgeschlossen")


def test_discord_received_event_routes_to_reporting_channel(tmp_path, monkeypatch):
    db_path = tmp_path / "discord-received-report.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _patch_kanban_reporting_config(monkeypatch)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="neuer Auftrag", assignee="default")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="discord",
            chat_id="1500203113867378789",
        )
        kb._append_event(
            conn,
            tid,
            kind="received",
            payload={"summary": "Auftrag via Dashboard eingegangen", "source": "dashboard"},
        )
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
        conn.commit()
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter, platform=Platform.DISCORD)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    sent = adapter.sent[0]
    assert sent["chat_id"] == "1495737862522405088"
    text = sent["text"]
    assert text.startswith("📥 Hermes Report")
    assert "Kurzfazit" in text
    assert f"Task: {tid} — neuer Auftrag" in text
    assert "Status: eingegangen" in text
    assert "Status: done" not in text
    assert "Auftrag via Dashboard eingegangen" in text
    assert "Nächster Schritt" in text


def test_discord_planned_retry_events_do_not_route_to_reporting_channel(tmp_path, monkeypatch):
    db_path = tmp_path / "discord-planned-retry-routing.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _patch_kanban_reporting_config(monkeypatch)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="retry workflow", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="discord",
            chat_id="1500203113867378789",
        )
        kb._append_event(conn, tid, kind="timed_out", payload={"limit_seconds": 60})
        kb._append_event(conn, tid, kind="crashed", payload={"pid": 1234})
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter, platform=Platform.DISCORD)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2
    assert {sent["chat_id"] for sent in adapter.sent} == {"1500203113867378789"}
    assert "1495737862522405088" not in {sent["chat_id"] for sent in adapter.sent}
    assert "timed out" in adapter.sent[0]["text"].lower()
    assert "crashed" in adapter.sent[1]["text"].lower()


def test_discord_gave_up_failure_routes_to_reporting_channel(tmp_path, monkeypatch):
    db_path = tmp_path / "discord-gave-up-routing.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _patch_kanban_reporting_config(monkeypatch)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="final failure", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="discord",
            chat_id="1500203113867378789",
        )
        kb._append_event(conn, tid, kind="gave_up", payload={"trigger_outcome": "timed_out"})
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter, platform=Platform.DISCORD)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "1495737862522405088"
    assert "gave up" in adapter.sent[0]["text"]


def test_discord_reporting_config_missing_does_not_fall_back_to_orchestrator(tmp_path, monkeypatch):
    db_path = tmp_path / "discord-missing-reporting-channel.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _patch_kanban_reporting_config(
        monkeypatch,
        reporting_channel_id="",
        orchestrator_channel_id="1500203113867378789",
    )

    _create_completed_subscription(
        summary="done",
        platform="discord",
        chat_id="1500203113867378789",
    )

    adapter = RecordingAdapter()
    runner = _make_runner(adapter, platform=Platform.DISCORD)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []


def test_kanban_notifier_failure_text_uses_event_specific_fallbacks(tmp_path, monkeypatch):
    db_path = tmp_path / "event-specific-failures.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="retry me", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(conn, tid, kind="timed_out", payload={})
        kb._append_event(conn, tid, kind="gave_up", payload={"trigger_outcome": "timed_out"})
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2
    timed_out_text = adapter.sent[0]["text"]
    gave_up_text = adapter.sent[1]["text"]
    assert "max_runtime=0s" not in timed_out_text
    assert "timed out" in timed_out_text.lower()
    assert "repeated spawn failures" not in gave_up_text
    assert "timed out" in gave_up_text.lower()


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


def test_notifier_delivers_decompose_child_terminal_state(tmp_path, monkeypatch):
    """H1 end-to-end: a child created by auto-decompose inherits the root's
    notify-subscription, so the child's own terminal state (here: blocked)
    reaches the originating chat via the gateway notifier — no manual
    notify-subscribe. This is the bug from task t_8ae7534a: a blocked
    decompose-child never pinged the user.
    """
    db_path = tmp_path / "decompose-child.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="ship a feature", triage=True)
        kb.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="chat-1")
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "build it", "assignee": "engineer", "parents": []}],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 1
        child = child_ids[0]
        # Parentless child was promoted ready -> drive it to a terminal block.
        assert kb.get_task(conn, child).status == "ready"
        assert kb.block_task(conn, child, reason="needs human input")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, (
        f"blocked decompose-child should ping the inherited chat; "
        f"got {len(adapter.sent)} deliveries"
    )
    sent = adapter.sent[0]
    assert sent["chat_id"] == "chat-1"
    assert child in sent["text"]
    assert "blocked" in sent["text"].lower()


@pytest.mark.parametrize("gw_profile,expect_sends", [("coordinator", 1), ("reviewer", 0)])
def test_decompose_child_inherited_sub_respects_notifier_profile_ownership(
    tmp_path, monkeypatch, gw_profile, expect_sends
):
    """FU-1/FU-2: a decompose-child inherits the root's notifier_profile verbatim,
    and the real watcher only delivers when the gateway's own profile matches that
    inherited owner_profile (coordinator) — a foreign-profile gateway (reviewer)
    skips the inherited sub via the owner_profile ownership gate in gateway/run.py.

    Extends test_notifier_delivers_decompose_child_terminal_state (which covers the
    profile-less sub) with the profile-routing dimension that the live delivery path
    actually exercises.
    """
    db_path = tmp_path / "owned.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="ship a feature", triage=True)
        kb.add_notify_sub(
            conn,
            task_id=root,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile="coordinator",
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "build it", "assignee": "engineer", "parents": []}],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 1
        child = child_ids[0]
        # FU-2 white-box: the inherited sub carries the root's owner profile verbatim.
        inherited = kb.list_notify_subs(conn, child)
        assert len(inherited) == 1
        assert inherited[0]["notifier_profile"] == "coordinator"
        # Parentless child was promoted ready -> drive it to a terminal block.
        assert kb.get_task(conn, child).status == "ready"
        assert kb.block_task(conn, child, reason="needs human input")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    # Critical: _make_runner does not set this; the watcher would otherwise fall
    # back to self._active_profile_name(). A truthy value pins the gateway's
    # profile deterministically so the ownership gate is what's under test.
    runner._kanban_notifier_profile = gw_profile
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == expect_sends, (
        f"gateway profile {gw_profile!r} vs inherited owner 'coordinator' should "
        f"yield {expect_sends} deliveries; got {len(adapter.sent)}"
    )
    if expect_sends:
        sent = adapter.sent[0]
        assert sent["chat_id"] == "chat-1"
        assert child in sent["text"]
        assert "blocked" in sent["text"].lower()


# ---------------------------------------------------------------------------
# K2 — per-tree completed-report aggregation
# ---------------------------------------------------------------------------


def test_tree_root_completion_emits_one_consolidated_report(tmp_path, monkeypatch):
    """A decomposed tree emits ONE consolidated completed report at the root
    (sink) and suppresses the per-child completed pings (K2 de-flood)."""
    db_path = tmp_path / "tree-consolidated.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="ship the feature", triage=True)
        # Sub on the root BEFORE decompose so children inherit it (_inherit_notify_subs).
        kb.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="chat-1")
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "build module A", "assignee": "coder", "parents": []},
                {"title": "build module B", "assignee": "coder", "parents": []},
            ],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 2
        a, b = child_ids
        # Work children complete first; the root (sink) completes last.
        kb.complete_task(conn, a, summary="Modul A gebaut.")
        kb.complete_task(conn, b, summary="Modul B gebaut.")
        kb.complete_task(conn, root, summary="Auftrag fertig: alles zusammengeführt.")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The two interior work tasks are suppressed; ONLY the root's consolidated
    # report goes out — one delivery, not three.
    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert "Teilaufgaben" in text
    assert root in text
    assert a in text and b in text
    assert "Auftrag fertig" in text  # root result body still surfaces
    # De-flood guard: exactly one report header, no per-child completed posts.
    assert text.count("Hermes Report") == 1


def test_tree_interior_completion_is_suppressed(tmp_path, monkeypatch):
    """An interior work node's completed report is suppressed (rolled into the
    root). With only the interior task subscribed, nothing is sent."""
    db_path = tmp_path / "tree-interior.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="root orchestration", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "interior work", "assignee": "coder", "parents": []}],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 1
        a = child_ids[0]
        # No root sub was added, so the child inherited nothing. Subscribe ONLY
        # the interior work task to isolate the suppression behaviour.
        kb.add_notify_sub(conn, task_id=a, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, a, summary="interior done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Interior completion rolls into the (here unsubscribed) root → nothing sent.
    assert adapter.sent == []


def test_standalone_completion_still_sends_single_report(tmp_path, monkeypatch):
    """A task with no task_links is untouched by K2 — it still gets the normal
    per-task completed report."""
    db_path = tmp_path / "standalone-report.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription(summary="Solo erledigt.")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert text.startswith("✅ Hermes Report — Task abgeschlossen")
    assert "Teilaufgaben" not in text
    assert tid in text


def _patch_stall_flush_config(monkeypatch, *, hours=0):
    """Patch load_config so the F1 stall-flush threshold is ``hours`` old."""
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "descendants_blocked_parent_hours": hours,
            }
        },
    )


def _build_stalled_tree(db_path, monkeypatch):
    """Root (sink) with A,B completed (suppressed) and C sticky-blocked → root
    can never complete. Returns (root, a, b, c)."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="ship the feature", triage=True)
        kb.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="chat-1")
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "build module A", "assignee": "coder", "parents": []},
                {"title": "build module B", "assignee": "coder", "parents": []},
                {"title": "build module C", "assignee": "coder", "parents": []},
            ],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 3
        a, b, c = child_ids
        kb.complete_task(conn, a, summary="Modul A gebaut.")  # suppressed by K2
        kb.complete_task(conn, b, summary="Modul B gebaut.")  # suppressed by K2
        assert kb.block_task(conn, c, reason="review-required: needs operator")
        return root, a, b, c
    finally:
        conn.close()


def test_stalled_root_flushes_suppressed_child_successes(tmp_path, monkeypatch):
    """F1: when a root can never complete (a member is sticky-blocked), the
    child-successes K2 suppressed are flushed once as a trailing report."""
    db_path = tmp_path / "stalled-flush.db"
    root, a, b, c = _build_stalled_tree(db_path, monkeypatch)
    _patch_stall_flush_config(monkeypatch, hours=0)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    flushes = [s for s in adapter.sent if "steckt fest" in s["text"]]
    assert len(flushes) == 1
    text = flushes[0]["text"]
    # The two swallowed successes are surfaced; the blocked member is shown too.
    assert a in text and b in text
    assert "Modul A gebaut" in text and "Modul B gebaut" in text
    assert root in text
    # The blocked member still gets its own per-task failure ping (K2 keeps it).
    assert any("blocked" in s["text"] for s in adapter.sent)


def test_stalled_root_flush_is_not_double_posted(tmp_path, monkeypatch):
    """F1: a second tick must not re-flush a root already flushed (dedup)."""
    db_path = tmp_path / "stalled-flush-dedup.db"
    _build_stalled_tree(db_path, monkeypatch)
    _patch_stall_flush_config(monkeypatch, hours=0)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    first = [s for s in adapter.sent if "steckt fest" in s["text"]]
    assert len(first) == 1

    # Second tick: no new stall flush, even though the root is still stalled.
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    all_flushes = [s for s in adapter.sent if "steckt fest" in s["text"]]
    assert len(all_flushes) == 1


def test_active_root_with_pending_work_does_not_flush(tmp_path, monkeypatch):
    """F1: a root that can still complete (no dead-end member) is left alone —
    its suppressed successes wait for K2's consolidated report."""
    db_path = tmp_path / "active-no-flush.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="ship it", triage=True)
        kb.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="chat-1")
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "build A", "assignee": "coder", "parents": []},
                {"title": "build B", "assignee": "coder", "parents": []},
            ],
            author="decomposer",
        )
        a, _b = child_ids
        kb.complete_task(conn, a, summary="A fertig.")  # suppressed; B still open
    finally:
        conn.close()
    _patch_stall_flush_config(monkeypatch, hours=0)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert not any("steckt fest" in s["text"] for s in adapter.sent)


def test_auto_receipt_written_on_done(tmp_path, monkeypatch):
    """K12: a task reaching terminal ``done`` drops a `<task_id>.md` receipt
    into HERMES_AUTO_RECEIPT_DIR containing the task id and title.
    """
    db_path = tmp_path / "auto-receipt-done.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    receipt_dir = tmp_path / "receipts"
    monkeypatch.setenv("HERMES_AUTO_RECEIPT_DIR", str(receipt_dir))
    detailed_result = (
        "Step 1: implementation completed with evidence. "
        "Step 2: targeted tests passed and the changed behavior was verified. "
        "Step 3: reviewer-facing handoff includes files changed, commands run, "
        "and residual risk, so the receipt contains a real result over 200 characters."
    )

    tid = _create_completed_subscription(
        summary="Kurz erledigt.",
        result=detailed_result,
        title="ship the receipt feature",
    )

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Normal delivery is unchanged by the receipt write.
    assert len(adapter.sent) == 1

    receipt = receipt_dir / f"{tid}.md"
    assert receipt.exists(), f"expected auto-receipt at {receipt}"
    content = receipt.read_text(encoding="utf-8")
    assert tid in content
    assert "ship the receipt feature" in content
    assert "Step-Ledger" in content
    assert detailed_result in content
    assert "Kurz erledigt." in content


def test_auto_receipt_written_on_gave_up(tmp_path, monkeypatch):
    """A final retry/timeout failure still needs a crash-safe task receipt."""
    db_path = tmp_path / "auto-receipt-gave-up.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    receipt_dir = tmp_path / "receipts"
    monkeypatch.setenv("HERMES_AUTO_RECEIPT_DIR", str(receipt_dir))

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="timeout failure", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(
            conn,
            tid,
            kind="gave_up",
            payload={
                "trigger_outcome": "timed_out",
                "error": "worker exceeded max runtime after two attempts",
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1

    receipt = receipt_dir / f"{tid}.md"
    assert receipt.exists(), f"expected failure auto-receipt at {receipt}"
    content = receipt.read_text(encoding="utf-8")
    assert "status: gave_up" in content
    assert "Step-Ledger" in content
    assert "timed out" in content.lower()
    assert "worker exceeded max runtime" in content


def test_auto_receipt_fail_soft_unwritable_dir(tmp_path, monkeypatch):
    """K12: an impossible receipt dir (a path *under a regular file*, so
    mkdir raises NotADirectoryError) must not break the tick or change
    delivery — the write is swallowed and the done ping still goes out.
    """
    db_path = tmp_path / "auto-receipt-failsoft.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    # A regular file; using it as a parent directory makes mkdir() raise.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    bad_dir = blocker / "nested" / "receipts"
    monkeypatch.setenv("HERMES_AUTO_RECEIPT_DIR", str(bad_dir))

    tid = _create_completed_subscription(title="fail-soft check")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    # Must not raise despite the unwritable receipt dir.
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Delivery is untouched by the swallowed receipt failure.
    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]
    # No stray receipt file got created.
    assert not bad_dir.exists()
