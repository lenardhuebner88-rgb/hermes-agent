"""Fork-owned: an honestly-measured token burn rate for the Hermes Deck phone app.

The app wants one number ("14,2 M Token/h") instead of today's "Budget unbekannt".
Getting that number wrong is worse than not having it, so this module is built
around two failure modes that were *measured*, not hypothesised, against the
live observability database:

1. **String-comparison trap.** SQLite's ``datetime('now','-1 hour')`` renders
   ``'2026-08-05 20:02:31'`` (a space between date and time), while
   ``occurred_at`` is stored as ISO8601 with a literal ``'T'`` and a trailing
   ``'Z'`` (``'2026-08-05T21:00:44.856Z'``). Because ``'T'`` (0x54) sorts
   *after* ``' '`` (0x20) in a plain string comparison, ``occurred_at >=
   datetime('now','-1 hour')`` matches the entire day instead of the last
   hour — 5,913 rows instead of 272 in one real measurement. So the cutoff is
   built in Python in the exact same ``%Y-%m-%dT%H:%M:%S.000Z`` shape as the
   column and passed in as a bound parameter; the query never calls
   ``datetime()`` itself.

2. **cache_read dominates.** In a real hour: 272 calls, ~850k "billable"
   tokens (input + output + cache_write) but ~50M cache_read tokens — the
   models re-read their context on every turn. A rate that folds cache_read
   in is off by a factor of roughly 59 and answers a different question
   ("how much context got re-read") than the one the burn-rate bar is meant
   to answer ("how much new work is the fleet doing"). The two are kept as
   separate counters throughout; only ``billable_tokens`` feeds the headline
   rate.

Attribution by ``lane`` (which agent/profile burned the tokens) comes from a
LEFT JOIN of ``run_llm_calls`` onto ``run_usage_facts.run_id`` — the calls
table has no lane of its own. Measured live coverage of that join has ranged
from single digits to zero out of ~270 calls/hour: most runs never write a
``run_usage_facts`` row with a lane before the window closes. That is not a
bug to fix here; it means a per-agent breakdown built from this join would be
drawing bars for a handful of runs and calling it the fleet. So lane
attribution is reported separately with an explicit ``attributed_share``, and
callers (the UI) must treat a low share as "cannot draw per-agent bars from
this", not as "no other agents are running".
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path
from typing import Any

# Overridable so tests can point at a throwaway SQLite file instead of the
# live observability DB.
DEFAULT_DB_PATH = "/mnt/data/hermes-observability/usage_facts.db"

DEFAULT_WINDOW_MINUTES = 60

# Below this many calls, or below this many covered seconds, an hourly
# extrapolation is noise, not a measurement.
MIN_CALLS_FOR_RATE = 2
MIN_COVERED_SECONDS_FOR_RATE = 5 * 60

_OCCURRED_AT_FORMAT = "%Y-%m-%dT%H:%M:%S.000Z"


def _format_cutoff(moment: _dt.datetime) -> str:
    """Render *moment* in the exact string shape ``occurred_at`` uses.

    Must stay lexicographically comparable to the stored column (same
    separators, same field widths) — see module docstring, trap 1.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    moment = moment.astimezone(_dt.timezone.utc)
    return moment.strftime(_OCCURRED_AT_FORMAT)


def _open_readonly(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _empty_breakdown() -> dict[str, dict[str, int]]:
    return {}


def compute_burn_rate(
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Measure the fleet's token burn rate over the trailing *window_minutes*.

    Returns a payload (never raises for a missing/empty DB — see the
    "Ehrlichkeit" rules below):

    - ``window_minutes``: the requested window.
    - ``calls``: number of ``run_llm_calls`` rows in the window.
    - ``billable_tokens`` / ``cache_read_tokens``: kept apart, see trap 2.
    - ``rate_billable_tokens_per_hour``: ``None`` unless there is enough
      signal to extrapolate honestly (see below); otherwise a float derived
      from the *actually covered* span, not the requested window.
    - ``rate_reason``: ``None`` when a rate was produced, otherwise a short
      German string explaining why not.
    - ``covered_from`` / ``covered_to``: the oldest/newest ``occurred_at`` seen
      in the window (``None`` if there were no rows).
    - ``covered_seconds``: span between those two, or ``None``.
    - ``by_provider`` / ``by_model``: per-key breakdown of
      ``{"calls", "billable_tokens", "cache_read_tokens"}``.
    - ``lane_attribution``: ``{"attributed_calls", "attributed_share",
      "by_lane", "note"}`` — see module docstring on why this is kept
      separate from, and must not be mistaken for, a full per-agent split.
    - ``source``: ``"db_missing"``, ``"db_empty"`` or ``"ok"``.

    A rate is only produced when there are at least ``MIN_CALLS_FOR_RATE``
    calls *and* the covered span is at least ``MIN_COVERED_SECONDS_FOR_RATE``
    — extrapolating an hourly rate from one call, or from calls a few seconds
    apart, is not a measurement.
    """
    db_path = str(db_path)
    if not Path(db_path).exists():
        return _empty_result(
            window_minutes,
            source="db_missing",
            rate_reason=f"Datenbank nicht gefunden: {db_path}",
        )

    moment = now or _dt.datetime.now(_dt.timezone.utc)
    cutoff = _format_cutoff(moment - _dt.timedelta(minutes=window_minutes))

    conn = _open_readonly(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(input_tokens + output_tokens + cache_write_tokens), 0),
                COALESCE(SUM(cache_read_tokens), 0),
                MIN(occurred_at),
                MAX(occurred_at)
            FROM run_llm_calls
            WHERE occurred_at >= ?
            """,
            (cutoff,),
        )
        calls, billable_tokens, cache_read_tokens, covered_from, covered_to = cur.fetchone()

        if calls == 0:
            return _empty_result(
                window_minutes,
                source="db_empty",
                rate_reason="keine Calls im Zeitfenster",
            )

        covered_seconds = _seconds_between(covered_from, covered_to)

        rate: float | None = None
        rate_reason: str | None = None
        if calls < MIN_CALLS_FOR_RATE:
            rate_reason = "zu wenig Messpunkte"
        elif covered_seconds is None or covered_seconds < MIN_COVERED_SECONDS_FOR_RATE:
            rate_reason = "zu wenig Messpunkte"
        else:
            rate = billable_tokens / covered_seconds * 3600.0

        by_provider = _breakdown(cur, cutoff, "provider")
        by_model = _breakdown(cur, cutoff, "model")
        lane_attribution = _lane_attribution(cur, cutoff, calls)

        return {
            "window_minutes": window_minutes,
            "calls": calls,
            "billable_tokens": billable_tokens,
            "cache_read_tokens": cache_read_tokens,
            "rate_billable_tokens_per_hour": rate,
            "rate_reason": rate_reason,
            "covered_from": covered_from,
            "covered_to": covered_to,
            "covered_seconds": covered_seconds,
            "by_provider": by_provider,
            "by_model": by_model,
            "lane_attribution": lane_attribution,
            "source": "ok",
        }
    finally:
        conn.close()


def _seconds_between(from_iso: str | None, to_iso: str | None) -> float | None:
    if not from_iso or not to_iso:
        return None
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    try:
        start = _dt.datetime.strptime(from_iso, fmt)
        end = _dt.datetime.strptime(to_iso, fmt)
    except ValueError:
        return None
    return (end - start).total_seconds()


def _breakdown(cur: sqlite3.Cursor, cutoff: str, column: str) -> dict[str, dict[str, int]]:
    cur.execute(
        f"""
        SELECT
            {column},
            COUNT(*),
            COALESCE(SUM(input_tokens + output_tokens + cache_write_tokens), 0),
            COALESCE(SUM(cache_read_tokens), 0)
        FROM run_llm_calls
        WHERE occurred_at >= ?
        GROUP BY {column}
        """,
        (cutoff,),
    )
    result: dict[str, dict[str, int]] = {}
    for key, calls, billable_tokens, cache_read_tokens in cur.fetchall():
        result[key or "unbekannt"] = {
            "calls": calls,
            "billable_tokens": billable_tokens,
            "cache_read_tokens": cache_read_tokens,
        }
    return result


def _lane_attribution(cur: sqlite3.Cursor, cutoff: str, total_calls: int) -> dict[str, Any]:
    cur.execute(
        """
        SELECT
            u.lane,
            COUNT(*),
            COALESCE(SUM(l.input_tokens + l.output_tokens + l.cache_write_tokens), 0)
        FROM run_llm_calls l
        LEFT JOIN run_usage_facts u ON u.run_id = l.run_id
        WHERE l.occurred_at >= ? AND u.lane IS NOT NULL
        GROUP BY u.lane
        """,
        (cutoff,),
    )
    by_lane: dict[str, dict[str, int]] = {}
    attributed_calls = 0
    for lane, calls, billable_tokens in cur.fetchall():
        by_lane[lane] = {"calls": calls, "billable_tokens": billable_tokens}
        attributed_calls += calls

    attributed_share = (attributed_calls / total_calls) if total_calls else None
    return {
        "attributed_calls": attributed_calls,
        "attributed_share": attributed_share,
        "by_lane": by_lane,
        "note": (
            "run_usage_facts traegt lane nur fuer einen Bruchteil der Runs; "
            "attributed_share unter ~0.5 heisst: keine Pro-Agent-Balken aus "
            "diesen Daten zeichnen."
        ),
    }


def _empty_result(window_minutes: int, *, source: str, rate_reason: str) -> dict[str, Any]:
    return {
        "window_minutes": window_minutes,
        "calls": 0,
        "billable_tokens": 0,
        "cache_read_tokens": 0,
        "rate_billable_tokens_per_hour": None,
        "rate_reason": rate_reason,
        "covered_from": None,
        "covered_to": None,
        "covered_seconds": None,
        "by_provider": _empty_breakdown(),
        "by_model": _empty_breakdown(),
        "lane_attribution": {
            "attributed_calls": 0,
            "attributed_share": None,
            "by_lane": {},
            "note": (
                "run_usage_facts traegt lane nur fuer einen Bruchteil der Runs; "
                "attributed_share unter ~0.5 heisst: keine Pro-Agent-Balken aus "
                "diesen Daten zeichnen."
            ),
        },
        "source": source,
    }
