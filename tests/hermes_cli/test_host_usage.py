from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from hermes_cli.host_usage import HostUsagePaths, build_host_usage


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _paths(tmp_path: Path) -> HostUsagePaths:
    return HostUsagePaths(
        hermes_home=tmp_path / "hermes",
        claude_root=tmp_path / "claude",
        codex_root=tmp_path / "codex",
        kimi_root=tmp_path / "kimi",
        qwen_usage_root=tmp_path / "qwen",
        grok_log=tmp_path / "grok" / "unified.jsonl",
    )


def _state_db(path: Path, *, at: float, provider: str = "openai-codex") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session_model_usage ("
        "session_id TEXT, model TEXT, billing_provider TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER, "
        "cache_read_tokens INTEGER, cache_write_tokens INTEGER, last_seen REAL)"
    )
    conn.execute(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?)",
        ("worker-1", "gpt-5.6-sol", provider, 10, 20, 30, 40, at),
    )
    conn.commit()
    conn.close()


def test_build_host_usage_combines_hermes_and_all_terminal_sources(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    now = time.time()
    iso = datetime.fromtimestamp(now, ZoneInfo("Europe/Berlin")).astimezone(ZoneInfo("UTC")).isoformat()
    millis = int(now * 1000)
    _state_db(paths.hermes_home / "state.db", at=now)
    _jsonl(
        paths.claude_root / "project" / "claude.jsonl",
        [{
            "type": "assistant",
            "timestamp": iso,
            "uuid": "claude-event",
            "sessionId": "claude-session",
            "message": {
                "model": "claude-opus-4-8",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 40,
                },
            },
        }],
    )
    _jsonl(
        paths.codex_root / "rollout.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "codex-session"}},
            {
                "type": "event_msg",
                "timestamp": iso,
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 40, "total_tokens": 140}},
                },
            },
        ],
    )
    _jsonl(
        paths.kimi_root / "kimi-session" / "agents" / "main" / "wire.jsonl",
        [{
            "type": "usage.record",
            "time": millis,
            "usage": {"inputOther": 10, "output": 20, "inputCacheRead": 30, "inputCacheCreation": 40},
        }],
    )
    _jsonl(
        paths.qwen_usage_root / "token-usage.jsonl",
        [{"timestamp": iso, "sessionId": "qwen-session", "inputTokens": 80, "cachedTokens": 20, "outputTokens": 40, "totalTokens": 120}],
    )
    _jsonl(
        paths.grok_log,
        [{
            "msg": "shell.turn.inference_done",
            "ts": iso,
            "sid": "grok-session",
            "ctx": {"prompt_tokens": 70, "cached_prompt_tokens": 60, "completion_tokens": 30, "reasoning_tokens": 20},
        }],
    )
    # File discovery deliberately uses mtime as a cheap prefilter.
    for path in tmp_path.rglob("*.jsonl"):
        os.utime(path, (now, now))

    payload = build_host_usage(days=7, now=now, paths=paths, active_tmux_panes=7)

    assert payload["total_tokens"] == 350
    assert payload["total_sessions"] == 6
    assert payload["active_tmux_panes"] == 7
    assert payload["errors"] == []
    by_provider = {row["provider"]: row for row in payload["providers"]}
    assert by_provider["codex"]["total_tokens"] == 150
    assert by_provider["claude"]["total_tokens"] == 30
    assert by_provider["kimi"]["total_tokens"] == 30
    assert by_provider["qwen"]["total_tokens"] == 100
    assert by_provider["grok"]["total_tokens"] == 40
    assert by_provider["codex"]["sessions"] == 2
    assert by_provider["codex"]["daily"][-1]["tokens"] == 150
    assert {row["source"]: row["sessions"] for row in payload["sources"]} == {
        "hermes": 1,
        "terminal": 5,
    }


def test_build_host_usage_is_fail_soft_and_excludes_old_events(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    now = time.time()
    old = now - 10 * 86400
    old_iso = datetime.fromtimestamp(old, ZoneInfo("UTC")).isoformat()
    _jsonl(
        paths.qwen_usage_root / "token-usage.jsonl",
        [{"timestamp": old_iso, "sessionId": "old", "totalTokens": 999}],
    )
    # Malformed rows and absent source roots do not turn the endpoint into 500.
    with (paths.qwen_usage_root / "token-usage.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
    os.utime(paths.qwen_usage_root / "token-usage.jsonl", (now, now))

    payload = build_host_usage(days=3, now=now, paths=paths, active_tmux_panes=0)

    assert payload["days"] == 3
    assert len(payload["dates"]) == 3
    assert payload["total_tokens"] == 0
    assert payload["providers"] == []
    assert payload["errors"] == []
