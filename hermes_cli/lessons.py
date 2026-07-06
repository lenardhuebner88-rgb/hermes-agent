"""Deterministic lessons harvester — ``hermes lessons harvest``.

Clusters recurring trap classes from three read-only sources into a single
JSON candidate artefact at ``<state_dir>/lessons/harvest_candidates.json``:

1. ``disposition_items`` (status open/accepted, last ``window_days``) from the
   live ``kanban.db`` — opened read-only (``mode=ro``).
2. ``task_events`` with ``kind='blocked'`` (last ``window_days``) — blocked
   reasons from task runs.
3. Loop-pack ``LEDGER.md`` files under ``<hermes_home>/loops/`` — the same
   artefact the loop-tuner dedups against.

The harvest path makes **no LLM calls**. Clustering is keyword-signature
based: each candidate cluster carries a ``signature`` (normalised keyword
tuple), an ``evidence_count``, and the source IDs that contributed. The
output is consumed downstream by the promote path (L3) which turns clusters
with ``>=2`` evidence points into held docs/skill-edit Kanban tasks.

Idempotent: re-running overwrites the JSON atomically (temp file + rename).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_WINDOW_DAYS = 30
DEFAULT_OUTPUT_PATH = "state/lessons/harvest_candidates.json"
MIN_EVIDENCE_FOR_CANDIDATE = 2
MAX_SOURCE_IDS_PER_CLUSTER = 15

# Known trap-class keyword signatures.  Each entry maps a cluster label to a
# list of keyword patterns (lowercased substrings). If *any* of these keywords
# appear in the item's evidence/next_action/disposition/reason, the item is
# attributed to that cluster. A single item can contribute to multiple
# clusters (overlapping trap classes are real).
TRAP_SIGNATURES: list[tuple[str, list[str]]] = [
    (
        "release-gate/born-blocked-holds",
        [
            "awaiting release-gate go",
            "awaiting release-gate",
            "green_code_not_runtime_activated",
            "born blocked",
            "initial_status=blocked",
            "release-gate go",
            "post-rollout",
            "remerge",
        ],
    ),
    (
        "artifact-policy-traps",
        [
            "artifact_policy_missing",
            "artifact policy",
            "artifact preserve",
            "preserve prefix",
        ],
    ),
    (
        "waitfor/vitest-under-load-flakes",
        [
            "asyncutiltimeout",
            "waitfor",
            "vitest[control]",
            "vitest flake",
            "vitest under load",
            "configure({asynctimeout",
        ],
    ),
    (
        "dirty-worktree/parallel-session-overlap",
        [
            "dirty_worktree",
            "dirty worktree",
            "dirty files",
            "parallel session",
            "parallel-session",
            "coordination overlap",
            "overlap",
        ],
    ),
    (
        "auto-decompose/token-cap-loops",
        [
            "auto_decompose",
            "auto-decompose",
            "auto decompose",
            "token cap exceeded",
            "input-token cap",
            "per-task input-token cap",
            "iteration budget exhausted",
            "loop 0",
        ],
    ),
    (
        "review-ac-semantic-gaps",
        [
            "needs_revision",
            "needs revision",
            "request_changes",
            "request changes",
            "negativtest fehlt",
            "negativ-test",
            "docstring drift",
            "scope-only",
            "scope_only",
        ],
    ),
]

# Normalised signature keys derived from TRAP_SIGNATURES — used for stable
# cluster ids.
_ALL_SIGNATURES = [label for label, _ in TRAP_SIGNATURES]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ClusterCandidate:
    """A clustered trap candidate with accumulated evidence."""

    cluster: str
    signature: tuple[str, ...]
    evidence_count: int = 0
    source_ids: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    evidence_samples: list[dict[str, Any]] = field(default_factory=list)

    def add_evidence(
        self,
        source_id: str,
        source_type: str,
        sample: dict[str, Any],
    ) -> None:
        self.evidence_count += 1
        if source_id not in self.source_ids:
            self.source_ids.append(source_id)
        if source_type not in self.source_types:
            self.source_types.append(source_type)
        if len(self.evidence_samples) < 3:
            self.evidence_samples.append(sample)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster": self.cluster,
            "signature": list(self.signature),
            "evidence_point_count": self.evidence_count,
            "distinct_source_ids": len(self.source_ids),
            "source_ids": self.source_ids[:MAX_SOURCE_IDS_PER_CLUSTER],
            "source_types": self.source_types,
            "evidence_samples": self.evidence_samples,
            "meets_threshold": self.evidence_count >= MIN_EVIDENCE_FOR_CANDIDATE,
        }


# ---------------------------------------------------------------------------
# Signature matching
# ---------------------------------------------------------------------------


def _match_signatures(text: str) -> list[str]:
    """Return the list of cluster labels whose keywords appear in *text*."""
    if not text:
        return []
    lowered = text.lower()
    hits: list[str] = []
    for label, keywords in TRAP_SIGNATURES:
        for kw in keywords:
            if kw in lowered:
                hits.append(label)
                break  # one hit per cluster is enough
    return hits


def _signature_for_cluster(cluster: str) -> tuple[str, ...]:
    """Normalised keyword tuple for a cluster label (stable id component)."""
    for label, keywords in TRAP_SIGNATURES:
        if label == cluster:
            return tuple(keywords)
    return ()


# ---------------------------------------------------------------------------
# Source harvesters
# ---------------------------------------------------------------------------


def _days_ago_ts(days: int, *, now: Optional[int] = None) -> int:
    """Return the unix timestamp *days* ago."""
    base = now if now is not None else int(datetime.now(timezone.utc).timestamp())
    return base - days * 86400


def harvest_disposition_items(
    conn: sqlite3.Connection,
    *,
    window_days: int,
    now_ts: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Harvest open/accepted disposition_items as raw source rows.

    Reads via an already-open (read-only) connection. Returns a list of dicts
    with normalised fields: ``id``, ``source_task_id``, ``typ``,
    ``disposition``, ``status``, ``evidence``, ``next_action``, ``created_at``.
    """
    since = _days_ago_ts(window_days, now=now_ts)
    rows = conn.execute(
        "SELECT id, source_task_id, typ, disposition, status, "
        "severity, evidence, next_action, created_at "
        "FROM disposition_items "
        "WHERE status IN ('open', 'accepted') AND "
        "(created_at IS NULL OR created_at >= ?) "
        "ORDER BY created_at DESC",
        (since,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "source_type": "disposition_item",
                "source_id": r["id"],
                "task_id": r["source_task_id"],
                "typ": r["typ"],
                "disposition": r["disposition"],
                "status": r["status"],
                "severity": r["severity"],
                "evidence": r["evidence"] or "",
                "next_action": r["next_action"] or "",
                "created_at": int(r["created_at"]) if r["created_at"] else None,
            }
        )
    return out


def harvest_blocked_events(
    conn: sqlite3.Connection,
    *,
    window_days: int,
    now_ts: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Harvest blocked task_events (kind='blocked') as raw source rows.

    The blocked event ``payload`` is a JSON string containing a ``reason``
    field. We extract the human-readable reason for signature matching.
    """
    since = _days_ago_ts(window_days, now=now_ts)
    rows = conn.execute(
        "SELECT id, task_id, run_id, payload, created_at "
        "FROM task_events "
        "WHERE kind = 'blocked' AND created_at >= ? "
        "ORDER BY created_at DESC",
        (since,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        reason = ""
        reason_payload_raw = None
        payload_str = r["payload"] or ""
        if payload_str:
            try:
                payload_obj = json.loads(payload_str)
                if isinstance(payload_obj, dict):
                    reason = str(payload_obj.get("reason") or payload_obj.get("reason_for_lane") or "")
                    reason_payload_raw = payload_obj
            except (json.JSONDecodeError, TypeError):
                pass
        out.append(
            {
                "source_type": "blocked_event",
                "source_id": f"te_{r['id']}",
                "task_id": r["task_id"],
                "run_id": r["run_id"],
                "reason": reason,
                "payload": reason_payload_raw,
                "created_at": int(r["created_at"]) if r["created_at"] else None,
            }
        )
    return out


_LEDG_KV_RE = re.compile(r"^\s*[-*]\s*(\d{4}-\d{2}-\d{2})\b.*$", re.MULTILINE)


def harvest_loop_ledgers(
    loops_root: Path,
) -> list[dict[str, Any]]:
    """Scan loop-pack LEDGER.md files for tuning entries.

    Each LEDGER.md entry is a markdown bullet line with a date prefix like
    ``- 2026-07-05: Fixed ...``. We extract these as evidence sources.
    """
    out: list[dict[str, Any]] = []
    if not loops_root.is_dir():
        return out
    for ledger_path in sorted(loops_root.glob("*/LEDGER.md")):
        pack_name = ledger_path.parent.name
        try:
            content = ledger_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Extract dated bullet entries as individual evidence sources
        for match in _LEDG_KV_RE.finditer(content):
            line = match.group(0).strip()
            date_str = match.group(1)
            out.append(
                {
                    "source_type": "loop_ledger",
                    "source_id": f"ledger/{pack_name}/{date_str}",
                    "pack_name": pack_name,
                    "entry": line,
                    "date": date_str,
                }
            )
        if not content.strip():
            continue
    return out


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def _text_from_disposition(row: dict[str, Any]) -> str:
    return " ".join(
        filter(
            None,
            [
                row.get("evidence") or "",
                row.get("next_action") or "",
                row.get("disposition") or "",
                row.get("typ") or "",
            ],
        )
    )


def _text_from_blocked(row: dict[str, Any]) -> str:
    return row.get("reason") or ""


def _text_from_ledger(row: dict[str, Any]) -> str:
    return row.get("entry") or ""


def cluster_candidates(
    disposition_rows: list[dict[str, Any]],
    blocked_rows: list[dict[str, Any]],
    ledger_rows: list[dict[str, Any]],
) -> list[ClusterCandidate]:
    """Cluster raw source rows into trap-class candidates.

    Each row is matched against TRAP_SIGNATURES. Rows that match no known
    signature are accumulated into an ``unclustered`` bucket so no evidence is
    lost (they just won't meet promote threshold).
    """
    clusters: dict[str, ClusterCandidate] = {}
    unclustered = ClusterCandidate(
        cluster="unclustered",
        signature=(),
    )

    def _ensure(label: str) -> ClusterCandidate:
        if label not in clusters:
            clusters[label] = ClusterCandidate(
                cluster=label,
                signature=_signature_for_cluster(label),
            )
        return clusters[label]

    for row in disposition_rows:
        text = _text_from_disposition(row)
        hits = _match_signatures(text)
        if not hits:
            unclustered.add_evidence(
                row["source_id"], "disposition_item", {"evidence": row.get("evidence", "")[:200]}
            )
            continue
        for label in hits:
            c = _ensure(label)
            c.add_evidence(
                row["source_id"],
                "disposition_item",
                {
                    "evidence": (row.get("evidence") or "")[:200],
                    "next_action": (row.get("next_action") or "")[:200],
                    "source_task_id": row.get("task_id"),
                },
            )

    for row in blocked_rows:
        text = _text_from_blocked(row)
        hits = _match_signatures(text)
        if not hits:
            unclustered.add_evidence(
                row["source_id"], "blocked_event", {"reason": (row.get("reason") or "")[:200]}
            )
            continue
        for label in hits:
            c = _ensure(label)
            c.add_evidence(
                row["source_id"],
                "blocked_event",
                {
                    "reason": (row.get("reason") or "")[:200],
                    "task_id": row.get("task_id"),
                },
            )

    for row in ledger_rows:
        text = _text_from_ledger(row)
        hits = _match_signatures(text)
        if not hits:
            unclustered.add_evidence(
                row["source_id"], "loop_ledger", {"entry": (row.get("entry") or "")[:200]}
            )
            continue
        for label in hits:
            c = _ensure(label)
            c.add_evidence(
                row["source_id"],
                "loop_ledger",
                {
                    "entry": (row.get("entry") or "")[:200],
                    "pack_name": row.get("pack_name"),
                },
            )

    all_clusters = sorted(clusters.values(), key=lambda c: -c.evidence_count)
    if unclustered.evidence_count > 0:
        all_clusters.append(unclustered)
    return all_clusters


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def default_output_path() -> Path:
    return get_hermes_home() / DEFAULT_OUTPUT_PATH


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open a read-only SQLite connection (mode=ro, uri=True).

    Raises FileNotFoundError if db_path doesn't exist; callers should check
    existence first and emit an empty-array source instead of crashing.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"kanban DB not found: {db_path}")
    path = str(db_path)
    if not path.startswith("/"):
        path = str(Path(path).resolve())
    conn = sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
        timeout=2.0,
    )
    conn.row_factory = sqlite3.Row
    return conn


def run_harvest(
    *,
    kanban_db_path: Optional[Path] = None,
    loops_root: Optional[Path] = None,
    output_path: Optional[Path] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now_ts: Optional[int] = None,
) -> dict[str, Any]:
    """Run the deterministic lessons harvest and write the JSON artefact.

    Returns a summary dict with ``output_path``, ``source_counts``, and
    ``candidate_count``. Does not make LLM calls. Idempotent.
    """
    from hermes_cli.kanban_db import kanban_db_path as resolve_db_path

    db_path = kanban_db_path if kanban_db_path is not None else resolve_db_path()
    out_path = output_path if output_path is not None else default_output_path()
    loops_dir = loops_root if loops_root is not None else (get_hermes_home() / "loops")

    disposition_rows: list[dict[str, Any]] = []
    blocked_rows: list[dict[str, Any]] = []
    source_status: dict[str, str] = {}

    # Source 1+2: kanban DB (read-only)
    try:
        conn = _connect_readonly(db_path)
    except FileNotFoundError as exc:
        logger.warning("Lessons harvest: %s — DB source skipped", exc)
        source_status["kanban_db"] = "missing"
    else:
        try:
            disposition_rows = harvest_disposition_items(conn, window_days=window_days, now_ts=now_ts)
            blocked_rows = harvest_blocked_events(conn, window_days=window_days, now_ts=now_ts)
            source_status["kanban_db"] = "ok"
        except sqlite3.DatabaseError as exc:
            logger.warning("Lessons harvest: DB read failed: %s", exc)
            source_status["kanban_db"] = f"error: {exc}"
        finally:
            conn.close()

    # Source 3: loop LEDGER.md files
    ledger_rows = harvest_loop_ledgers(loops_dir)
    source_status["loop_ledgers"] = "ok" if ledger_rows else "empty" if loops_dir.is_dir() else "missing"

    # Cluster
    candidates = cluster_candidates(disposition_rows, blocked_rows, ledger_rows)

    # Build output
    summary = {
        "harvested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_days": window_days,
        "sources": {
            "disposition_items": len(disposition_rows),
            "blocked_events": len(blocked_rows),
            "loop_ledger_entries": len(ledger_rows),
        },
        "source_status": source_status,
        "min_evidence_for_candidate": MIN_EVIDENCE_FOR_CANDIDATE,
        "candidates": [c.to_dict() for c in candidates],
    }
    # Overall candidate count = clusters meeting the threshold (not unclustered)
    meeting = sum(1 for c in candidates if c.cluster != "unclustered" and c.evidence_count >= MIN_EVIDENCE_FOR_CANDIDATE)
    summary["candidate_count"] = meeting

    # Atomic write
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)

    logger.info(
        "Lessons harvest: %d disposition + %d blocked + %d ledger → %d candidates → %s",
        len(disposition_rows),
        len(blocked_rows),
        len(ledger_rows),
        meeting,
        out_path,
    )

    return {
        "output_path": str(out_path),
        "source_counts": summary["sources"],
        "candidate_count": meeting,
        "total_clusters": len(candidates),
    }


# ---------------------------------------------------------------------------
# Promote — turn harvested candidates into held docs-edit Kanban tasks (L3)
# ---------------------------------------------------------------------------

DEFAULT_PROMOTE_CAP = 5
"""Maximum number of docs-edit tasks to create per promote run."""

DOCS_DEDUP_FILES = [
    "AGENTS.md",
    "docs/agent-dev-guide.md",
]


def _load_cluster_docs(repo_dir: Path) -> str:
    """Read the combined text of the pitfall-bearing docs for dedup.

    Reads AGENTS.md (Important Pitfalls section) and docs/agent-dev-guide.md
    from ``repo_dir``. Missing files are silently skipped — dedup just
    won't match against them.
    """
    chunks: list[str] = []
    for rel in DOCS_DEDUP_FILES:
        candidate = repo_dir / rel
        try:
            if candidate.is_file():
                chunks.append(candidate.read_text(encoding="utf-8"))
        except OSError:
            logger.warning("promote: could not read %s for dedup", candidate)
    return "\n".join(chunks)


def _slug_from_cluster(cluster: str) -> str:
    """Turn a cluster label like ``release-gate/born-blocked-holds`` into a
    filesystem-safe slug ``release-gate-born-blocked-holds``."""
    return re.sub(r"[^a-z0-9-]+", "-", cluster.lower()).strip("-")


def _is_cluster_documented(cluster: str, signature: tuple[str, ...], docs_text: str) -> bool:
    """Return True if any signature keyword already appears in the docs.

    The cluster label is slugified and also checked, so a candidate whose
    label is already mentioned in prose is considered documented.
    """
    if not docs_text:
        return False
    slug = _slug_from_cluster(cluster)
    if slug and slug in docs_text.lower():
        return True
    lower_docs = docs_text.lower()
    for kw in signature:
        if kw and kw.lower() in lower_docs:
            return True
    return False


def _build_task_body(candidate: dict[str, Any], cluster_slug: str) -> str:
    """Compose the docs-edit task brief for a promoted candidate."""
    cluster = candidate.get("cluster", cluster_slug)
    evidence_count = candidate.get("evidence_point_count", 0)
    source_ids = candidate.get("source_ids", [])
    source_types = candidate.get("source_types", [])
    samples = candidate.get("evidence_samples", [])

    lines = [
        f"## Lessons-to-Docs Promote (auto-generated)",
        "",
        f"Recurring trap-class cluster **{cluster}** reached {evidence_count} "
        f"evidence points (>= {MIN_EVIDENCE_FOR_CANDIDATE}) in the latest harvest.",
        "",
        "### Target",
        f"Add a one-line pitfall entry to **AGENTS.md** (Important Pitfalls section) "
        f"or the affected **SKILL.md**, depending on where the trap surfaces.",
        "Do NOT commit directly — this task is a held docs-edit request awaiting review.",
        "",
        "### Evidence (from harvest_candidates.json)",
        f"- Source types: {', '.join(sorted(set(source_types))) or 'n/a'}",
        f"- Source IDs ({len(source_ids)}): {', '.join(source_ids[:10])}",
    ]
    if samples:
        lines.append("")
        lines.append("### Evidence samples (max 3)")
        for i, s in enumerate(samples, 1):
            text = str(s).strip()
            # Truncate long samples to keep the task body compact.
            if len(text) > 500:
                text = text[:497] + "..."
            lines.append(f"{i}. {text}")
    lines.append("")
    lines.append("### Acceptance")
    lines.append("- Pitfall is documented in AGENTS.md or the relevant SKILL.md.")
    lines.append("- One sentence capturing the trap and the mitigation.")
    lines.append("- No speculative content — only what the evidence supports.")
    return "\n".join(lines)


def run_promote(
    harvest_path: Optional[Path] = None,
    repo_dir: Optional[Path] = None,
    cap: int = DEFAULT_PROMOTE_CAP,
    dry_run: bool = False,
    board: Optional[str] = None,
) -> dict[str, Any]:
    """Promote harvest candidates to held docs-edit Kanban tasks.

    Reads ``harvest_candidates.json``, filters clusters with
    ``meets_threshold=True`` (>= 2 evidence points), deduplicates against
    existing pitfalls in AGENTS.md and docs/agent-dev-guide.md, and creates
    **held** Kanban tasks (``initial_status='blocked'``) for the top-N
    candidates. The assignee is ``coder`` — these are docs-edit requests,
    not direct commits.

    Idempotent via ``idempotency_key='lessons:<slug>'`` — re-running with
    the same harvest artefact produces no duplicate tasks.

    Returns a summary dict with counts of promoted, skipped (documented),
    and capped.
    """
    from hermes_cli import kanban_db

    if harvest_path is None:
        hp: Path = get_hermes_home() / DEFAULT_OUTPUT_PATH
    else:
        hp = harvest_path
    if not hp.is_file():
        raise FileNotFoundError(
            f"Harvest artefact not found: {hp}. "
            "Run 'hermes lessons harvest' first."
        )

    data = json.loads(hp.read_text(encoding="utf-8"))
    raw_candidates = data.get("candidates", [])

    # Determine repo_dir for dedup — fall back to the worktree's repo root.
    if repo_dir is None:
        repo_dir = Path(__file__).resolve().parent.parent

    docs_text = _load_cluster_docs(repo_dir)

    eligible: list[dict[str, Any]] = []
    skipped_documented: list[str] = []

    for cand in raw_candidates:
        if not cand.get("meets_threshold", False):
            continue
        cluster = cand.get("cluster", "")
        if not cluster or cluster == "unclustered":
            continue
        signature = tuple(cand.get("signature", []))
        if _is_cluster_documented(cluster, signature, docs_text):
            skipped_documented.append(cluster)
            continue
        eligible.append(cand)

    # Sort by evidence count descending, then alphabetically for stability.
    eligible.sort(
        key=lambda c: (-c.get("evidence_point_count", 0), c.get("cluster", ""))
    )

    capped = max(0, len(eligible) - cap)
    to_promote = eligible[:cap]

    created: list[dict[str, Any]] = []
    skipped_existing: list[str] = []

    for cand in to_promote:
        cluster = cand.get("cluster", "")
        slug = _slug_from_cluster(cluster)
        title = f"Docs-Edit: document recurring trap-class '{cluster}' as pitfall"
        body = _build_task_body(cand, slug)
        idem = f"lessons:{slug}"

        if dry_run:
            created.append({"title": title, "idempotency_key": idem, "dry_run": True})
            continue

        with kanban_db.connect_closing(board=board) as conn:
            created_id = kanban_db.create_task(
                conn,
                title=title,
                body=body,
                assignee="coder",
                created_by="lessons-promote",
                workspace_kind="dir",
                idempotency_key=idem,
                initial_status="blocked",
                kind="code",
                tenant="planspec",
            )
        created.append({"task_id": created_id, "cluster": cluster, "title": title})

    summary = {
        "promoted": len(created),
        "skipped_documented": len(skipped_documented),
        "documented_clusters": skipped_documented,
        "capped": capped,
        "created": created,
    }
    logger.info(
        "Lessons promote: %d promoted, %d already documented, %d capped",
        len(created),
        len(skipped_documented),
        capped,
    )
    return summary
