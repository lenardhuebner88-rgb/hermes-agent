#!/usr/bin/env python3
"""Kanban Dispatcher Heartbeat Watchdog (R3 / P3-dispatcher-watchdog, AC3).

Companion to ``~/.hermes/bin/gateway_exit_watchdog.py``, but versioned and
testable in-repo. The gateway's kanban dispatcher writes a heartbeat after every
tick (``gateway/kanban_watchers.py`` →
``hermes_cli.kanban_db.write_kanban_dispatcher_heartbeat``) to
``~/.hermes/state/kanban_dispatcher_heartbeat.json`` with fields:

    last_tick_at        unix seconds of the last completed dispatcher tick
    tick_health         "ok" when the tick finished cleanly, else a reason
    last_green_gate_at  unix seconds of the last green gate (or null)
    counts              aggregate self-heal / parked / escalation / stranded
    boards              per-board breakdown

This watchdog runs on a fast cadence (systemd timer, ~5 min). When the heartbeat
is STALE (``last_tick_at`` older than ``--stale-after-min`` minutes, default 15)
OR the dispatcher reported an unhealthy tick (``tick_health != "ok"``) OR the
heartbeat is missing/unreadable, it posts ONE Discord alert to #hermes-oc —
gated to once per calendar day (UTC) by a state file so a sustained outage does
not spam the ops channel. A fresh, healthy heartbeat → no alert.

ALERT-ONLY by design: it never restarts the gateway or the dispatcher. Recovery
is a human decision; this just makes a silent dispatcher loud.

Idempotency: once per UTC calendar day per ``last_alert_bucket``. State at
``~/.hermes/state/kanban_dispatcher_watchdog_state.json``.

Usage:
    python3 scripts/kanban_dispatcher_watchdog.py [--dry-run]
        [--stale-after-min 15] [--heartbeat <path>] [--state <path>]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

def _default_hermes_root(
    *, native: Path | None = None, env: str | None = None
) -> Path:
    """Resolve the Hermes root the SAME way the heartbeat *writer* does.

    The dispatcher writes the heartbeat to
    ``hermes_constants.get_default_hermes_root() / "state" / ...``, which
    collapses a profile-scoped ``HERMES_HOME`` (``<root>/profiles/<name>``)
    back to ``<root>``. If this watchdog read the raw ``HERMES_HOME`` instead,
    it would look under ``<root>/profiles/<name>/state`` and falsely report a
    missing heartbeat whenever it runs inside a profile env — a watchdog that
    cries wolf. We replicate the collapse here (standalone, no repo import) so
    the script still works under the systemd ``env python3`` launcher, where
    the venv editable install is not on ``sys.path``.
    """
    native = (Path("~/.hermes").expanduser()) if native is None else native
    env = os.environ.get("HERMES_HOME", "") if env is None else env
    if not env:
        return native
    env_path = Path(env)
    try:
        env_path.resolve().relative_to(native.resolve())
        # HERMES_HOME is under ~/.hermes (normal or profile mode) → root.
        return native
    except ValueError:
        pass
    # Docker / custom layout: <root>/profiles/<name> → grandparent is the root.
    if env_path.parent.name == "profiles":
        return env_path.parent.parent
    # Otherwise HERMES_HOME itself is the root.
    return env_path


HERMES_ROOT = _default_hermes_root()
HEARTBEAT_FILE = HERMES_ROOT / "state" / "kanban_dispatcher_heartbeat.json"
STATE_FILE = HERMES_ROOT / "state" / "kanban_dispatcher_watchdog_state.json"
ENV_FILE = HERMES_ROOT / ".env"

DISCORD_OPS_CHANNEL = "1495737862522405088"  # #hermes-oc (ops/stability lane)
STALE_AFTER_SECONDS = 15 * 60  # 15 minutes


def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _read_token() -> str | None:
    if not ENV_FILE.exists():
        return None
    try:
        for line in ENV_FILE.open():
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        return None
    return None


def _post_discord(body: str, channel: str = DISCORD_OPS_CHANNEL) -> dict:
    token = _read_token()
    if not token:
        return {"result": "error", "error": "no_token"}
    if not re.match(r"^[0-9]{17,20}$", channel):
        return {"result": "error", "error": "invalid_channel_id"}
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel}/messages",
        data=json.dumps({"content": body[:2000]}).encode("utf-8"),
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (kanban-dispatcher-watchdog, 1.0)",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            b = json.loads(r.read())
            return {"result": "sent", "message_id": b.get("id")}
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            err_body = ""
        return {"result": "error", "error": f"http_{e.code}", "body": err_body}
    except Exception as e:
        return {"result": "error", "error": str(e)[:200]}


def _load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_heartbeat(path: Path = HEARTBEAT_FILE) -> dict | None:
    """Return the heartbeat payload dict, or None if missing/unreadable."""
    try:
        if not Path(path).is_file():
            return None
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else None
    except Exception:
        return None


def evaluate(
    heartbeat: dict | None,
    *,
    now: float,
    stale_after_seconds: int = STALE_AFTER_SECONDS,
) -> tuple[bool, str, dict]:
    """Classify a heartbeat.

    Returns ``(healthy, reason, detail)``. ``healthy`` is True only when the
    heartbeat is present, fresh, and ``tick_health == "ok"``. Otherwise
    ``reason`` is one of ``heartbeat_missing`` / ``heartbeat_invalid`` /
    ``stale`` / ``tick_health`` and ``detail`` carries age/health for the alert.
    """
    if heartbeat is None:
        return False, "heartbeat_missing", {"heartbeat_age_s": None}

    raw_tick = heartbeat.get("last_tick_at")
    try:
        last_tick = int(raw_tick or 0)
    except (TypeError, ValueError):
        last_tick = 0
    if last_tick <= 0:
        return False, "heartbeat_invalid", {"heartbeat_age_s": None}

    age = max(0.0, now - last_tick)
    tick_health = str(heartbeat.get("tick_health") or "unknown")
    detail = {
        "heartbeat_age_s": round(age, 1),
        "tick_health": tick_health,
        "last_tick_at": last_tick,
    }
    if age > stale_after_seconds:
        detail["stale_after_s"] = stale_after_seconds
        return False, "stale", detail
    if tick_health != "ok":
        return False, "tick_health", detail
    return True, "ok", detail


def _format_alert(reason: str, detail: dict, *, stale_after_seconds: int) -> str:
    age = detail.get("heartbeat_age_s")
    age_str = f"{age:.0f}s" if isinstance(age, (int, float)) else "unknown"
    health = detail.get("tick_health", "unknown")
    if reason == "heartbeat_missing":
        line = "heartbeat file **missing** — dispatcher has not written a tick"
    elif reason == "heartbeat_invalid":
        line = "heartbeat **unreadable / has no last_tick_at**"
    elif reason == "stale":
        line = (
            f"heartbeat **stale**: last tick `{age_str}` ago "
            f"(threshold {stale_after_seconds // 60} min)"
        )
    else:  # tick_health
        line = f"dispatcher tick unhealthy: `tick_health={health}` (last tick {age_str} ago)"
    return (
        f"[STOP-CODE: kanban-dispatcher-watchdog] kanban-dispatcher alert\n"
        f"{line}\n"
        f"file: `~/.hermes/state/kanban_dispatcher_heartbeat.json`\n"
        f"alert-only — no auto-restart. Check the gateway "
        f"(`systemctl --user status hermes-gateway`) and "
        f"`hermes kanban list --status ready`.\n"
        f"_via: scripts/kanban_dispatcher_watchdog.py — R3 / P3-dispatcher-watchdog_"
    )


def run(
    *,
    dry_run: bool = False,
    heartbeat_path: Path = HEARTBEAT_FILE,
    state_path: Path = STATE_FILE,
    stale_after_seconds: int = STALE_AFTER_SECONDS,
    channel: str = DISCORD_OPS_CHANNEL,
    now: float | None = None,
) -> dict:
    """One watchdog tick. Returns a result dict for logging."""
    now = time.time() if now is None else float(now)
    heartbeat = read_heartbeat(heartbeat_path)
    healthy, reason, detail = evaluate(
        heartbeat, now=now, stale_after_seconds=stale_after_seconds
    )
    if healthy:
        return {"action": "noop", "reason": "healthy", **detail}

    # Idempotency: at most one alert per UTC calendar day.
    bucket = dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()
    state = _load_state(state_path)
    if state.get("last_alert_bucket") == bucket:
        return {
            "action": "noop",
            "reason": "already_alerted_today",
            "alert_reason": reason,
            "last_alert_bucket": bucket,
            **detail,
        }

    body = _format_alert(reason, detail, stale_after_seconds=stale_after_seconds)
    if dry_run:
        return {
            "action": "alert_would_have_fired",
            "alert_reason": reason,
            "preview": body,
            **detail,
        }

    send_result = _post_discord(body, channel)
    if send_result.get("result") == "sent":
        state["last_alert_bucket"] = bucket
        state["last_alert_ts"] = utcnow_iso()
        state["last_alert_reason"] = reason
        state["last_alert_message_id"] = send_result.get("message_id")
        _save_state(state_path, state)
        action = "alert_emitted"
    else:
        action = "alert_send_failed"
    return {
        "action": action,
        "alert_reason": reason,
        "send_result": send_result,
        **detail,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run", action="store_true", help="Compute + print, no Discord post"
    )
    ap.add_argument(
        "--heartbeat", type=Path, default=HEARTBEAT_FILE, help="Path to the heartbeat JSON"
    )
    ap.add_argument("--state", type=Path, default=STATE_FILE, help="Path to watchdog state JSON")
    ap.add_argument(
        "--stale-after-min",
        type=int,
        default=STALE_AFTER_SECONDS // 60,
        help="Alert if last_tick_at is older than this many minutes (default 15)",
    )
    ap.add_argument(
        "--channel", default=DISCORD_OPS_CHANNEL, help="Discord channel id for the alert"
    )
    args = ap.parse_args(argv)
    result = run(
        dry_run=args.dry_run,
        heartbeat_path=args.heartbeat,
        state_path=args.state,
        stale_after_seconds=args.stale_after_min * 60,
        channel=args.channel,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
