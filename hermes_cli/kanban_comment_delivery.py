"""Fork-owned delivery metadata for newly written Kanban comments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class CommentDelivery:
    """What a newly stored comment can affect in the current worker cycle."""

    comment_id: int
    reaches_current_worker: bool
    effective_from: str
    worker_is_live: bool
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "comment_id": self.comment_id,
            "reaches_current_worker": self.reaches_current_worker,
            "effective_from": self.effective_from,
            "worker_is_live": self.worker_is_live,
            "message": self.message,
        }


def build_comment_delivery(
    conn: Any,
    task_id: str,
    comment_id: int,
    now: int,
    pid_is_alive: Callable[[Any], bool],
) -> CommentDelivery:
    """Describe delivery without treating a stale ``running`` state as live."""
    row = conn.execute(
        "SELECT claim_lock, claim_expires, worker_pid FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown task {task_id}")
    try:
        claim_expires = int(row["claim_expires"] or 0)
    except (TypeError, ValueError):
        claim_expires = 0
    valid_claim = bool(row["claim_lock"]) and claim_expires > now
    worker_is_live = bool(
        valid_claim and row["worker_pid"] and pid_is_alive(row["worker_pid"])
    )
    if worker_is_live:
        message = (
            "Gespeichert. Der aktuelle Worker-Brief ist bereits erstellt; "
            "der Text gilt ab dem nächsten Worker-Brief."
        )
    else:
        message = "Gespeichert. Der Text gilt ab dem nächsten Worker-Brief."
    return CommentDelivery(
        comment_id=comment_id,
        reaches_current_worker=False,
        effective_from="next_worker_brief",
        worker_is_live=worker_is_live,
        message=message,
    )
