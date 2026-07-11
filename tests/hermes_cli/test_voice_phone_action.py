import asyncio
from pathlib import Path

import pytest

from hermes_cli.voice_phone_action import PhoneActionBroker, validate_phone_action
from tools.voice_live_tools import FUNCTION_DECLARATIONS, NON_BLOCKING_TOOLS, VoiceToolExecutor

CLIENT_HTML = Path(__file__).parents[2] / "hermes_cli" / "voice_client" / "index.html"


@pytest.mark.parametrize("url", ["http://example.com", "javascript:alert(1)", "file:///tmp/x", "content://x", "https://u:p@example.com", "https://exa mple.com"])
def test_phone_action_rejects_non_allowlisted_urls(url):
    action, error = validate_phone_action({"action": "open_url", "url": url})
    assert action is None
    assert error


def test_phone_action_accepts_bounded_https_and_rejects_limits_and_extra_fields():
    assert validate_phone_action({"action": "open_url", "url": "https://example.com/a?q=1"})[0]
    assert validate_phone_action({"action": "copy_text", "text": "x" * 4096})[0]
    assert validate_phone_action({"action": "copy_text", "text": "x" * 4097})[0] is None
    assert validate_phone_action({"action": "share_text", "text": "x" * 8193})[0] is None
    assert validate_phone_action({"action": "copy_text", "text": "ok", "url": "https://evil"})[0] is None
    assert validate_phone_action({"action": "open_app", "app": "wifi"})[0] == {
        "action": "open_app", "app": "wifi",
    }
    assert validate_phone_action({"action": "open_app", "app": "com.android.settings"})[0] is None


@pytest.mark.asyncio
async def test_broker_requires_correlated_confirmation_and_native_result_exactly_once():
    events = []
    async def emit(event):
        events.append(event)
        return True
    broker = PhoneActionBroker(emit, timeout=1)
    task = asyncio.create_task(broker.request({"action": "copy_text", "text": "secret"}))
    await asyncio.sleep(0)
    request_id = events[0]["request_id"]
    assert events[0]["preview"] == "secret"
    assert await broker.handle_control({"type": "phone_action_decision", "request_id": "stale", "decision": "confirmed"})
    assert len(events) == 1
    await broker.handle_control({"type": "phone_action_decision", "request_id": request_id, "decision": "confirmed"})
    assert events[-1]["type"] == "phone_action_execute"
    assert events[-1]["text"] == "secret"
    await broker.handle_control({"type": "phone_action_decision", "request_id": request_id, "decision": "confirmed"})
    assert len(events) == 2
    await broker.handle_control({"type": "phone_action_result", "request_id": request_id, "status": "executed"})
    await broker.handle_control({"type": "phone_action_result", "request_id": request_id, "status": "executed"})
    assert await task == {"status": "executed"}


@pytest.mark.asyncio
async def test_broker_cancel_timeout_and_single_pending():
    async def emit(_event): return True
    broker = PhoneActionBroker(emit, timeout=0.01)
    first = asyncio.create_task(broker.request({"action": "copy_text", "text": "a"}))
    await asyncio.sleep(0)
    assert await broker.request({"action": "copy_text", "text": "b"}) == {"status": "failed", "code": "phone_action_busy"}
    assert await first == {"status": "timeout"}
    second = asyncio.create_task(broker.request({"action": "share_text", "text": "c"}))
    await asyncio.sleep(0)
    broker.cancel()
    assert await second == {"status": "cancelled"}


@pytest.mark.asyncio
async def test_broker_terminal_state_cancels_blocked_execute_delivery():
    events = []
    execute_started = asyncio.Event()

    async def emit(event):
        if event["type"] == "phone_action_execute":
            execute_started.set()
            await asyncio.Event().wait()
        events.append(event)
        return True

    broker = PhoneActionBroker(emit, timeout=1)
    request = asyncio.create_task(broker.request({"action": "copy_text", "text": "x"}))
    await asyncio.sleep(0)
    request_id = events[0]["request_id"]
    decision = asyncio.create_task(broker.handle_control({
        "type": "phone_action_decision", "request_id": request_id, "decision": "confirmed",
    }))
    await execute_started.wait()
    broker.cancel()
    assert await request == {"status": "cancelled"}
    assert await decision is True
    assert [event["type"] for event in events] == ["phone_action_confirmation"]


@pytest.mark.asyncio
async def test_broker_timeout_cancels_blocked_execute_delivery():
    events = []
    execute_started = asyncio.Event()
    async def emit(event):
        if event["type"] == "phone_action_execute":
            execute_started.set()
            await asyncio.Event().wait()
        events.append(event)
        return True
    broker = PhoneActionBroker(emit, timeout=0.02)
    request = asyncio.create_task(broker.request({"action": "open_url", "url": "https://example.com"}))
    await asyncio.sleep(0)
    request_id = events[0]["request_id"]
    decision = asyncio.create_task(broker.handle_control({
        "type": "phone_action_decision", "request_id": request_id, "decision": "confirmed",
    }))
    await execute_started.wait()
    assert await request == {"status": "timeout"}
    assert await decision is True
    assert [event["type"] for event in events] == [
        "phone_action_confirmation", "phone_action_closed",
    ]


@pytest.mark.asyncio
async def test_broker_task_cancellation_closes_correlated_card():
    events = []
    async def emit(event):
        events.append(event)
        return True
    broker = PhoneActionBroker(emit, timeout=1)
    request = asyncio.create_task(broker.request({"action": "share_text", "text": "x"}))
    await asyncio.sleep(0)
    request_id = events[0]["request_id"]
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert events[-1] == {
        "type": "phone_action_closed", "request_id": request_id, "status": "cancelled",
    }


@pytest.mark.asyncio
async def test_tool_is_live_and_spar_visible_and_open_app_is_allowlisted():
    declaration = next(item for item in FUNCTION_DECLARATIONS if item["name"] == "phone_action")
    assert set(declaration["parameters"]["properties"]["action"]["enum"]) == {"copy_text", "open_url", "share_text", "open_app"}
    assert "phone_action" in NON_BLOCKING_TOOLS
    callback_calls = []
    async def callback(action):
        callback_calls.append(action)
        return {"status": "executed"}
    executor = VoiceToolExecutor(delegate=None, request_phone_action=callback)
    assert await executor.execute("phone_action", {"action": "copy_text", "text": "abc"}) == {"status": "executed"}
    assert callback_calls == [{"action": "copy_text", "text": "abc"}]
    result = await executor.execute("phone_action", {"action": "open_app", "app": "settings"})
    assert result == {"status": "executed"}
    assert callback_calls[-1] == {"action": "open_app", "app": "settings"}
    rejected = await executor.execute("phone_action", {"action": "open_app", "app": "evil"})
    assert rejected["status"] == "failed"


@pytest.mark.asyncio
async def test_spar_session_end_cancels_confirmation_without_execution():
    from hermes_cli import voice_ws

    async def emit(_event): return True
    broker = PhoneActionBroker(emit, timeout=1)
    class Socket:
        async def receive(self):
            return {"text": '{"type":"end"}'}
    deferred = voice_ws._SparDeferredMessages()
    result = await voice_ws._request_spar_phone_action(
        Socket(), broker, {"action": "copy_text", "text": "x"},
        asyncio.Event(), deferred, asyncio.Event(),
    )
    assert result == {"status": "cancelled"}
    assert deferred.popleft() == {"text": '{"type":"end"}'}


def test_confirmation_card_accessibility_and_short_viewport_scroll_contract():
    html = CLIENT_HTML.read_text(encoding="utf-8")
    assert 'id="phone-action-card"' in html
    assert 'aria-live="assertive"' in html
    assert 'aria-atomic="true"' in html
    assert ".phone-action-buttons button { min-height: 48px; }" in html
    assert "overflow-y: auto;" in html
    assert 'body[data-phone-action-open="true"] .state-card { display: none; }' in html
