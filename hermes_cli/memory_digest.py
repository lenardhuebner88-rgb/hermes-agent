"""``hermes memory digest`` — weekly decision extract from completion receipts.

Backs the architecture proposal delivered in task ``t_dbc95124`` (Shared
Decision/Facts Ledger · Receipt-first Extraction · Memory-Router). Rather than
growing another cross-agent boot file, durable decisions live in the receipts
agents already write — the structured completion metadata of each kanban run —
and are *pulled* on demand instead of *pushed* into every session prompt.

Two pieces live here:

* :func:`normalize_completion_metadata` — the canonical completion-metadata
  schema. Workers report ``decisions[]`` / ``operator_followup`` /
  ``supersedes[]`` (the MANDATORY handoff already asks for ``decisions``); this
  normalises the loose shapes they actually send (string shorthand, a lone
  value, alias keys) into canonical records while preserving every unrelated
  key. Applied on the ``hermes kanban complete`` write path so stored receipts
  are schema-consistent going forward, and again on read so legacy rows still
  parse.

* :func:`build_digest` / :func:`render_digest` — receipt-first extraction over
  a window of completed ``task_runs`` into the digest product: top decisions
  (newest first, de-duplicated), open operator follow-ups, and superseded
  references, each with a source-task link.

The CLI entry point is :func:`cmd_memory_digest`, wired as
``hermes memory digest`` in :mod:`hermes_cli.subcommands.memory`.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable, Optional


# Canonical durable completion-metadata keys. Everything else a worker reports
# (changed_files, tests_run, commit, usage, …) is preserved verbatim.
CANONICAL_KEYS = ("decisions", "operator_followup", "supersedes")

# Keys a decision record may arrive under, in priority order.
_DECISION_TEXT_ALIASES = ("text", "decision", "summary", "title")


# --------------------------------------------------------------------------- #
# Schema normalisation                                                         #
# --------------------------------------------------------------------------- #


def _as_str_list(value: Any) -> list[str]:
    """Coerce a string / list / None into a list of non-blank strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        s = str(item).strip()
        if s:
            out.append(s)
    return out


def normalize_decision(item: Any) -> Optional[dict]:
    """Return a canonical decision record, or ``None`` if it has no text.

    Canonical shape::

        {"text": str, "scope": str | None, "supersedes": [str, ...]}

    Accepts a bare string (shorthand) or a dict using any of the text
    aliases (``text`` / ``decision`` / ``summary`` / ``title``). ``None`` and
    blank items are dropped (return ``None``).
    """
    if item is None:
        return None
    if isinstance(item, str):
        text = item.strip()
        return {"text": text, "scope": None, "supersedes": []} if text else None
    if isinstance(item, dict):
        text = ""
        for key in _DECISION_TEXT_ALIASES:
            raw = item.get(key)
            if raw is not None and str(raw).strip():
                text = str(raw).strip()
                break
        if not text:
            return None
        scope = item.get("scope")
        scope = str(scope).strip() if scope is not None and str(scope).strip() else None
        return {
            "text": text,
            "scope": scope,
            "supersedes": _as_str_list(item.get("supersedes")),
        }
    # Numbers, booleans, etc. — coerce to text so nothing silently vanishes.
    text = str(item).strip()
    return {"text": text, "scope": None, "supersedes": []} if text else None


def _normalize_decisions(value: Any) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[dict] = []
    for item in value:
        rec = normalize_decision(item)
        if rec is not None:
            out.append(rec)
    return out


def normalize_completion_metadata(meta: Any, *, ensure_keys: bool = False) -> dict:
    """Return a copy of ``meta`` with the canonical decision schema applied.

    ``decisions`` become canonical records, ``operator_followup`` and
    ``supersedes`` become string lists. Every other key is preserved
    untouched. A non-dict input yields ``{}``.

    With ``ensure_keys=True`` the three canonical keys are always present
    (empty defaults injected when absent) — useful for callers that want a
    fully-shaped record. By default absent keys stay absent so stored
    metadata is not bloated with empty lists.
    """
    if not isinstance(meta, dict):
        return {key: [] for key in CANONICAL_KEYS} if ensure_keys else {}

    out = dict(meta)
    if "decisions" in out:
        out["decisions"] = _normalize_decisions(out.get("decisions"))
    elif ensure_keys:
        out["decisions"] = []

    if "operator_followup" in out:
        out["operator_followup"] = _as_str_list(out.get("operator_followup"))
    elif ensure_keys:
        out["operator_followup"] = []

    if "supersedes" in out:
        out["supersedes"] = _as_str_list(out.get("supersedes"))
    elif ensure_keys:
        out["supersedes"] = []

    return out


# --------------------------------------------------------------------------- #
# Window parsing                                                               #
# --------------------------------------------------------------------------- #

_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
_SINCE_RE = re.compile(r"^(\d+)([mhdw]?)$")


def parse_since(spec: str) -> int:
    """Parse a window spec like ``7d`` / ``24h`` / ``2w`` / ``30m`` into seconds.

    A bare number defaults to days (``5`` → 5 days). Case-insensitive.
    Raises :class:`ValueError` on anything else.
    """
    if not isinstance(spec, str):
        raise ValueError(f"invalid window: {spec!r}")
    m = _SINCE_RE.match(spec.strip().lower())
    if not m:
        raise ValueError(
            f"invalid window {spec!r}: expected e.g. 7d, 24h, 2w, 30m or a bare number of days"
        )
    n = int(m.group(1))
    if n <= 0:
        raise ValueError(f"window must be positive: {spec!r}")
    unit = m.group(2) or "d"
    return n * _UNIT_SECONDS[unit]


# --------------------------------------------------------------------------- #
# Digest construction                                                          #
# --------------------------------------------------------------------------- #


def build_digest(
    runs: Iterable[dict],
    *,
    now: int,
    since_seconds: int,
    profile: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """Extract a decision digest from completed runs.

    ``runs`` is an iterable of dicts with keys ``task_id``, ``title``,
    ``profile``, ``tenant``, ``ended_at`` (epoch seconds), ``summary`` and
    ``metadata`` (a dict). Runs whose ``ended_at`` falls before the window,
    or whose ``profile`` doesn't match ``profile`` (when given and not
    ``all``), are skipped.

    Returns a JSON-serialisable digest::

        {
          "generated_at": <ts>,
          "window": {"since_seconds": .., "from_ts": .., "to_ts": ..},
          "profile": <str|None>,
          "decisions": [ {text, scope, supersedes, task_id, title,
                          profile, tenant, ended_at}, ... ],   # newest first
          "operator_followups": [ {text, task_id, title, ended_at}, ... ],
          "superseded": [ {ref, by_task, ended_at}, ... ],
          "stats": {runs_in_window, runs_with_decisions, decisions, followups},
        }
    """
    from_ts = now - since_seconds
    want_profile = None if profile in (None, "", "all") else profile

    decisions: list[dict] = []
    followups: list[dict] = []
    superseded: list[dict] = []
    runs_in_window = 0
    runs_with_decisions = 0

    for run in runs:
        ended_at = run.get("ended_at")
        if ended_at is None or ended_at < from_ts:
            continue
        if want_profile is not None and run.get("profile") != want_profile:
            continue
        runs_in_window += 1

        meta = normalize_completion_metadata(run.get("metadata"))
        source = {
            "task_id": run.get("task_id"),
            "title": run.get("title"),
            "profile": run.get("profile"),
            "tenant": run.get("tenant"),
            "ended_at": ended_at,
        }

        run_decisions = meta.get("decisions") or []
        if run_decisions:
            runs_with_decisions += 1
        for dec in run_decisions:
            decisions.append({**dec, **source})
            for ref in dec.get("supersedes") or []:
                superseded.append({"ref": ref, "by_task": run.get("task_id"), "ended_at": ended_at})

        for ref in meta.get("supersedes") or []:
            superseded.append({"ref": ref, "by_task": run.get("task_id"), "ended_at": ended_at})

        for item in meta.get("operator_followup") or []:
            followups.append({
                "text": item,
                "task_id": run.get("task_id"),
                "title": run.get("title"),
                "ended_at": ended_at,
            })

    # Newest first.
    decisions.sort(key=lambda d: d["ended_at"], reverse=True)
    followups.sort(key=lambda f: f["ended_at"], reverse=True)
    superseded.sort(key=lambda s: s["ended_at"], reverse=True)

    # De-duplicate identical decision text (case/space-insensitive), keeping
    # the newest occurrence — agents re-state the same call across retries.
    seen: set[str] = set()
    deduped: list[dict] = []
    for dec in decisions:
        key = " ".join(dec["text"].lower().split())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(dec)
    decisions = deduped

    total_decisions = len(decisions)
    if limit is not None and limit >= 0:
        decisions = decisions[:limit]

    return {
        "generated_at": now,
        "window": {"since_seconds": since_seconds, "from_ts": from_ts, "to_ts": now},
        "profile": want_profile,
        "decisions": decisions,
        "operator_followups": followups,
        "superseded": superseded,
        "stats": {
            "runs_in_window": runs_in_window,
            "runs_with_decisions": runs_with_decisions,
            "decisions": total_decisions,
            "followups": len(followups),
        },
    }


# --------------------------------------------------------------------------- #
# Rendering                                                                    #
# --------------------------------------------------------------------------- #


def _fmt_day(ts: Optional[int]) -> str:
    if not ts:
        return "?"
    return time.strftime("%Y-%m-%d", time.localtime(int(ts)))


def render_digest(digest: dict, *, fmt: str = "text") -> str:
    """Render a digest as ``text`` (markdown) or ``json``."""
    if fmt == "json":
        return json.dumps(digest, ensure_ascii=False, indent=2)

    win = digest.get("window", {})
    stats = digest.get("stats", {})
    lines: list[str] = []
    lines.append("# Memory Digest — Entscheidungsauszug")
    lines.append(
        f"_Fenster: {_fmt_day(win.get('from_ts'))} → {_fmt_day(win.get('to_ts'))}"
        f" · {stats.get('runs_with_decisions', 0)}/{stats.get('runs_in_window', 0)} Runs mit Entscheidungen"
        + (f" · Profil: {digest['profile']}" if digest.get("profile") else "")
        + "_"
    )
    lines.append("")

    decisions = digest.get("decisions") or []
    lines.append(f"## Entscheidungen ({len(decisions)})")
    if not decisions:
        lines.append("_No decisions / keine Entscheidungen im Fenster._")
    else:
        for d in decisions:
            scope = f" [{d['scope']}]" if d.get("scope") else ""
            sup = ""
            if d.get("supersedes"):
                sup = f" (ersetzt {', '.join(d['supersedes'])})"
            src = d.get("task_id") or "?"
            title = d.get("title")
            src_label = f"{src}" + (f" · {title}" if title else "")
            lines.append(f"- {d['text']}{scope}{sup}  \n  ↳ {src_label} · {_fmt_day(d.get('ended_at'))}")
    lines.append("")

    followups = digest.get("operator_followups") or []
    lines.append(f"## Offene Operator-Follow-ups ({len(followups)})")
    if not followups:
        lines.append("_Keine._")
    else:
        for f in followups:
            src = f.get("task_id") or "?"
            lines.append(f"- {f['text']}  \n  ↳ {src} · {_fmt_day(f.get('ended_at'))}")
    lines.append("")

    superseded = digest.get("superseded") or []
    if superseded:
        lines.append(f"## Ersetzt / superseded ({len(superseded)})")
        for s in superseded:
            lines.append(f"- {s['ref']} ← {s.get('by_task') or '?'} · {_fmt_day(s.get('ended_at'))}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Data loading + CLI                                                           #
# --------------------------------------------------------------------------- #


def load_completed_runs(conn, *, since_ts: int) -> list[dict]:
    """Load completed runs ended at/after ``since_ts`` joined with task info.

    Read-only. Returns dicts shaped for :func:`build_digest`.
    """
    rows = conn.execute(
        """
        SELECT r.task_id   AS task_id,
               r.profile   AS profile,
               r.ended_at  AS ended_at,
               r.summary   AS summary,
               r.metadata  AS metadata,
               t.title     AS title,
               t.tenant    AS tenant
          FROM task_runs r
          LEFT JOIN tasks t ON t.id = r.task_id
         WHERE r.outcome = 'completed'
           AND r.ended_at IS NOT NULL
           AND r.ended_at >= ?
         ORDER BY r.ended_at DESC
        """,
        (int(since_ts),),
    ).fetchall()

    out: list[dict] = []
    for row in rows:
        raw = row["metadata"]
        try:
            meta = json.loads(raw) if raw else {}
        except Exception:
            meta = {}
        out.append({
            "task_id": row["task_id"],
            "profile": row["profile"],
            "ended_at": int(row["ended_at"]) if row["ended_at"] is not None else None,
            "summary": row["summary"],
            "metadata": meta,
            "title": row["title"],
            "tenant": row["tenant"],
        })
    return out


def cmd_memory_digest(args) -> int:
    """Handler for ``hermes memory digest``."""
    from hermes_cli import kanban_db as kb

    spec = getattr(args, "since", None) or "7d"
    try:
        since_seconds = parse_since(spec)
    except ValueError as exc:
        print(f"hermes memory digest: {exc}")
        return 2

    fmt = getattr(args, "json", False) and "json" or "text"
    profile = getattr(args, "profile", None)
    limit = getattr(args, "limit", None)
    board = getattr(args, "board", None)

    now = int(time.time())
    since_ts = now - since_seconds
    conn = kb.connect(board=board)
    try:
        runs = load_completed_runs(conn, since_ts=since_ts)
    finally:
        conn.close()

    digest = build_digest(
        runs, now=now, since_seconds=since_seconds, profile=profile, limit=limit
    )
    print(render_digest(digest, fmt=fmt))
    return 0
