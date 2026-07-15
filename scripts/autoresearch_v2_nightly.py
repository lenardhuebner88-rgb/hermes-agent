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

from hermes_cli import autoresearch_budget as arb  # noqa: E402
from hermes_cli import autoresearch_proposals as _proposals  # noqa: E402
from hermes_cli import autoresearch_reconcile as reconciler  # noqa: E402
from hermes_cli import outcome_verification as outcome_verifier  # noqa: E402
from hermes_cli import deep_audit, test_foundry  # noqa: E402
from hermes_cli.autoresearch_lane_contracts import (  # noqa: E402
    LaneOutcome,
    classify_lane_outcome,
    load_lane_specs,
    nightly_exit_code,
)

# Operator-assigned report channel (override with --channel-id / env at the unit).
DEFAULT_CHANNEL_ID = "1495737862522405088"


def _lane_specs():
    try:
        return load_lane_specs()
    except Exception:
        return load_lane_specs(config={})


def _lane_model(lane: str) -> str:
    """Effective model behind the lane's aux task (best-effort)."""
    try:
        from agent.auxiliary_client import _resolve_task_provider_model

        aux_task = _lane_specs()[lane].aux_task
        _provider, model, _base, _key, _mode = _resolve_task_provider_model(aux_task)
        return str(model or "")
    except Exception:
        return ""


def _quota_gate(lane: str) -> str | None:
    """Fresh subscription-quota decision for this lane. Returns the skip
    reason (quota_skipped) or None. Records the percent snapshot in the
    shared ledger for observability."""
    budget_cfg = arb.load_budget_config()
    decision = arb.evaluate_quota(arb.fetch_quota_snapshot(), budget_cfg)
    try:
        arb.DailyLedger(config=budget_cfg).record_quota_snapshot(
            f"pre-{lane}",
            session_percent=decision.session_percent,
            weekly_percent=decision.weekly_percent,
        )
    except Exception:
        pass
    return arb.quota_block_reason(decision, _lane_model(lane))

# Short tags for the common non-yield reasons, so the Discord line stays compact.
_SKIP_TAGS = (
    ("not clean", "skip:dirty"),
    ("no affected tests", "skip:no-tests"),
    ("not found", "skip:missing"),
    ("no files resolved", "skip:no-files"),
    ("baseline tests failed", "skip:red-baseline"),
    ("quota skip", "skip:quota"),
    ("cooldown active", "skip:cooldown"),
    ("budget exhausted", "skip:budget"),
)


def _expected_skip_error(error: str | None) -> str | None:
    """Expected guard decisions (quota/cooldown) routed through ``tf_error``
    must not render as FEHLER in the Discord report."""
    text = (error or "").lower()
    if text.startswith("quota skip"):
        return "quota_skipped"
    if "cooldown active" in text:
        return "skipped_expected"
    return None


def _env_float(name: str, default: float = 0.0) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


# Watchdog fires at this multiple of the soft wall-clock budget: the budget
# itself is enforced cooperatively between lane steps; the watchdog only
# catches a lane that is stuck INSIDE a blocking call.
_WATCHDOG_BUDGET_FACTOR = 1.5
_WATCHDOG_POLL_SECONDS = 30.0
_WATCHDOG_EXIT_CODE = 70  # EX_SOFTWARE — distinguishable from lane failures


def _install_hang_forensics(
    started: float,
    budget_seconds: float,
    *,
    _exit=os._exit,
    poll_seconds: float = _WATCHDOG_POLL_SECONDS,
) -> None:
    """Make an in-lane hang observable and bounded.

    8 of 17 nights the sweep was killed by the unit's start timeout with
    ZERO journal output: a blocking call inside a lane (the wall-clock
    budget is only checked BETWEEN steps) while stdout was still
    block-buffered, so even pre-hang prints died with the SIGKILL.

    * line-buffer stdout/stderr so every checkpoint reaches journald when
      printed, not at process exit;
    * dump all thread stacks on SIGTERM (the unit's kill signal) so the
      journal shows exactly WHERE the sweep hung;
    * when a soft budget is configured, a daemon watchdog thread hard-aborts
      (with a full traceback dump) at 1.5x that budget — a bounded,
      self-diagnosing failure instead of 40 silent minutes.
    """
    import faulthandler

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass

    try:
        import signal as _signal

        faulthandler.register(_signal.SIGTERM, chain=True)
    except Exception:
        pass

    if budget_seconds and budget_seconds > 0:
        import threading

        deadline = budget_seconds * _WATCHDOG_BUDGET_FACTOR

        def _watch() -> None:
            while True:
                time.sleep(poll_seconds)
                elapsed = time.monotonic() - started
                if elapsed > deadline:
                    print(
                        f"[autoresearch-v2-nightly] WATCHDOG: {elapsed:.0f}s "
                        f"elapsed > {deadline:.0f}s (1.5x budget) — a lane is "
                        "stuck inside a blocking call; dumping stacks and "
                        "aborting.",
                        file=sys.stderr,
                        flush=True,
                    )
                    faulthandler.dump_traceback(file=sys.stderr)
                    _exit(_WATCHDOG_EXIT_CODE)

        threading.Thread(
            target=_watch, name="ar-v2-watchdog", daemon=True
        ).start()


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
    scanned = len(result.get("files") or [])
    expected_no_files = result.get("reason") == "no files resolved"
    summary = {
        "subsystem": subsystem,
        "ok": bool(result.get("ok")),
        "findings": len(findings),
        "tokens": int(result.get("tokens") or 0),
        "usage_source": str(result.get("usage_source") or "measured"),
        # Deep audit's model calls == tool-loop iterations.
        "llm_calls": int(result.get("iterations") or 0),
        "model": result.get("model"),
        "reason": result.get("reason") or "",
        "scanned": scanned,
        "errors": 0 if result.get("ok") or expected_no_files else max(1, scanned),
    }
    summary["outcome"] = _classify_deep_audit(summary).outcome
    return summary


def run_test_foundry_lane(
    targets: Sequence[str],
    *,
    max_mutants: int,
    started: float | None = None,
    budget_seconds: float = 0.0,
) -> list[dict[str, Any]]:
    """Run Test-Foundry over each target (dry-run); return per-target summaries.

    Mutation-testing a heavy target (large suite x ``max_mutants``) can run for
    tens of minutes, and the day-rotated target pick means some nights overrun
    the systemd start timeout and get SIGTERM'd mid-run (burning ~40 min CPU for
    no report). When ``budget_seconds`` is set, the wall-clock budget is checked
    BEFORE each target so a heavy night degrades gracefully — remaining targets
    are marked skipped and the nightly still posts a partial report instead of
    failing hard. ``started`` is the run start (``time.monotonic()``).
    """
    summaries: list[dict[str, Any]] = []
    for target in targets:
        if started is not None and _budget_exhausted(started, budget_seconds):
            summaries.append({
                "target": target,
                "ok": False,
                "tests_kept": 0,
                "survivors": 0,
                "tokens": 0,
                "llm_calls": 0,
                "model": None,
                "reason": "skipped: wall-clock budget exhausted",
            })
            continue
        payload = test_foundry.write_request(target=target, max_mutants=max_mutants, apply=False)
        result = test_foundry.run_request_file(Path(payload["request_path"]))
        summary = {
            "target": target,
            "ok": bool(result.get("ok")),
            "tests_kept": int(result.get("tests_kept") or 0),
            "survivors": len(result.get("survivors") or []),
            "tokens": int(result.get("tokens") or 0),
            "usage_source": str(result.get("usage_source") or "measured"),
            # Only surviving mutants trigger an LLM call — killed mutants must
            # never count as healthy model calls for the cooldown.
            "llm_calls": int(result.get("llm_calls") or 0),
            "model": result.get("model"),
            "reason": result.get("reason") or "",
            "scanned": int(result.get("mutants_run") or 0),
            "errors": int(result.get("infra_errors") or 0) + int(result.get("invalid_outputs") or 0),
        }
        summary["outcome"] = _classify_test_foundry(summary).outcome
        summaries.append(summary)
    return summaries


def _classify_deep_audit(summary: dict[str, Any]) -> LaneOutcome:
    reason = str(summary.get("error") or summary.get("reason") or "")
    errors = int(summary.get("errors") or (1 if summary.get("error") else 0))
    scanned = int(summary.get("scanned") or 0)
    if not summary.get("ok") and errors > 0 and scanned > 0 and "no files resolved" not in reason.lower():
        errors = max(errors, scanned)
    return classify_lane_outcome(
        "deep-audit",
        scanned=scanned,
        errors=errors,
        yielded=int(summary.get("findings") or 0),
        ok=bool(summary.get("ok")) and not bool(summary.get("error")),
        reason=reason,
    )


def _classify_test_foundry(summary: dict[str, Any]) -> LaneOutcome:
    return classify_lane_outcome(
        "test-foundry",
        scanned=int(summary.get("scanned") or 0),
        errors=int(summary.get("errors") or 0),
        yielded=int(summary.get("tests_kept") or 0),
        ok=bool(summary.get("ok")),
        reason=str(summary.get("reason") or ""),
    )


def _da_line(da: dict[str, Any] | None) -> str:
    if da is None:
        return "🔍 Deep-Audit · (übersprungen)"
    if da.get("error"):
        outcome = da.get("outcome") or _classify_deep_audit(da).outcome
        return f"🔍 Deep-Audit · {da.get('subsystem', '?')} · FEHLER: {da['error']} [{outcome}]"
    model = da.get("model") or "?"
    tail = f"{da['findings']} Funde · {_fmt_tok(da['tokens'])} tok · {model}"
    reason = _short_reason(da.get("reason")) if not da.get("findings") else ""
    if reason:
        tail += f" ({reason})"
    outcome = da.get("outcome") or _classify_deep_audit(da).outcome
    tail += f" [{outcome}]"
    return f"🔍 Deep-Audit · {da['subsystem']} · {tail}"


def _tf_line(tf: list[dict[str, Any]] | None, error: str | None = None) -> str:
    if error:
        skip_outcome = _expected_skip_error(error)
        if skip_outcome:
            return f"🧪 Test-Foundry · übersprungen: {error} [{skip_outcome}]"
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
            outcome = item.get("outcome") or _classify_test_foundry(item).outcome
            parts.append(f"{name}(0{', ' + reason if reason else ''}) [{outcome}]")
    total = sum(int(i.get("tokens") or 0) for i in tf)
    return f"🧪 Test-Foundry · {', '.join(parts)} · {_fmt_tok(total)} tok"


def _run_reconciler() -> dict:
    summary = reconciler.reconcile_proposals()
    print(json.dumps({"lane": "reconcile", **summary}, indent=2, ensure_ascii=False))
    return summary


def _run_shadow_verifier() -> dict:
    summary = outcome_verifier.run_shadow_verifier(phase="shadow", max_measurements=3)
    print(json.dumps({"lane": "outcome-shadow", **summary}, indent=2, ensure_ascii=False))
    return summary


def _record_lane_cooldown(lane: str, outcome: LaneOutcome, summary: dict[str, Any]) -> None:
    """Best-effort zero-yield streak bookkeeping (never sinks the report)."""
    try:
        if summary.get("llm_calls") is not None:
            # Real model calls only — e.g. Test Foundry mutants that were
            # killed without an LLM call must not look like healthy calls.
            healthy_calls = max(0, int(summary.get("llm_calls") or 0) - int(summary.get("errors") or 0))
        else:
            healthy_calls = max(0, int(summary.get("scanned") or 0) - int(summary.get("errors") or 0))
        arb.record_lane_run_for_cooldown(
            lane,
            outcome=outcome.outcome,
            yielded=outcome.yielded,
            healthy_calls=healthy_calls,
        )
    except Exception:
        pass


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
    parser.add_argument("--tf-targets", type=int, default=None,
                        help="Default: autoresearch.lanes.test-foundry.budget.targets (config.yaml)")
    parser.add_argument("--tf-mutants", type=int, default=None,
                        help="Default: autoresearch.lanes.test-foundry.budget.max_mutants (config.yaml)")
    parser.add_argument("--da-max-files", type=int, default=None,
                        help="Default: autoresearch.lanes.deep-audit.budget.max_files (config.yaml)")
    parser.add_argument("--wall-clock-budget-seconds", type=float, default=None)
    parser.add_argument("--ignore-cooldown", action="store_true",
                        help="operator override: run lanes despite an active zero-yield cooldown")
    parser.add_argument("--circuit-breaker-threshold", type=int, default=2)
    args = parser.parse_args(argv)

    when = _parse_date(args.date)
    day = day_of_year(when)
    lanes = {lane.strip() for lane in args.lanes.split(",") if lane.strip()}
    started = time.monotonic()
    specs = _lane_specs()
    da_max_files = args.da_max_files if args.da_max_files is not None else int(
        specs["deep-audit"].budget.get("max_files") or 6)
    tf_targets_n = args.tf_targets if args.tf_targets is not None else int(
        specs["test-foundry"].budget.get("targets") or 1)
    tf_mutants = args.tf_mutants if args.tf_mutants is not None else int(
        specs["test-foundry"].budget.get("max_mutants") or 6)

    # Per-lane wall-clock: CLI > legacy env override > validated lane contract.
    override = args.wall_clock_budget_seconds
    if override is None:
        env_budget = _env_float("AR_V2_WALL_CLOCK_BUDGET_SECONDS", 0.0)
        override = env_budget if env_budget > 0 else None
    da_budget = float(override) if override is not None else float(
        specs["deep-audit"].budget.get("wall_clock_seconds") or 600)
    tf_budget = float(override) if override is not None else float(
        specs["test-foundry"].budget.get("wall_clock_seconds") or 600)
    total_budget = da_budget + tf_budget

    # Install hang forensics BEFORE any lane runs: line-buffer stdout/stderr,
    # register the SIGTERM stack dumper, and start the watchdog. Without this
    # the 8/17-nights silent-hang-to-unit-timeout failure recurs.
    _install_hang_forensics(started, total_budget)
    print(
        f"[autoresearch-v2-nightly] start: lanes={sorted(lanes)} day={day} "
        f"budget(da/tf)={da_budget:.0f}s/{tf_budget:.0f}s",
        flush=True,
    )

    circuit_failures = 0

    da_summary: dict[str, Any] | None = None
    tf_summary: list[dict[str, Any]] | None = None
    tf_error: str | None = None

    def _cooldown_gate(lane: str) -> str | None:
        until = arb.lane_cooldown_until(lane)
        if until and not args.ignore_cooldown:
            return f"cooldown active until {until} (healthy zero-yield runs)"
        if until:
            print(f"[autoresearch-v2-nightly] operator override: --ignore-cooldown ({lane} until {until})",
                  file=sys.stderr)
        return None

    if "deep-audit" in lanes:
        subsystem = select_subsystem(list(deep_audit.SUBSYSTEM_GLOBS.keys()), day)
        if _budget_exhausted(started, total_budget):
            da_summary = {"subsystem": subsystem, "error": "Wall-clock budget exhausted before Deep-Audit"}
        elif (skip := _quota_gate("deep-audit") or _cooldown_gate("deep-audit")):
            da_summary = {
                "subsystem": subsystem, "ok": True, "findings": 0, "tokens": 0,
                "model": None, "reason": skip, "scanned": 0, "errors": 0,
            }
            da_summary["outcome"] = _classify_deep_audit(da_summary).outcome
            print(f"[autoresearch-v2-nightly] deep-audit skipped: {skip}", flush=True)
        else:
            try:
                da_summary = run_deep_audit_lane(subsystem, max_files=da_max_files)
            except Exception as exc:  # one lane must never kill the other / the report
                traceback.print_exc()
                circuit_failures += 1
                da_summary = {
                    "subsystem": subsystem, "error": _lane_error(exc), "ok": False,
                    "findings": 0, "tokens": 0, "scanned": 0, "errors": 1,
                }
                da_summary["outcome"] = _classify_deep_audit(da_summary).outcome
            _record_lane_cooldown("deep-audit", _classify_deep_audit(da_summary), da_summary)

    if "test-foundry" in lanes:
        if _circuit_open(circuit_failures, args.circuit_breaker_threshold):
            tf_error = "Circuit breaker open before Test-Foundry"
        elif _budget_exhausted(started, total_budget):
            tf_error = "Wall-clock budget exhausted before Test-Foundry"
        # A fresh quota decision between lanes: session >= 60% stops the next
        # lane of the same night window.
        elif (skip := _quota_gate("test-foundry") or _cooldown_gate("test-foundry")):
            tf_error = skip
            print(f"[autoresearch-v2-nightly] test-foundry skipped: {skip}", flush=True)
        else:
            targets = select_targets(test_foundry.curated_targets(), day, tf_targets_n)
            tf_started = time.monotonic()
            try:
                tf_summary = run_test_foundry_lane(
                    targets,
                    max_mutants=tf_mutants,
                    started=tf_started,
                    budget_seconds=tf_budget,
                )
            except Exception as exc:
                traceback.print_exc()
                circuit_failures += 1
                tf_error = _lane_error(exc)
            for item in tf_summary or []:
                _record_lane_cooldown("test-foundry", _classify_test_foundry(item), item)

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

    try:
        _run_shadow_verifier()
    except Exception as exc:  # Shadow-Messung darf den Forschungsbericht nie killen
        traceback.print_exc()
        print(f"[autoresearch-v2-nightly] outcome shadow fehlgeschlagen: {exc}", file=sys.stderr)

    message = build_summary(when, da_summary, tf_summary, tf_error=tf_error)
    print(message)

    outcomes: list[LaneOutcome] = []
    if "deep-audit" in lanes and da_summary is not None:
        outcomes.append(_classify_deep_audit(da_summary))
    if "test-foundry" in lanes:
        outcomes.extend(_classify_test_foundry(item) for item in (tf_summary or []))
        if tf_error:
            outcomes.append(classify_lane_outcome(
                "test-foundry", scanned=0, errors=1, yielded=0, ok=False, reason=tf_error
            ))
    result_code = nightly_exit_code(outcomes)

    if args.send:
        try:
            post_summary(message, channel_id=args.channel_id)
        except Exception as exc:
            traceback.print_exc()
            print(f"[autoresearch-v2-nightly] Discord-Post fehlgeschlagen: {exc}", file=sys.stderr)
            return 1
    return result_code


if __name__ == "__main__":
    raise SystemExit(main())
