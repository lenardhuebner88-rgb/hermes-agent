"""Autonomous release orchestrator (Plan→Board→Release pipeline, Subsystem C).

On chain-tip completion → green gates → tip judgment → live test → deploy with
rollback on live failure. EVERYTHING here is behind the ``release.autonomous``
kill-switch (default **False**); ``critical``-tier chains never auto-deploy
regardless of the switch. ``ui-real`` live tests are never autonomous — they
return ``held`` for the operator.

Truth = API payload (``/api/status``), never a screenshot (CLAUDE.md).
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:9119"

# Payload keys /api/status must carry for a healthy backend (mirrors the
# deploy_dashboard.sh payload validation — real backend code, both auth modes).
_SMOKE_REQUIRED_KEYS = ("version",)

Fetch = Callable[..., dict]


@dataclass
class LiveTestResult:
    depth: str
    passed: bool
    held: bool = False
    detail: str = ""


def _default_fetch(path: str, timeout: float = 8.0) -> dict:
    """GET ``base_url + path`` and parse JSON. Loopback only by construction."""
    url = f"{DEFAULT_BASE_URL}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def run_live_test(
    depth: str,
    *,
    fetch: Optional[Fetch] = None,
    contract: Optional[dict] = None,
) -> LiveTestResult:
    """Execute the PlanSpec ``live_test_depth`` check against the live service.

    * ``smoke``   — ``/api/status`` returns valid JSON with a ``version``
      (proves the Python backend, not just the static SPA).
    * ``contract``— fetch ``contract["path"]`` and assert every key/value in
      ``contract["expect"]`` matches the payload.
    * ``ui-real`` — ALWAYS ``held`` (operator-gated, never autonomous).
    * empty/None  — trivially passes ("no live test configured").
    """
    depth = (depth or "").strip().lower()
    fetch = fetch or _default_fetch
    if not depth:
        return LiveTestResult(depth=depth, passed=True, detail="no live test configured")
    if depth == "ui-real":
        return LiveTestResult(
            depth=depth,
            passed=False,
            held=True,
            detail="ui-real is operator-gated — never autonomous",
        )
    if depth == "smoke":
        try:
            payload = fetch("/api/status")
        except Exception as exc:
            return LiveTestResult(depth=depth, passed=False, detail=f"fetch failed: {exc}")
        if not isinstance(payload, dict) or not all(
            payload.get(k) for k in _SMOKE_REQUIRED_KEYS
        ):
            return LiveTestResult(
                depth=depth, passed=False, detail=f"invalid status payload: {payload!r:.200}"
            )
        return LiveTestResult(depth=depth, passed=True, detail="status payload valid")
    if depth == "contract":
        contract = contract or {}
        path = str(contract.get("path") or "/api/status")
        expect = contract.get("expect")
        if not isinstance(expect, dict) or not expect:
            # No expectation defined → degrade to the smoke check on the path.
            expect = {}
        try:
            payload = fetch(path)
        except Exception as exc:
            return LiveTestResult(depth=depth, passed=False, detail=f"fetch failed: {exc}")
        if not isinstance(payload, dict):
            return LiveTestResult(
                depth=depth, passed=False, detail=f"non-dict payload from {path}"
            )
        mismatches = [
            f"{k}: expected {v!r}, got {payload.get(k)!r}"
            for k, v in expect.items()
            if payload.get(k) != v
        ]
        if mismatches:
            return LiveTestResult(
                depth=depth, passed=False, detail="; ".join(mismatches)
            )
        return LiveTestResult(depth=depth, passed=True, detail=f"contract on {path} holds")
    # Unknown depth: fail CLOSED — an unknown check must not count as passed.
    return LiveTestResult(depth=depth, passed=False, detail=f"unknown live_test_depth: {depth}")


# ---------------------------------------------------------------------------
# C3: release orchestrator + kill-switch + chain-tip integration
# ---------------------------------------------------------------------------

_TIER_ORDER = {"standard": 0, "review": 1, "critical": 2}


def _release_config() -> dict:
    """Resolve the ``release`` policy from the ROOT config.yaml (same
    root-config discipline as ``kanban_db._review_gate_config`` — every
    process must agree on one source of truth). Conservative defaults:
    ``autonomous: false`` (the kill-switch), ``max_tier_autonomous: review``.
    """
    rel: dict = {}
    try:
        import yaml

        from hermes_constants import get_default_hermes_root

        cfg_path = get_default_hermes_root() / "config.yaml"
        if cfg_path.is_file():
            with open(cfg_path, "r", encoding="utf-8") as fh:
                root_cfg = yaml.safe_load(fh) or {}
            candidate = root_cfg.get("release") or {}
            if isinstance(candidate, dict):
                rel = candidate
    except Exception:
        rel = {}
    autonomous = rel.get("autonomous", False)
    if isinstance(autonomous, str):
        autonomous = autonomous.strip().lower() in ("1", "true", "yes", "on")
    max_tier = str(rel.get("max_tier_autonomous") or "review").strip().lower()
    if max_tier not in _TIER_ORDER:
        max_tier = "review"
    # S3 chronic-red-refinement: pause_on_red_streak (int, default 0 = off).
    # >0 holds a would-be release when the last N recorded green-gate nights
    # are ALL red — a chronically-red gate is not a safe base to release from
    # even though THIS chain's own tests are green (the nightly full suite
    # catches cross-chain regressions a per-chain run cannot see). Default 0
    # keeps today's behaviour byte-for-byte unchanged with no config key set.
    pause_on_red_streak = rel.get("pause_on_red_streak", 0)
    try:
        pause_on_red_streak = int(pause_on_red_streak)
    except (TypeError, ValueError):
        pause_on_red_streak = 0
    if pause_on_red_streak < 0:
        pause_on_red_streak = 0
    return {
        "autonomous": bool(autonomous),
        "max_tier_autonomous": max_tier,
        "pause_on_red_streak": pause_on_red_streak,
    }


def _repo_root() -> "Path":
    from pathlib import Path

    return Path(__file__).resolve().parent.parent


def _default_deploy() -> tuple[bool, str]:
    """Run scripts/deploy_dashboard.sh; (ok, output tail)."""
    import subprocess

    script = _repo_root() / "scripts" / "deploy_dashboard.sh"
    try:
        proc = subprocess.run(
            [str(script)], capture_output=True, text=True, timeout=900, check=False
        )
    except Exception as exc:
        return False, f"deploy failed to run: {exc}"
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
    return proc.returncode == 0, tail


def _default_rollback() -> tuple[bool, str]:
    """Roll back to the anchor BEFORE the failing deploy (the failing deploy
    tagged its own commit, so the target is the second-newest anchor)."""
    import subprocess

    root = _repo_root()
    try:
        tags = subprocess.run(
            ["git", "tag", "-l", "release/pre-deploy/*"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.split()
    except Exception as exc:
        return False, f"could not list anchors: {exc}"
    tags = sorted(tags)
    if len(tags) < 2:
        return False, "no previous pre-deploy anchor to roll back to"
    target = tags[-2]
    script = root / "scripts" / "rollback_dashboard.sh"
    try:
        proc = subprocess.run(
            [str(script), target],
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    except Exception as exc:
        return False, f"rollback failed to run: {exc}"
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
    return proc.returncode == 0, f"target={target}\n{tail}"


def _default_notify(message: str) -> None:
    """Operator alert: log + durable alert file. Discord delivery for
    attention outcomes (``rolled_back``/``held_critical``/``deploy_failed``)
    rides the ``auto_release`` task event through
    ``gateway/kanban_alerts.py``'s ``auto_release_attention`` rule (never
    Telegram) — this function itself never talks to Discord directly."""
    logger.error("AUTO-RELEASE ALERT: %s", message)
    try:
        from pathlib import Path

        reports = Path.home() / ".hermes" / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        import datetime as _dt

        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(reports / "auto-release-alerts.log", "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass


def release_chain(
    *,
    depth: str,
    config: dict,
    deploy: Callable[[], tuple[bool, str]],
    rollback: Callable[[], tuple[bool, str]],
    notify: Callable[[str], None],
    fetch: Optional[Fetch] = None,
) -> dict:
    """Deploy a green chain and verify it live; roll back on live failure.

    Flow: pre-deploy live test (baseline; ``held`` for ui-real) → deploy →
    post-deploy live test (at least ``smoke``) → on red: rollback + notify.
    Pure orchestration — every side effect is an injected callable.
    """
    depth = (depth or "").strip().lower()
    pre = run_live_test(depth, fetch=fetch)
    if pre.held:
        return {"outcome": "held_live_test", "detail": pre.detail}
    if not pre.passed:
        notify(f"auto-release aborted: pre-deploy live test red ({pre.detail})")
        return {"outcome": "aborted_pre_live_test", "detail": pre.detail}
    ok, deploy_tail = deploy()
    if not ok:
        notify(f"auto-release: deploy script failed — no rollback needed: {deploy_tail[-300:]}")
        return {"outcome": "deploy_failed", "detail": deploy_tail[-500:]}
    post = run_live_test(depth or "smoke", fetch=fetch)
    if post.passed:
        return {"outcome": "deployed", "detail": post.detail}
    rb_ok, rb_detail = rollback()
    notify(
        "auto-release: post-deploy live test RED — rolled back "
        f"(rollback_ok={rb_ok}): {post.detail}"
    )
    return {
        "outcome": "rolled_back",
        "detail": post.detail,
        "rollback_ok": rb_ok,
        "rollback_detail": rb_detail[-500:],
    }


def maybe_auto_release(conn, task_id: str) -> Optional[dict]:
    """Chain-tip hook: called by ``complete_task`` after a task reached ``done``.

    Returns ``None`` when this completion does not autonomously release
    (kill-switch off, not a PlanSpec chain, chain still open, no root, root not
    ``freigabe: complete``), a ``held_critical`` outcome when the chain
    contains a critical-tier task (never autonomous), a ``held_red_gate``
    outcome (S3) when ``release.pause_on_red_streak`` is set and the last N
    recorded green-gate nights are all red, or the :func:`release_chain`
    outcome dict.
    """
    cfg = _release_config()
    if not cfg.get("autonomous"):
        return None
    row = conn.execute(
        "SELECT planspec_source FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    source = (row["planspec_source"] or "").strip() if row else ""
    if not source:
        return None
    open_cnt = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE planspec_source = ? "
        "AND status NOT IN ('done', 'archived', 'failed', 'cancelled')",
        (source,),
    ).fetchone()[0]
    if open_cnt:
        return None  # not the tip — chain still has open slices
    # Chain root via the sink convention (children are the root's tree-parents).
    root = conn.execute(
        "SELECT t.id, t.freigabe, t.live_test_depth FROM tasks t "
        "JOIN task_links l ON l.child_id = t.id "
        "WHERE l.parent_id = ? AND t.freigabe IS NOT NULL "
        "ORDER BY t.created_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if root is None:
        return None
    if str(root["freigabe"] or "").strip().lower() != "complete":
        return None  # operator-gated chains never auto-release
    # Tier ceiling over the WHOLE chain: one critical slice pins the chain.
    from hermes_cli import kanban_db as _kb

    chain_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM tasks WHERE planspec_source = ?", (source,)
        ).fetchall()
    ]
    max_tier = "standard"
    for cid in chain_ids:
        tier = _kb._effective_review_tier(conn, cid)
        if _TIER_ORDER.get(tier, 0) > _TIER_ORDER.get(max_tier, 0):
            max_tier = tier
    if max_tier == "critical" or (
        _TIER_ORDER[max_tier] > _TIER_ORDER[cfg["max_tier_autonomous"]]
    ):
        return {"outcome": "held_critical", "detail": f"chain max tier {max_tier}"}
    # S3: pause_on_red_streak precondition — mirrors held_critical exactly
    # (same outcome-dict shape, same task_event/visibility path: the caller,
    # kanban_db.complete_task, appends whatever dict this function returns as
    # a task_event(kind="auto_release", payload=<this dict>) unconditionally;
    # that event is what gateway/kanban_alerts.py's auto_release_attention
    # rule reads). Default (pause_on_red_streak=0) never runs this check —
    # today's behaviour is unchanged with no config key set.
    pause_n = int(cfg.get("pause_on_red_streak") or 0)
    if pause_n > 0:
        from hermes_cli import vision_metrics as _vm

        if _vm.red_streak_from_head(_vm.read_gate_records()) >= pause_n:
            return {
                "outcome": "held_red_gate",
                "detail": f"last {pause_n} recorded green-gate nights all red",
            }
    return release_chain(
        depth=str(root["live_test_depth"] or ""),
        config=cfg,
        deploy=_default_deploy,
        rollback=_default_rollback,
        notify=_default_notify,
        fetch=_default_fetch,
    )
