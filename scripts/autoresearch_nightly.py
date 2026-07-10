#!/usr/bin/env python3
"""Nightly autoresearch sweep (systemd-timer entrypoint) — rotating coverage.

Alternates lane by day-of-year so the whole surface gets covered over time,
unattended and always **dry-run** (proposals only — no skill or code file is
mutated; fixes are applied solely through the operator's judge + batch-confirm
step in the dashboard):

  * even day  → SKILL lane: capability sweep over the used-skill set, with
    ``HERMES_AUTORESEARCH_MIN_USE_COUNT=1`` so it reaches the long tail the
    converged ≥5-threshold never touches.
  * odd day   → CODE lane: one incremental code-weakness batch over the
    ``hermes_cli/`` allowlist (only changed/unscanned files, capped).

Budget guard (plan 2026-07-10): before any lane runs, the subscription quota
is checked (weekly >= 50% skips expensive models, >= 70% skips everything,
session >= 60% stops the window) and a lane cooldown from three healthy
zero-yield runs is honored (override: ``--ignore-cooldown``). Lane caps come
from the validated lane contracts (``autoresearch.lanes`` in config.yaml);
``AR_NIGHTLY_ITERATIONS`` stays honored as a backwards-compatible override
until the unit drops it. Every model call inside the lanes is additionally
bounded by the shared daily ledger.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO), str(_REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import autoresearch_request as arr  # noqa: E402
import run_autoresearch_request as runner  # noqa: E402
from hermes_cli import autoresearch_budget as arb  # noqa: E402
from hermes_cli import autoresearch_reconcile as reconciler  # noqa: E402
from hermes_cli.autoresearch_lane_contracts import (  # noqa: E402
    FATAL_OUTCOMES,
    classify_lane_outcome,
    load_lane_specs,
)


def _is_code_night() -> bool:
    """Coverage rotation: odd day-of-year = code scan, even = skill long-tail."""
    return datetime.now(timezone.utc).timetuple().tm_yday % 2 == 1


def _lane_specs():
    try:
        return load_lane_specs()
    except Exception:
        return load_lane_specs(config={})


def _lane_model(lane_aux_task: str) -> str:
    """Effective model behind the lane's aux task (best-effort)."""
    try:
        from agent.auxiliary_client import _resolve_task_provider_model

        _provider, model, _base, _key, _mode = _resolve_task_provider_model(lane_aux_task)
        return str(model or "")
    except Exception:
        return ""


def _print_skip(lane: str, reason: str) -> None:
    outcome = classify_lane_outcome(lane, scanned=0, errors=0, yielded=0, ok=True, reason=reason)
    print(json.dumps({"lane": lane, **outcome.as_dict()}, indent=2))


def _gate_lane(lane: str, aux_task: str, *, ignore_cooldown: bool) -> str | None:
    """Quota + cooldown gate. Returns a skip reason or None (lane may run)."""
    budget_cfg = arb.load_budget_config()
    snapshot = arb.fetch_quota_snapshot()
    decision = arb.evaluate_quota(snapshot, budget_cfg)
    try:
        arb.DailyLedger(config=budget_cfg).record_quota_snapshot(
            f"pre-{lane}",
            session_percent=decision.session_percent,
            weekly_percent=decision.weekly_percent,
        )
    except Exception:
        pass
    blocked = arb.quota_block_reason(decision, _lane_model(aux_task))
    if blocked:
        return blocked
    until = arb.lane_cooldown_until(lane)
    if until and not ignore_cooldown:
        return f"cooldown active until {until} (healthy zero-yield runs)"
    if until and ignore_cooldown:
        print(f"[autoresearch-nightly] operator override: --ignore-cooldown (until {until})", file=sys.stderr)
    return None


def _run_skill_night(specs=None) -> int:
    lane_specs = specs or _lane_specs()
    config_cap = int(lane_specs["skill"].budget.get("max_iterations") or 12)
    env_cap = os.environ.get("AR_NIGHTLY_ITERATIONS")
    iterations = int(env_cap) if env_cap else config_cap
    iterations = max(1, min(iterations, arr.MAX_ITERATIONS))
    # Long-tail: research skills below the default use-count threshold. The runner
    # reads this via hermes_cli.autoresearch_proposals._usage_min_use_count().
    os.environ.setdefault("HERMES_AUTORESEARCH_MIN_USE_COUNT", "1")
    request_path = arr.create_request(
        mode="skills", area="all", focus="capability",
        max_iterations=iterations, mutation_policy="requires_operator_go",
    )
    # Dry-run (no --apply): proposals queued, no skill file mutated.
    return runner.main([str(request_path), "--max-iterations", str(iterations)])


def _run_code_night(specs=None) -> int:
    from hermes_cli.autoresearch_proposals import generate_code_weakness_proposals
    lane_specs = specs or _lane_specs()
    max_files = int(lane_specs["code"].budget.get("max_files") or 12)
    limit = int(lane_specs["code"].budget.get("max_proposals") or 4)
    res = generate_code_weakness_proposals(scope="incremental", max_files=max_files, limit=limit)
    outcome_name = str(res.get("outcome") or "")
    if not outcome_name:
        try:
            outcome_name = classify_lane_outcome(
                "code",
                scanned=int(res.get("files_seen") or 0),
                errors=len(res.get("errors") or []),
                yielded=int(res.get("findings_seen") or 0),
                ok=bool(res.get("ok")),
                reason="; ".join(str(item.get("reason") or "") for item in (res.get("errors") or [])[:3]),
            ).outcome
        except Exception:
            outcome_name = "invalid_output"
    print(json.dumps({
        "lane": "code", "created_count": res.get("created_count"),
        "files_seen": res.get("files_seen"), "skipped_unchanged": res.get("skipped_unchanged"),
        "vetoed": res.get("vetoed"),
        "errors": len(res.get("errors") or []), "outcome": outcome_name,
        "tokens": res.get("tokens"), "usage_source": res.get("usage_source"),
        "scope": res.get("scope"),
    }, indent=2))
    return 2 if outcome_name in FATAL_OUTCOMES else 0


def _run_reconciler() -> dict:
    summary = reconciler.reconcile_proposals()
    print(json.dumps({"lane": "reconcile", **summary}, indent=2, ensure_ascii=False))
    return summary


def main(argv: list[str] | None = None) -> int:
    """Rotating, unattended coverage, followed by proposal reconciliation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ignore-cooldown", action="store_true",
        help="operator override: run the lane despite an active zero-yield cooldown",
    )
    args = parser.parse_args([] if argv is None else argv)

    specs = _lane_specs()
    lane = "code" if _is_code_night() else "skill"
    skip_reason = _gate_lane(lane, specs[lane].aux_task, ignore_cooldown=args.ignore_cooldown)
    if skip_reason:
        _print_skip(lane, skip_reason)
        rc = 0
    else:
        rc = _run_code_night(specs) if lane == "code" else _run_skill_night(specs)
        try:
            decision = arb.evaluate_quota(arb.fetch_quota_snapshot(), arb.load_budget_config())
            arb.DailyLedger().record_quota_snapshot(
                f"post-{lane}",
                session_percent=decision.session_percent,
                weekly_percent=decision.weekly_percent,
            )
        except Exception:
            pass
    try:
        _run_reconciler()
    except Exception as exc:
        print(f"[autoresearch-nightly] reconciler failed: {exc}", file=sys.stderr)
        return rc or 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
