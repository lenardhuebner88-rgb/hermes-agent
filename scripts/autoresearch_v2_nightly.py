#!/usr/bin/env python3
"""Nightly sweep for the Autoresearch-v2 lanes (Deep-Audit + Test-Foundry).

Companion to ``autoresearch_nightly.py`` (which rotates the skill/code lanes). This
entrypoint drives the two v2 lanes every night, **dry-run** (proposals only — no skill
or code file is mutated; Test-Foundry runs with ``apply=False`` so it never writes a
branch). Both lanes persist their proposals + a run record straight into the dashboard
Autoresearch tab, and a one-line German summary is posted to Discord for observability.

Per night (rotation keyed on day-of-year, ``--date`` override-able):
  * Deep-Audit  → one subsystem (read-only tool loop over its file allowlist).
  * Test-Foundry → two curated targets (worktree-isolated mutation gate).

Model selection is intentionally delegated to ``call_llm(task="code_audit" /
"test_hardening")`` — i.e. whatever the operator picks per lane in the dashboard model
picker (``/api/model/set``) is what runs here. ``call_llm`` additionally auto-falls-back
to the next available provider on an HTTP 402 / credit error, so the sweep keeps working
if one provider's plan runs dry.

No hard token ceiling (operator choice); the cumulative token spend is surfaced in the
Discord line instead.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO), str(_REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from hermes_cli import autoresearch_proposals as _proposals  # noqa: E402
from hermes_cli import autoresearch_reconcile as reconciler  # noqa: E402
from hermes_cli import deep_audit, test_foundry  # noqa: E402

# Operator-assigned report channel (override with --channel-id / env at the unit).
DEFAULT_CHANNEL_ID = "1495737862522405088"
DEFAULT_TF_MUTANTS = 15
DEFAULT_TF_TARGETS = 2
DEFAULT_DA_MAX_FILES = 12

# Short tags for the common non-yield reasons, so the Discord line stays compact.
_SKIP_TAGS = (
    ("not clean", "skip:dirty"),
    ("no affected tests", "skip:no-tests"),
    ("not found", "skip:missing"),
    ("no files resolved", "skip:no-files"),
    ("baseline tests failed", "skip:red-baseline"),
)


def _env_float(name: str, default: float = 0.0) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _budget_exhausted(started: float, budget_seconds: float) -> bool:
    return budget_seconds > 0 and (time.monotonic() - started) >= budget_seconds


def _circuit_open(failures: int, threshold: int) -> bool:
    return threshold > 0 and failures >= threshold


def _lane_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def day_of_year(when: date_cls | None = None) -> int:
    """Day-of-year (1..366). UTC today when not given."""
    if when is None:
        when = datetime.now(timezone.utc).date()
    return when.timetuple().tm_yday


def select_subsystem(subsystems: Sequence[str], day: int) -> str:
    return subsystems[day % len(subsystems)]


def select_targets(targets: Sequence[str], day: int, count: int) -> list[str]:
    """Rotating, de-duplicated slice of ``count`` targets for this day."""
    n = len(targets)
    count = max(1, min(count, n))
    start = (day * count) % n
    out: list[str] = []
    for i in range(n):
        cand = targets[(start + i) % n]
        if cand not in out:
            out.append(cand)
        if len(out) == count:
            break
    return out


def _short_reason(reason: str | None) -> str:
    text = (reason or "").lower()
    for needle, tag in _SKIP_TAGS:
        if needle in text:
            return tag
    return "skip" if reason else ""


def _fmt_tok(tokens: int) -> str:
    if tokens >= 1000:
        return f"{tokens / 1000:.0f}k"
    return str(int(tokens))


def run_deep_audit_lane(subsystem: str, *, max_files: int) -> dict[str, Any]:
    """Run one Deep-Audit subsystem; return a compact summary dict."""
    payload = deep_audit.write_request(subsystem=subsystem, focus=None, max_files=max_files)
    result = deep_audit.run_request_file(Path(payload["request_path"]))
    findings = result.get("findings") or []
    return {
        "subsystem": subsystem,
        "ok": bool(result.get("ok")),
        "findings": len(findings),
        "tokens": int(result.get("tokens") or 0),
        "model": result.get("model"),
        "reason": result.get("reason") or "",
    }


def run_test_foundry_lane(targets: Sequence[str], *, max_mutants: int) -> list[dict[str, Any]]:
    """Run Test-Foundry over each target (dry-run); return per-target summaries."""
    summaries: list[dict[str, Any]] = []
    for target in targets:
        payload = test_foundry.write_request(target=target, max_mutants=max_mutants, apply=False)
        result = test_foundry.run_request_file(Path(payload["request_path"]))
        summaries.append({
            "target": target,
            "ok": bool(result.get("ok")),
            "tests_kept": int(result.get("tests_kept") or 0),
            "survivors": len(result.get("survivors") or []),
            "tokens": int(result.get("tokens") or 0),
            "model": result.get("model"),
            "reason": result.get("reason") or "",
        })
    return summaries


def _da_line(da: dict[str, Any] | None) -> str:
    if da is None:
        return "🔍 Deep-Audit · (übersprungen)"
    if da.get("error"):
        return f"🔍 Deep-Audit · {da.get('subsystem', '?')} · FEHLER: {da['error']}"
    model = da.get("model") or "?"
    tail = f"{da['findings']} Funde · {_fmt_tok(da['tokens'])} tok · {model}"
    reason = _short_reason(da.get("reason")) if not da.get("findings") else ""
    if reason:
        tail += f" ({reason})"
    return f"🔍 Deep-Audit · {da['subsystem']} · {tail}"


def _tf_line(tf: list[dict[str, Any]] | None, error: str | None = None) -> str:
    if error:
        return f"🧪 Test-Foundry · FEHLER: {error}"
    if not tf:
        return "🧪 Test-Foundry · (übersprungen)"
    parts = []
    for item in tf:
        name = Path(item["target"]).name
        if item.get("tests_kept"):
            parts.append(f"{name}(+{item['tests_kept']})")
        else:
            reason = _short_reason(item.get("reason"))
            parts.append(f"{name}(0{', ' + reason if reason else ''})")
    total = sum(int(i.get("tokens") or 0) for i in tf)
    return f"🧪 Test-Foundry · {', '.join(parts)} · {_fmt_tok(total)} tok"


def _run_reconciler() -> dict:
    summary = reconciler.reconcile_proposals()
    print(json.dumps({"lane": "reconcile", **summary}, indent=2, ensure_ascii=False))
    return summary


def build_summary(
    when: date_cls,
    da: dict[str, Any] | None,
    tf: list[dict[str, Any]] | None,
    *,
    tf_error: str | None = None,
) -> str:
    """Pure formatter for the Discord one-liner (multi-line message)."""
    da_tok = int(da.get("tokens") or 0) if da and not da.get("error") else 0
    tf_tok = sum(int(i.get("tokens") or 0) for i in (tf or []))
    total = da_tok + tf_tok
    return (
        f"🌙 Autoresearch-v2 Nightly · {when.isoformat()}\n"
        f"{_da_line(da)}\n"
        f"{_tf_line(tf, error=tf_error)}\n"
        f"Σ {_fmt_tok(total)} tok → Review: Dashboard-Tab :9119/control"
    )


def post_summary(message: str, *, channel_id: str, sender: Callable[..., Any] | None = None) -> None:
    """Send the summary via the shared send_message_tool contract.

    Run as a bare systemd process (not inside the gateway), so there is no live
    Discord adapter — the standalone sender needs ``DISCORD_BOT_TOKEN``. Load it
    from ``~/.hermes/.env`` via the canonical loader. Skipped when a sender is
    injected (unit tests), which needs no token.
    """
    from daily_research_post import post_to_discord

    if sender is None:
        try:
            from hermes_cli.env_loader import load_hermes_dotenv

            load_hermes_dotenv()
        except Exception:  # token may already be in env; let the send surface any real failure
            pass

    post_to_discord(message, channel_id=channel_id, sender=sender)


def _parse_date(raw: str | None) -> date_cls:
    if not raw:
        return datetime.now(timezone.utc).date()
    return datetime.strptime(raw, "%Y-%m-%d").date()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nightly sweep for the Autoresearch-v2 lanes.")
    parser.add_argument("--send", dest="send", action="store_true", default=True, help="Post the summary to Discord (default).")
    parser.add_argument("--no-send", dest="send", action="store_false", help="Print the summary instead of posting.")
    parser.add_argument("--once", action="store_true", help="No-op marker for an explicit single run (clarity in cron/manual use).")
    parser.add_argument("--channel-id", default=DEFAULT_CHANNEL_ID)
    parser.add_argument("--date", help="Override rotation date (YYYY-MM-DD), for testing.")
    parser.add_argument("--lanes", default="deep-audit,test-foundry", help="Comma list of lanes to run.")
    parser.add_argument("--tf-targets", type=int, default=DEFAULT_TF_TARGETS)
    parser.add_argument("--tf-mutants", type=int, default=DEFAULT_TF_MUTANTS)
    parser.add_argument("--da-max-files", type=int, default=DEFAULT_DA_MAX_FILES)
    parser.add_argument("--wall-clock-budget-seconds", type=float, default=None)
    parser.add_argument("--circuit-breaker-threshold", type=int, default=2)
    args = parser.parse_args(argv)

    when = _parse_date(args.date)
    day = day_of_year(when)
    lanes = {lane.strip() for lane in args.lanes.split(",") if lane.strip()}
    started = time.monotonic()
    budget_seconds = args.wall_clock_budget_seconds
    if budget_seconds is None:
        budget_seconds = _env_float("AR_V2_WALL_CLOCK_BUDGET_SECONDS", 0.0)
    circuit_failures = 0

    da_summary: dict[str, Any] | None = None
    tf_summary: list[dict[str, Any]] | None = None
    tf_error: str | None = None

    if "deep-audit" in lanes:
        subsystem = select_subsystem(list(deep_audit.SUBSYSTEM_GLOBS.keys()), day)
        if _budget_exhausted(started, budget_seconds):
            da_summary = {"subsystem": subsystem, "error": "Wall-clock budget exhausted before Deep-Audit"}
        else:
            try:
                da_summary = run_deep_audit_lane(subsystem, max_files=args.da_max_files)
            except Exception as exc:  # one lane must never kill the other / the report
                traceback.print_exc()
                circuit_failures += 1
                da_summary = {"subsystem": subsystem, "error": _lane_error(exc)}

    if "test-foundry" in lanes:
        if _circuit_open(circuit_failures, args.circuit_breaker_threshold):
            tf_error = "Circuit breaker open before Test-Foundry"
        elif _budget_exhausted(started, budget_seconds):
            tf_error = "Wall-clock budget exhausted before Test-Foundry"
        else:
            targets = select_targets(test_foundry.curated_targets(), day, args.tf_targets)
            try:
                tf_summary = run_test_foundry_lane(targets, max_mutants=args.tf_mutants)
            except Exception as exc:
                traceback.print_exc()
                circuit_failures += 1
                tf_error = _lane_error(exc)

    # Quellen-Hygiene: alte reverted/crashed proposed auto-skippen + done/skipped
    # archivieren. Laeuft nightly mit, damit gate.phase-Zombies und alte proposed
    # nicht unbegrenzt akkumulieren (kein eigener Service noetig).
    try:
        prune_summary = _proposals.prune_proposals()
        print(
            f"[autoresearch-v2-nightly] prune: {prune_summary.get('auto_skipped', 0)} auto-skipped, "
            f"{prune_summary.get('archived', 0)} archived"
        )
    except Exception as exc:  # Hygiene darf den Report nie killen
        traceback.print_exc()
        print(f"[autoresearch-v2-nightly] prune fehlgeschlagen: {exc}", file=sys.stderr)

    try:
        _run_reconciler()
    except Exception as exc:  # Reconcile darf den Report nie killen
        traceback.print_exc()
        print(f"[autoresearch-v2-nightly] reconcile fehlgeschlagen: {exc}", file=sys.stderr)

    message = build_summary(when, da_summary, tf_summary, tf_error=tf_error)
    print(message)

    if args.send:
        try:
            post_summary(message, channel_id=args.channel_id)
        except Exception as exc:
            traceback.print_exc()
            print(f"[autoresearch-v2-nightly] Discord-Post fehlgeschlagen: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
