"""Ownership contract for the reconciled Kanban dashboard API surface."""

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

