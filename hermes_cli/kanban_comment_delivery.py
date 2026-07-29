"""Fork-owned delivery metadata for newly written Kanban comments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class CommentDelivery:
    """What a newly stored comment can affect in the current worker cycle."""

    comment_id: int
    kind: str
    reaches_current_worker: bool
    effective_from: str
    worker_is_live: bool
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "comment_id": self.comment_id,
            "kind": self.kind,
            "reaches_current_worker": self.reaches_current_worker,
            "effective_from": self.effective_from,
            "worker_is_live": self.worker_is_live,
            "message": self.message,
        }


def build_comment_delivery(
    conn: Any,
    task_id: str,
    comment_id: int,
    kind: str,
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
            f"{'Direktive' if kind == 'directive' else 'Kommentar'} gespeichert. "
            "Der aktuelle Worker-Brief ist bereits erstellt; der Text gilt ab dem "
            "nächsten Worker-Brief."
        )
    else:
        message = (
            f"{'Direktive' if kind == 'directive' else 'Kommentar'} gespeichert; "
            "kein lebender Worker kann sie empfangen. Der Text gilt ab dem nächsten "
            "Worker-Brief."
        )
    return CommentDelivery(
        comment_id=comment_id,
        kind=kind,
        reaches_current_worker=False,
        effective_from="next_worker_brief",
        worker_is_live=worker_is_live,
        message=message,
    )


def write_comment(
    conn: Any,
    task_id: str,
    author: str,
    body: str,
    *,
    kind: str = "comment",
) -> CommentDelivery:
    """Shared operator write path: store a comment and report its delivery.

    The CLI (``_cmd_comment``) and both dashboard writers (the comment endpoint
    and the operator nudge action) funnel their writes through here, so a comment
    that can no longer reach the running worker is flagged wherever it is written
    — not only in the CLI, where the loss is least likely to happen.

    Persistence stays in :func:`kanban_db.add_comment`, which keeps returning the
    bare comment id for every other caller; only this operator-facing wrapper
    turns that id into a :class:`CommentDelivery`. The comment is stored in every
    case — the returned metadata reports *when* the text takes effect (the next
    worker brief) and never changes or aborts the write.
    """
    import time

    from hermes_cli import kanban_db as _kb

    comment_id = int(_kb.add_comment(conn, task_id, author, body, kind=kind))
    now = int(time.time())
    return build_comment_delivery(
        conn,
        task_id,
        comment_id,
        kind,
        now,
        _kb._pid_alive,
    )
