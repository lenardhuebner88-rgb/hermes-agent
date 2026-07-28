"""24h-Outcome-Übersicht für den Fleet Worker-Tab (fork-owned extension).

``GET /runs/outcomes?hours=24&board=`` liefert die Outcome-Verteilung (counts
pro Outcome) plus eine kompakte Runs-Liste des Fensters — Datenquelle für die
Sektion „Letzte 24 Stunden" im Worker-Tab (Mockup V2, 2026-07-27). Read-only
über task_runs ⋈ tasks; das SQL lebt bewusst in diesem fork-owned Modul, nicht
in der upstream-owned kanban_db.py.
"""

from __future__ import annotations

# extension_runtime.load_api_extension injects the parent API context.

# Fehler-Preview in der kompakten Liste (die volle Meldung bleibt über
# GET /runs/{run_id} erreichbar).
_ERROR_PREVIEW_CHARS = 200

# T1: Die Verdict-Wahrheit ist die Spalte task_runs.verdict (geschrieben von
# _set_run_verdict); metadata['review_verdict'] ist der dokumentierte Fallback.
# metadata['verdict'] ist explizit KEIN Review-Urteil (kanban_db
# _extract_review_verdict) und darf hier nicht gelesen werden. Alles außerhalb
# des Vokabulars wird verworfen, statt falsche Chips zu zeigen.
_VERDICT_VOCABULARY = frozenset({"APPROVED", "REQUEST_CHANGES"})


def _clean_verdict(value):
    if isinstance(value, str):
        candidate = value.strip().upper()
        if candidate in _VERDICT_VOCABULARY:
            return candidate
    return None


@observability_routes.get("/runs/outcomes")
def get_runs_outcomes(
    hours: int = Query(24, ge=1, le=24 * 30),
    limit: int = Query(15, ge=1, le=50),
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Outcome counts + compact run list for the last ``hours`` hours.

    Runs zählen über ``started_at`` ins Fenster (laufende Runs inklusive —
    deren Outcome ist ``running``). ``verdict`` kommt aus der Spalte
    ``task_runs.verdict`` mit Fallback ``metadata.review_verdict`` (T1).
    Registered BEFORE ``/runs/{run_id}`` so the literal ``outcomes`` segment
    isn't captured as a run id by the path-param route.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        now = int(time.time())
        cutoff = now - hours * 3600
        rows = conn.execute(
            """
            SELECT r.id AS run_id, r.task_id, t.title AS task_title, r.profile,
                   r.outcome, r.status, r.started_at, r.ended_at,
                   r.input_tokens, r.output_tokens, r.cost_usd, r.error,
                   COALESCE(
                       r.verdict,
                       CASE WHEN json_valid(r.metadata)
                            THEN json_extract(r.metadata, '$.review_verdict') END
                   ) AS verdict
            FROM task_runs r
            LEFT JOIN tasks t ON t.id = r.task_id
            WHERE r.started_at >= ?
            ORDER BY r.started_at DESC, r.id DESC
            """,
            (cutoff,),
        ).fetchall()

        outcomes: dict[str, int] = {}
        for row in rows:
            outcome = row["outcome"] or (
                "running" if row["status"] == "running" else (row["status"] or "unknown")
            )
            outcomes[outcome] = outcomes.get(outcome, 0) + 1

        runs = []
        for row in rows[:limit]:
            error = row["error"]
            if error and len(error) > _ERROR_PREVIEW_CHARS:
                error = error[:_ERROR_PREVIEW_CHARS] + "…"
            runs.append(
                {
                    "run_id": int(row["run_id"]),
                    "task_id": row["task_id"],
                    "task_title": row["task_title"] or row["task_id"],
                    "profile": row["profile"],
                    "outcome": row["outcome"] or (
                        "running" if row["status"] == "running" else (row["status"] or "unknown")
                    ),
                    "verdict": _clean_verdict(row["verdict"]),
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"],
                    "cost_usd": row["cost_usd"],
                    "error": error,
                }
            )

        return {
            "hours": hours,
            "now": now,
            "total": len(rows),
            "outcomes": outcomes,
            "runs": runs,
        }
    finally:
        conn.close()


__all__ = ("get_runs_outcomes",)
