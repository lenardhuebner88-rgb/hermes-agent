#!/usr/bin/env python3
"""Pricing coverage gate over observed (model, provider) combinations.

Mechanical check from Canon ``2026-07-31-metrik-ssot-register`` §7.4:
every (model, provider) combination observed in ``run_usage_facts``
within the window must be fully priceable through the fork pricing seam
(``hermes_cli/usage_facts_pricing.py``) — or land on the named, reasoned
remainder list.  Rows with ``model='<synthetic>'`` carry zero tokens in
every bucket; they are excluded from the denominator (Fable D3): there
is nothing to price, and attributing them would invent facts.

Priceability is evaluated per observed origin, because cache semantics
differ by client path (Anthropic protocol with cache_control vs
OpenAI-compatible implicit caching).  Remainder entries carry a
classification: ``documented_absent`` (the rate sourcedly does not
exist — negative proof on file) vs ``open_gap`` vs capture gaps
(``model_missing`` / ``provider_missing``).

Read-only against the facts DB.  Exit 0 when coverage is complete,
exit 1 when a remainder stays.

Examples::

    scripts/check_usage_facts_pricing_coverage.py --days 30
    scripts/check_usage_facts_pricing_coverage.py \\
        --db /tmp/probe/usage_facts.db --days 30
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hermes_cli.usage_facts_db import usage_facts_db_path  # noqa: E402
from hermes_cli.usage_facts_pricing import priceability  # noqa: E402

SYNTHETIC_MODEL = "<synthetic>"


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    resolved = db_path.expanduser().resolve()
    connection = sqlite3.connect(
        f"file:{quote(str(resolved), safe='/')}?mode=ro",
        uri=True,
        timeout=2.0,
    )
    connection.row_factory = sqlite3.Row
    return connection


def coverage_report(
    db_path: Path | str,
    *,
    days: int = 30,
) -> dict[str, Any]:
    resolved = usage_facts_db_path(db_path)
    connection = _connect_read_only(resolved)
    try:
        rows = connection.execute(
            """
            SELECT model,
                   provider,
                   origin,
                   COUNT(*) AS run_count,
                   COALESCE(SUM(input_tokens), 0)
                       + COALESCE(SUM(output_tokens), 0)
                       + COALESCE(SUM(cache_read_tokens), 0)
                       + COALESCE(SUM(cache_write_tokens), 0) AS token_count
              FROM run_usage_facts
             WHERE captured_at >= datetime('now', ?)
               AND (model IS NULL OR model != ?)
             GROUP BY model, provider, origin
             ORDER BY run_count DESC
            """,
            (f"-{int(days)} days", SYNTHETIC_MODEL),
        ).fetchall()
    finally:
        connection.close()

    priced: list[dict[str, Any]] = []
    remainder: list[dict[str, Any]] = []
    for row in rows:
        model = row["model"]
        provider = row["provider"]
        origin = row["origin"]
        check = priceability(model, provider, origin)
        item = {
            "model": model,
            "provider": provider,
            "origin": origin,
            "run_count": int(row["run_count"]),
            "token_count": int(row["token_count"]),
            "resolved_provider": check.get("resolved_provider"),
            "resolved_model": check.get("resolved_model"),
            "alias_of": check.get("alias_of"),
            "classification": check.get("classification"),
        }
        if check["priceable"]:
            priced.append(item)
        else:
            item["reason"] = check["reason"]
            remainder.append(item)

    total_runs = sum(item["run_count"] for item in priced) + sum(
        item["run_count"] for item in remainder
    )
    total_tokens = sum(item["token_count"] for item in priced) + sum(
        item["token_count"] for item in remainder
    )
    priced_runs = sum(item["run_count"] for item in priced)
    priced_tokens = sum(item["token_count"] for item in priced)

    classification_counts: dict[str, int] = {}
    for item in remainder:
        key = str(item["classification"])
        classification_counts[key] = classification_counts.get(key, 0) + 1

    return {
        "db": str(resolved),
        "window_days": int(days),
        "synthetic_rows_excluded_from_denominator": True,
        "observed_items": len(priced) + len(remainder),
        "priced_items": len(priced),
        "run_coverage": {
            "priced": priced_runs,
            "total": total_runs,
            "ratio": (priced_runs / total_runs) if total_runs else None,
        },
        "token_coverage": {
            "priced": priced_tokens,
            "total": total_tokens,
            "ratio": (priced_tokens / total_tokens) if total_tokens else None,
        },
        "complete": not remainder,
        "remainder_by_classification": classification_counts,
        "priced": priced,
        "remainder": remainder,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check pricing coverage of observed (model, provider) "
            "combinations in usage_facts.db (read-only)."
        )
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="usage_facts.db path (default: HERMES_USAGE_FACTS_DB or /mnt/data/...)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Observation window in days (default: 30)",
    )
    args = parser.parse_args(argv)
    report = coverage_report(args.db, days=args.days)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
