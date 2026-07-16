"""Ownership contract for the reconciled Kanban dashboard API surface."""

from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli.kanban import _task_to_dict
from plugins.kanban.dashboard import plugin_api


UPSTREAM_CORE_ROUTES = {
    ("GET", "/board"),
    ("GET", "/tasks/{task_id}"),
    ("POST", "/tasks"),
    ("GET", "/tasks/{task_id}/attachments"),
    ("POST", "/tasks/{task_id}/attachments"),
    ("GET", "/attachments/{attachment_id}"),
    ("DELETE", "/attachments/{attachment_id}"),
    ("PATCH", "/tasks/{task_id}"),
    ("DELETE", "/tasks/{task_id}"),
    ("POST", "/tasks/{task_id}/comments"),
    ("POST", "/links"),
    ("DELETE", "/links"),
    ("POST", "/tasks/bulk"),
    ("GET", "/diagnostics"),
    ("GET", "/workers/active"),
    ("GET", "/runs/{run_id}"),
    ("GET", "/runs/{run_id}/inspect"),
    ("POST", "/runs/{run_id}/terminate"),
    ("POST", "/tasks/{task_id}/reclaim"),
    ("POST", "/tasks/{task_id}/specify"),
    ("POST", "/tasks/{task_id}/reassign"),
    ("GET", "/config"),
    ("GET", "/home-channels"),
    ("POST", "/tasks/{task_id}/home-subscribe/{platform}"),
    ("DELETE", "/tasks/{task_id}/home-subscribe/{platform}"),
    ("GET", "/stats"),
    ("GET", "/assignees"),
    ("GET", "/tasks/{task_id}/log"),
    ("POST", "/dispatch"),
    ("GET", "/boards"),
    ("POST", "/boards"),
    ("PATCH", "/boards/{slug}"),
    ("DELETE", "/boards/{slug}"),
    ("POST", "/boards/{slug}/switch"),
    ("GET", "/profiles"),
    ("PATCH", "/profiles/{profile_name}"),
    ("POST", "/profiles/{profile_name}/describe-auto"),
    ("POST", "/tasks/{task_id}/decompose"),
    ("GET", "/orchestration"),
    ("PUT", "/orchestration"),
    ("WEBSOCKET", "/events"),
}


def test_upstream_core_routes_have_one_explicit_owner():
    contract = plugin_api.route_contract

    assert contract.route_keys("core") == UPSTREAM_CORE_ROUTES
    assert len(contract.records) == len(plugin_api.router.routes)
    assert len({record.key for record in contract.records}) == len(contract.records)


def test_local_dashboard_strengths_live_in_edge_namespaces():
    owners = plugin_api.route_contract.owner_by_key()

    assert owners[("GET", "/tasks/{task_id}/deliverables")] == "evidence"
    assert owners[("GET", "/decision-queue")] == "control"
    assert owners[("GET", "/lanes")] == "lanes"
    assert owners[("GET", "/runs/costs")] == "observability"
    assert owners[("POST", "/push/subscribe")] == "delivery"
    assert owners[("GET", "/planspecs")] == "planspec"
    assert owners[("POST", "/tasks/{task_id}/release-gate")] == "flow_release"


def test_extension_handlers_are_physically_outside_the_core_api_module():
    """Owner metadata must correspond to a real source-file boundary."""
    core_path = Path(plugin_api.__file__).resolve()
    core_source = core_path.read_text(encoding="utf-8")

    assert len(core_source.splitlines()) < 4_000
    for owner in (
        "evidence",
        "control",
        "lanes",
        "observability",
        "delivery",
        "planspec",
        "flow_release",
    ):
        assert f"@{owner}_routes" not in core_source

    for record, route in zip(
        plugin_api.route_contract.records,
        plugin_api.router.routes,
        strict=True,
    ):
        endpoint_path = Path(route.endpoint.__code__.co_filename).resolve()
        if record.owner == "core":
            assert endpoint_path == core_path
        else:
            assert endpoint_path != core_path


def test_task_detail_read_models_project_planspec_metadata():
    """The task-detail route and CLI JSON expose structured PlanSpec metadata."""
    acceptance_criteria = [
        {
            "id": "AC-1",
            "statement": "Projection is available",
            "verification": "GET task detail",
            "done_signal": "fields returned",
        }
    ]
    task = kb.Task(
        id="t_projection",
        title="Projection",
        body=None,
        assignee=None,
        status="done",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
        acceptance_criteria=plugin_api.json.dumps(acceptance_criteria),
        planspec_subtask_id="S1",
        freigabe="complete",
        live_test_depth="smoke",
    )

    route_task = getattr(plugin_api, "_task_dict")(task)
    cli_task = _task_to_dict(task)

    for projected in (route_task, cli_task):
        assert projected["acceptance_criteria"] == acceptance_criteria
        assert projected["planspec_subtask_id"] == "S1"
        assert projected["freigabe"] == "complete"
        assert projected["live_test_depth"] == "smoke"


def test_task_detail_read_models_fail_soft_on_invalid_acceptance_json():
    task = kb.Task(
        id="t_invalid_projection",
        title="Invalid Projection",
        body=None,
        assignee=None,
        status="done",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
        acceptance_criteria="not json",
    )

    projected = getattr(plugin_api, "_task_dict")(task)

    assert projected["acceptance_criteria"] is None
    assert projected["acceptance_criteria_raw"] == "not json"
