import logging

import pytest

from agent import chat_completion_helpers as cch


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now


class _FakeAgent:
    def __init__(self, *, api_mode="codex_responses"):
        self.api_mode = api_mode
        self.model = "gpt-5.5"
        self.base_url = "https://chatgpt.com/backend-api/codex"
        self.provider = "openai-codex"
        self._interrupt_requested = False
        self._last_activity_ts = 0.0
        self.statuses = []
        self.closed = []

    def _compute_non_stream_stale_timeout(self, _messages):
        return 300.0

    def _touch_activity(self, _desc):
        self._last_activity_ts = cch.time.time()

    def _emit_status(self, message):
        self.statuses.append(message)

    def _create_request_openai_client(self, *args, **kwargs):
        return object()

    def _close_request_openai_client(self, client, reason):
        self.closed.append(reason)

    def _abort_request_openai_client(self, client, reason):
        self.closed.append(reason)

    def _client_log_context(self):
        return "provider=openai-codex model=gpt-5.5"

    def _run_codex_stream(self, api_kwargs, client=None, on_first_delta=None):
        return None


class _FakeThread:
    def __init__(self, *, clock, agent, active_ticks, refresh_activity):
        self.clock = clock
        self.agent = agent
        self.active_ticks = active_ticks
        self.refresh_activity = refresh_activity
        self.joins = 0

    def start(self):
        pass

    def is_alive(self):
        return self.joins < self.active_ticks

    def join(self, timeout=None):
        self.joins += 1
        self.clock.now += 100.0
        if self.refresh_activity:
            self.agent._last_activity_ts = self.clock.now


def _install_fake_thread(monkeypatch, *, clock, agent, active_ticks, refresh_activity):
    def _factory(*args, **kwargs):
        return _FakeThread(
            clock=clock,
            agent=agent,
            active_ticks=active_ticks,
            refresh_activity=refresh_activity,
        )

    monkeypatch.setattr(cch.threading, "Thread", _factory)


def test_codex_responses_active_stream_activity_does_not_trigger_non_stream_stale_watchdog(
    monkeypatch, caplog
):
    clock = _FakeClock()
    agent = _FakeAgent(api_mode="codex_responses")
    monkeypatch.setattr(cch.time, "time", clock.time)
    _install_fake_thread(
        monkeypatch,
        clock=clock,
        agent=agent,
        active_ticks=4,
        refresh_activity=True,
    )

    with caplog.at_level(logging.WARNING, logger="agent.chat_completion_helpers"):
        cch.interruptible_api_call(agent, {"model": "gpt-5.5", "messages": []})

    assert "Non-streaming API call stale" not in caplog.text
    assert agent.statuses == []
    assert "stale_call_kill" not in agent.closed


def test_codex_responses_idle_over_threshold_still_triggers_non_stream_stale_watchdog(
    monkeypatch, caplog
):
    clock = _FakeClock()
    agent = _FakeAgent(api_mode="codex_responses")
    monkeypatch.setattr(cch.time, "time", clock.time)
    _install_fake_thread(
        monkeypatch,
        clock=clock,
        agent=agent,
        active_ticks=10,
        refresh_activity=False,
    )

    with caplog.at_level(logging.WARNING, logger="agent.chat_completion_helpers"):
        with pytest.raises(TimeoutError):
            cch.interruptible_api_call(agent, {"model": "gpt-5.5", "messages": []})

    assert "Non-streaming API call stale" in caplog.text
    assert any("non-streaming" in status for status in agent.statuses)
