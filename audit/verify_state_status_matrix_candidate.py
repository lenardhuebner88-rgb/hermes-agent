#!/usr/bin/env python3
"""Materialise and prove Fleet task/run state truth on audit-scratch."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright


BASE = os.environ.get("KANBAN_CANDIDATE_BASE", "http://127.0.0.1:9123")
OUT = Path("audit/iteration-4-state")
TASK_LABELS = {
    "triage": "Triage",
    "todo": "Offen",
    "scheduled": "Geplant",
    "ready": "Startklar",
    "running": "Läuft",
    "blocked": "Blockiert",
    "review": "In Prüfung",
    "done": "Fertig",
    "archived": "Archiv",
}
ACTIVE_ORDER = [
    "triage",
    "todo",
    "scheduled",
    "ready",
    "running",
    "blocked",
    "review",
    "done",
]
EXPECTED_ACTIONS = {
    "triage": ["Profil ändern", "Plan", "Abbrechen"],
    "todo": ["Profil ändern", "Abbrechen"],
    "scheduled": ["Profil ändern", "Starten", "Abbrechen"],
    "ready": ["Profil ändern", "Abbrechen"],
    "running": [],
    "blocked": ["Profil ändern", "Reopen", "Wiederholen", "Abbrechen"],
    "review": ["Profil ändern", "Abbrechen"],
    "done": [],
    "archived": [],
}
ACTION_LABELS = {
    "Profil ändern",
    "Plan",
    "Starten",
    "Reopen",
    "Wiederholen",
    "Abbrechen",
    "Kette abbrechen",
}
_FIXTURE_CONN: Any = None
_FIXTURE_IDS: list[str] = []


def _task_title(conn: Any, task_id: str) -> str:
    row = conn.execute("SELECT title FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"task vanished during fixture creation: {task_id}")
    return str(row["title"])


def _claim(conn: Any, task_id: str, *, default_claimer: bool = False) -> Any:
    from hermes_cli import kanban_db as kb

    claimed = kb.claim_task(
        conn,
        task_id,
        **({} if default_claimer else {"claimer": "codex-audit"}),
    )
    if not claimed:
        task = kb.get_task(conn, task_id)
        raise RuntimeError(
            f"could not claim {task_id}: {task.status if task else 'missing'}"
        )
    run = kb.latest_run(conn, task_id)
    if run is None:
        raise RuntimeError(f"claim created no run for {task_id}")
    return run


def create_fixtures() -> tuple[dict[str, str], dict[str, str], list[str], Any, str]:
    """Use production lifecycle primitives; return task/run representatives."""
    global _FIXTURE_CONN, _FIXTURE_IDS

    os.environ["HERMES_KANBAN_BOARD"] = "audit-scratch"
    os.environ["HERMES_KANBAN_CRASH_GRACE_SECONDS"] = "0"
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    suffix = str(time.time_ns())
    all_ids: list[str] = []
    _FIXTURE_CONN = conn
    _FIXTURE_IDS = all_ids

    def create(kind: str, *, prefix: str = "STATUS", **kwargs: Any) -> str:
        task_id = kb.create_task(
            conn,
            title=f"AUDIT {prefix} MATRIX {suffix} {kind}",
            body=f"Loop-4 sanctioned fixture for {kind} truth.",
            created_by="codex-audit",
            idempotency_key=f"codex-audit-{prefix.lower()}-{suffix}-{kind}",
            **kwargs,
        )
        all_ids.append(task_id)
        return task_id

    # Nine task states. The todo task has a real unsatisfied predecessor so
    # its missing Starten action can be checked against authoritative links.
    helper = create("todo-parent", prefix="HELPER", initial_status="blocked")
    states: dict[str, str] = {}
    states["triage"] = create("triage", triage=True)
    states["todo"] = create("todo", parents=[helper])
    states["scheduled"] = create("scheduled")
    if not kb.schedule_task(conn, states["scheduled"], reason="audit schedule"):
        raise RuntimeError("scheduled fixture transition failed")
    states["ready"] = create("ready")
    states["running"] = create("running", assignee="audit-deleted-lane")
    _claim(conn, states["running"])
    kb._set_worker_pid(conn, states["running"], os.getpid())
    states["blocked"] = create("blocked")
    _claim(conn, states["blocked"])
    if not kb.block_task(conn, states["blocked"], reason="audit transient block"):
        raise RuntimeError("blocked fixture transition failed")
    states["review"] = create(
        "review",
        assignee="coder",
        acceptance_criteria="- AC-1: Status matrix is truthful",
    )
    review_run = _claim(conn, states["review"])
    if not kb.complete_task(
        conn,
        states["review"],
        summary="Candidate submitted for independent review",
        metadata={"tests_run": ["status matrix"]},
        expected_run_id=review_run.id,
        review_gate=True,
    ):
        raise RuntimeError("review fixture transition failed")
    states["done"] = create("done")
    done_run = _claim(conn, states["done"])
    if not kb.complete_task(
        conn,
        states["done"],
        summary="Audit task completed",
        expected_run_id=done_run.id,
        review_gate=False,
    ):
        raise RuntimeError("done fixture transition failed")
    states["archived"] = create("archived")
    if not kb.archive_task(conn, states["archived"]):
        raise RuntimeError("archived fixture transition failed")

    # Current production run vocabulary. `failed` and `released` remain legacy
    # TypeScript hints: no current lifecycle primitive writes them and the live
    # default-board census in N-43 contains neither.
    runs: dict[str, str] = {
        "running": states["running"],
        "done": states["done"],
        "blocked": states["blocked"],
        "scheduled": states["scheduled"],
    }

    # A claimed completion deliberately records run status ``done`` with
    # semantic outcome ``completed``.  The distinct persisted run status
    # ``completed`` is produced by the zero-duration/manual completion path.
    runs["completed"] = create("completed", prefix="RUN")
    if not kb.complete_task(
        conn,
        runs["completed"],
        summary="Audit zero-duration completion",
        review_gate=False,
    ):
        raise RuntimeError("completed run fixture transition failed")

    runs["spawn_failed"] = create("spawn-failed", prefix="RUN")
    _claim(conn, runs["spawn_failed"], default_claimer=True)
    kb._record_spawn_failure(
        conn,
        runs["spawn_failed"],
        "audit deterministic spawn failure",
        failure_limit=99,
    )

    runs["reclaimed"] = create("reclaimed", prefix="RUN")
    _claim(conn, runs["reclaimed"], default_claimer=True)
    if not kb.reclaim_task(
        conn,
        runs["reclaimed"],
        reason="audit operator reclaim",
        signal_fn=lambda *_args: None,
    ):
        raise RuntimeError("reclaimed run fixture transition failed")

    runs["timed_out"] = create(
        "timed-out",
        prefix="RUN",
        max_runtime_seconds=1,
        max_retries=99,
    )
    _claim(conn, runs["timed_out"], default_claimer=True)
    kb._set_worker_pid(conn, runs["timed_out"], 987_654_321)
    time.sleep(2.05)
    if runs["timed_out"] not in kb.enforce_max_runtime(
        conn, signal_fn=lambda *_args: None
    ):
        raise RuntimeError("timed-out run fixture transition failed")

    runs["transient_retry"] = create(
        "transient-retry", prefix="RUN", max_retries=99
    )
    _claim(conn, runs["transient_retry"], default_claimer=True)
    kb._set_worker_pid(conn, runs["transient_retry"], 987_654_322)
    kb.detect_crashed_workers(conn)

    runs["crashed"] = create("crashed", prefix="RUN", max_retries=99)
    for attempt in range(int(kb.TRANSIENT_RETRY_LIMIT) + 1):
        _claim(conn, runs["crashed"], default_claimer=True)
        kb._set_worker_pid(conn, runs["crashed"], 987_654_400 + attempt)
        kb.detect_crashed_workers(conn)

    runs["gave_up"] = create(
        "gave-up", prefix="RUN", max_retries=1
    )
    _claim(conn, runs["gave_up"], default_claimer=True)
    kb._record_spawn_failure(
        conn,
        runs["gave_up"],
        "audit breaker exhaustion",
        failure_limit=99,
    )

    for status, task_id in states.items():
        task = kb.get_task(conn, task_id)
        if task is None or task.status != status:
            raise RuntimeError(
                f"fixture {status} ended as {task.status if task else 'missing'}"
            )
    for status, task_id in runs.items():
        latest = kb.latest_run(conn, task_id)
        if latest is None or latest.status != status:
            raise RuntimeError(
                f"run fixture {status} ended as {latest.status if latest else 'missing'}"
            )
    return states, runs, all_ids, conn, suffix


def api_get(page: Page, path: str) -> dict[str, Any]:
    return page.evaluate(
        """async ({path}) => {
          const headers = {'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__};
          const response = await fetch(path, {headers});
          return {status: response.status, body: await response.json()};
        }""",
        {"path": path},
    )


def select_board(page: Page) -> None:
    page.get_by_role("button", name="Subtab Board", exact=True).click()
    page.get_by_label("Tasks durchsuchen").wait_for(timeout=20_000)


def open_active_task(page: Page, title: str, task_id: str) -> None:
    close = page.get_by_role("button", name="Schließen", exact=True)
    if close.count() and close.is_visible():
        close.click()
    page.get_by_label("Nach Status filtern").select_option("all")
    page.get_by_label("Tasks durchsuchen").fill(title)
    row = page.locator(".fleet-boardtab-title", has_text=title)
    row.wait_for(timeout=20_000)
    row.click()
    page.get_by_text(task_id, exact=True).wait_for(timeout=20_000)
    page.wait_for_timeout(1_000)


def visible_actions(page: Page) -> list[str]:
    labels = page.locator(".fleet-dr-actions button:visible").all_inner_texts()
    return [label.strip() for label in labels if label.strip() in ACTION_LABELS]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    states: dict[str, str] = {}
    runs: dict[str, str] = {}
    all_ids: list[str] = []
    conn = None
    console_errors: list[str] = []
    result: dict[str, Any] = {}
    try:
        states, runs, all_ids, conn, suffix = create_fixtures()
        state_titles = {status: _task_title(conn, task_id) for status, task_id in states.items()}
        run_titles = {status: _task_title(conn, task_id) for status, task_id in runs.items()}
        db_state_rows = {
            row["id"]: {"status": row["status"], "assignee": row["assignee"]}
            for row in conn.execute(
                "SELECT id, status, assignee FROM tasks WHERE id IN ("
                + ",".join("?" for _ in states)
                + ")",
                list(states.values()),
            ).fetchall()
        }
        db_run_rows = {
            status: [
                {"id": int(row["id"]), "status": row["status"], "outcome": row["outcome"]}
                for row in conn.execute(
                    "SELECT id, status, outcome FROM task_runs WHERE task_id = ? ORDER BY id",
                    (task_id,),
                ).fetchall()
            ]
            for status, task_id in runs.items()
        }

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text)
                if msg.type == "error"
                else None,
            )
            page.goto(f"{BASE}/control/fleet", wait_until="domcontentloaded", timeout=30_000)
            page.get_by_role("button", name="Subtab Board", exact=True).wait_for(timeout=30_000)

            board_api = api_get(page, "/api/plugins/kanban/board")
            details_api = {
                status: api_get(page, f"/api/plugins/kanban/tasks/{task_id}")
                for status, task_id in {**states, **runs}.items()
            }

            select_board(page)
            page.get_by_label("Tasks durchsuchen").fill(f"AUDIT STATUS MATRIX {suffix}")
            page.get_by_text(state_titles["triage"], exact=True).wait_for(timeout=20_000)
            status_groups = page.locator(".fleet-boardtab-group").evaluate_all(
                """sections => sections.map((section, index) => {
                  const statusNode = section.querySelector('.fleet-boardtab-status');
                  const countNode = section.querySelector('.fleet-boardtab-count');
                  const statusClass = Array.from(statusNode?.classList ?? [])
                    .find(value => value.startsWith('fleet-status-')) ?? '';
                  return {
                    index,
                    status: statusClass.replace('fleet-status-', ''),
                    label: statusNode?.textContent?.trim() ?? '',
                    count: Number(countNode?.textContent ?? '-1'),
                    color: statusNode ? getComputedStyle(statusNode).color : '',
                  };
                })"""
            )
            active_screenshot = OUT / "status-matrix-active-1440x900.png"
            page.screenshot(path=str(active_screenshot), full_page=True)

            action_matrix: dict[str, Any] = {}
            for status in ACTIVE_ORDER:
                open_active_task(page, state_titles[status], states[status])
                actions = visible_actions(page)
                action_matrix[status] = {
                    "actions": actions,
                    "expected": EXPECTED_ACTIONS[status],
                    "guard": page.locator(".fleet-ta-error[role='status']").all_inner_texts(),
                }

            # Archive is loaded from its dedicated server-side scope.
            page.get_by_label("Nach Status filtern").select_option("archived")
            page.get_by_label("Tasks durchsuchen").fill(state_titles["archived"])
            archive_row = page.locator(
                ".fleet-boardtab-title", has_text=state_titles["archived"]
            )
            archive_row.wait_for(timeout=20_000)
            archive_group = page.locator(".fleet-boardtab-group").evaluate(
                """section => {
                  const statusNode = section.querySelector('.fleet-boardtab-status');
                  return {
                    status: 'archived',
                    label: statusNode?.textContent?.trim() ?? '',
                    count: Number(section.querySelector('.fleet-boardtab-count')?.textContent ?? '-1'),
                    color: statusNode ? getComputedStyle(statusNode).color : '',
                  };
                }"""
            )
            archive_row.click()
            page.get_by_text(states["archived"], exact=True).wait_for(timeout=20_000)
            page.wait_for_timeout(1_000)
            action_matrix["archived"] = {
                "actions": visible_actions(page),
                "expected": EXPECTED_ACTIONS["archived"],
                "guard": [],
            }
            archive_screenshot = OUT / "status-matrix-archive-1440x900.png"
            page.screenshot(path=str(archive_screenshot), full_page=True)

            # Honest empty states for the two Board data scopes.
            page.get_by_label("Tasks durchsuchen").fill(f"NO MATCH {suffix}")
            page.get_by_text("Keine Archivtreffer", exact=True).wait_for(timeout=20_000)
            archive_empty = page.locator(".fleet-empty").inner_text()
            page.get_by_label("Nach Status filtern").select_option("all")
            page.get_by_label("Tasks durchsuchen").fill(f"NO MATCH {suffix}")
            page.get_by_text("Keine Treffer", exact=True).wait_for(timeout=20_000)
            active_empty = page.locator(".fleet-empty").inner_text()

            # Every latest real run value must survive detail API parsing and
            # be named by the drawer with its raw persisted token.
            # Start from a clean UI state so the archive-scope fetch above
            # cannot race the first active-run detail selection.
            page.reload(wait_until="domcontentloaded", timeout=30_000)
            page.get_by_role("button", name="Subtab Board", exact=True).wait_for(
                timeout=30_000
            )
            select_board(page)
            run_dom: dict[str, str] = {}
            for status, task_id in runs.items():
                open_active_task(page, run_titles[status], task_id)
                drawer = page.locator(".fleet-drawer-inner")
                drawer.get_by_text(re.compile(rf"\({re.escape(status)}\)"), exact=False).wait_for(
                    timeout=20_000
                )
                run_dom[status] = drawer.inner_text()

            # Visit the six Fleet surfaces with the same real fixture set.
            if page.get_by_role("button", name="Schließen", exact=True).count():
                page.get_by_role("button", name="Schließen", exact=True).click()
            subtab_evidence: dict[str, Any] = {}
            for label in ("Heute", "Worker", "Ketten", "Board", "Plan", "Risiko"):
                page.get_by_role(
                    "button",
                    name=re.compile(rf"^Subtab {re.escape(label)}"),
                ).click()
                page.wait_for_timeout(1_500)
                main = page.locator(".fleet-tablet-main-scroll")
                text = main.inner_text()
                overflow = page.evaluate(
                    """() => ({
                      document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                      body: document.body.scrollWidth - document.body.clientWidth,
                    })"""
                )
                screenshot = OUT / f"status-matrix-{label.lower()}-1440x900.png"
                page.screenshot(path=str(screenshot), full_page=True)
                subtab_evidence[label] = {
                    "text_excerpt": text[:1_500],
                    "text_length": len(text),
                    "overflow": overflow,
                    "screenshot": str(screenshot),
                }

            # Deleted-lane worker remains visible by its raw name.
            page.get_by_role("button", name="Subtab Worker", exact=True).click()
            page.get_by_text("audit-deleted-lane", exact=False).first.wait_for(
                timeout=20_000
            )
            deleted_lane_visible = page.get_by_text(
                "audit-deleted-lane", exact=False
            ).count()

            context.close()
            browser.close()

        api_state_statuses = {
            status: details_api[status]["body"]["task"]["status"]
            for status in states
        }
        api_latest_runs = {
            status: details_api[status]["body"]["runs"][-1]["status"]
            for status in runs
        }
        unexpected_console = [
            item for item in console_errors if "404 (Not Found)" not in item
        ]
        result = {
            "base": BASE,
            "board": "audit-scratch",
            "fixture_suffix": suffix,
            "db_task_states": db_state_rows,
            "api_task_states": api_state_statuses,
            "active_status_groups": status_groups,
            "archive_status_group": archive_group,
            "action_matrix": action_matrix,
            "db_run_histories": db_run_rows,
            "api_latest_runs": api_latest_runs,
            "dom_run_statuses": {
                status: f"({status})" in text for status, text in run_dom.items()
            },
            "legacy_without_current_writer": ["failed", "released"],
            "empty_states": {"active": active_empty, "archive": archive_empty},
            "subtabs": subtab_evidence,
            "deleted_lane_dom_matches": deleted_lane_visible,
            "board_api_http": board_api["status"],
            "unexpected_console_errors": unexpected_console,
            "screenshots": [
                str(active_screenshot),
                str(archive_screenshot),
            ],
        }
        (OUT / "state-status-matrix-summary.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(result, indent=2, sort_keys=True))

        status_group_ok = [row["status"] for row in status_groups] == ACTIVE_ORDER and all(
            row["label"] == TASK_LABELS[row["status"]]
            and row["count"] == 1
            and bool(row["color"])
            for row in status_groups
        )
        action_ok = all(
            row["actions"] == row["expected"] for row in action_matrix.values()
        )
        todo_guard = " ".join(action_matrix["todo"]["guard"])
        run_ok = (
            set(api_latest_runs) == set(runs)
            and all(api_latest_runs[key] == key for key in runs)
            and all(result["dom_run_statuses"].values())
        )
        subtab_ok = all(
            row["text_length"] > 0
            and row["overflow"]["document"] <= 0
            and row["overflow"]["body"] <= 0
            for row in subtab_evidence.values()
        )
        return 0 if (
            board_api["status"] == 200
            and set(api_state_statuses) == set(states)
            and all(api_state_statuses[key] == key for key in states)
            and status_group_ok
            and archive_group["label"] == "Archiv"
            and archive_group["count"] == 1
            and action_ok
            and "Starten nicht verfügbar" in todo_guard
            and run_ok
            and "Keine Treffer" in active_empty
            and "Keine Archivtreffer" in archive_empty
            and deleted_lane_visible > 0
            and subtab_ok
            and not unexpected_console
        ) else 1
    finally:
        cleanup_conn = conn if conn is not None else _FIXTURE_CONN
        cleanup_ids = all_ids if all_ids else _FIXTURE_IDS
        if cleanup_conn is not None:
            from hermes_cli import kanban_db as kb

            for task_id in reversed(cleanup_ids):
                task = kb.get_task(cleanup_conn, task_id)
                if task and task.status != "archived":
                    kb.archive_task(cleanup_conn, task_id)
            cleanup_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
