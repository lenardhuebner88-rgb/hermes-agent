"""Tests for the stdio dispatch guard in tui_gateway/entry.py main().

The stdio ``hermes --tui`` path called ``dispatch(req)`` with no exception
guard, so a raising inline/fast handler crashed the whole gateway
subprocess (the WebSocket path already guards this and returns a JSON-RPC
``-32603`` internal error). These assert the stdio loop now contains a
raising dispatch: it replies ``-32603`` for the bad request and keeps
serving subsequent requests instead of exiting.
"""

import json

import tui_gateway.entry as entry


def test_dispatch_crash_returns_internal_error_and_continues(monkeypatch):
    # Two requests: the first makes dispatch raise, the second succeeds.
    req_crash = {"jsonrpc": "2.0", "id": 1, "method": "config.get"}
    req_ok = {"jsonrpc": "2.0", "id": 2, "method": "config.get"}
    lines = [json.dumps(req_crash) + "\n", json.dumps(req_ok) + "\n"]

    monkeypatch.setattr(entry.sys, "stdin", lines)

    # Strip startup side effects: no sidecar publisher, no MCP discovery
    # thread, no crash-log/stderr writes (keeps the test off real ~/.hermes).
    monkeypatch.setattr(entry, "_install_sidecar_publisher", lambda: None)
    monkeypatch.setattr(entry, "resolve_skin", lambda: "default")
    monkeypatch.setattr(entry, "_log_exit", lambda reason: None)
    import hermes_cli.config as _cfg

    monkeypatch.setattr(_cfg, "read_raw_config", lambda: {})

    calls = {"dispatch": 0}

    def fake_dispatch(req):
        calls["dispatch"] += 1
        if calls["dispatch"] == 1:
            raise RuntimeError("boom")
        return {"jsonrpc": "2.0", "id": req.get("id"), "result": {"ok": True}}

    monkeypatch.setattr(entry, "dispatch", fake_dispatch)

    written = []
    monkeypatch.setattr(entry, "write_json", lambda obj: (written.append(obj), True)[1])

    entry.main()

    # written[0] is the startup gateway.ready event.
    assert written[0]["params"]["type"] == "gateway.ready"
    # The crashing request gets a JSON-RPC internal error, not a process exit.
    err_resp = written[1]
    assert err_resp["id"] == 1
    assert err_resp["error"]["code"] == -32603
    # The gateway kept serving: the second request got a normal result.
    ok_resp = written[2]
    assert ok_resp["id"] == 2
    assert ok_resp["result"] == {"ok": True}
    assert calls["dispatch"] == 2
