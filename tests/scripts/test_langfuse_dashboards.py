from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from scripts import langfuse_dashboards as dashboards


class _Response:
    status = 200

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class _Opener:
    def __init__(self, responses: list[object]) -> None:
        self._responses: Iterator[object] = iter(responses)
        self.requests: list[Any] = []

    def open(self, request: Any, *, timeout: float) -> _Response:
        self.requests.append(request)
        return _Response(next(self._responses))


def test_create_dashboard_posts_the_version_pinned_trpc_batch_envelope() -> None:
    opener = _Opener([[{"result": {"data": {"json": {"id": "dashboard_1"}}}}]])
    client = dashboards.TrpcClient("http://langfuse.test", opener=opener)

    dashboard = client.create_dashboard(
        project_id="project_1", name="North star", description="source-grounded"
    )

    assert dashboard["id"] == "dashboard_1"
    request = opener.requests[0]
    assert request.full_url == "http://langfuse.test/api/trpc/dashboard.createDashboard?batch=1"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {
        "0": {
            "json": {
                "projectId": "project_1",
                "name": "North star",
                "description": "source-grounded",
            }
        }
    }


def test_trpc_error_envelope_fails_loudly_before_a_follow_up_mutation() -> None:
    opener = _Opener(
        [
            [
                {
                    "error": {
                        "json": {
                            "message": "UNAUTHORIZED",
                            "data": {"path": "dashboard.createDashboard"},
                        }
                    }
                }
            ]
        ]
    )
    client = dashboards.TrpcClient("http://langfuse.test", opener=opener)

    with pytest.raises(dashboards.ProvisionError, match="UNAUTHORIZED"):
        client.create_dashboard(project_id="project_1", name="North star", description="x")

    assert len(opener.requests) == 1


def test_provision_uses_all_three_post_mutations_and_app_read_back() -> None:
    opener = _Opener(
        [
            [{"result": {"data": {"json": {"id": "dashboard_1"}}}}],
            [
                {
                    "result": {
                        "data": {
                            "json": {"success": True, "widget": {"id": "widget_1"}}
                        }
                    }
                }
            ],
            [{"result": {"data": {"json": {"id": "dashboard_1"}}}}],
            [
                {
                    "result": {
                        "data": {
                            "json": {
                                "id": "dashboard_1",
                                "definition": {"widgets": [{"widgetId": "widget_1"}]},
                            }
                        }
                    }
                }
            ],
        ]
    )
    plan = dashboards.DashboardInput(
        project_id="project_1",
        name="North star",
        description="source-grounded",
        widgets=(
            dashboards.WidgetInput(
                name="Cost p95",
                description="fixture-derived",
                view="observations",
                dimensions=[{"field": "providedModelName"}],
                metrics=[{"measure": "count", "agg": "COUNT"}],
                filters=[],
                chart_type="NUMBER",
                chart_config={"type": "NUMBER"},
            ),
        ),
    )

    read_back = dashboards.provision_dashboard(
        dashboards.TrpcClient("http://langfuse.test", opener=opener), plan
    )

    assert read_back["id"] == "dashboard_1"
    assert [request.full_url for request in opener.requests] == [
        "http://langfuse.test/api/trpc/dashboard.createDashboard?batch=1",
        "http://langfuse.test/api/trpc/dashboardWidgets.create?batch=1",
        "http://langfuse.test/api/trpc/dashboard.updateDashboardDefinition?batch=1",
        "http://langfuse.test/api/trpc/dashboard.getDashboard?batch=1",
    ]
    update_body = json.loads(opener.requests[2].data)
    assert update_body["0"]["json"]["definition"]["widgets"][0]["widgetId"] == "widget_1"


def test_direct_sql_is_unreachable_without_the_explicit_flag() -> None:
    calls: list[str] = []

    def write() -> None:
        calls.append("write")

    with pytest.raises(dashboards.ProvisionError, match="--allow-direct-sql"):
        dashboards.run_direct_sql_fallback(allow_direct_sql=False, write=write)

    assert calls == []


def test_direct_sql_flag_does_not_bypass_the_missing_guarded_adapter() -> None:
    calls: list[str] = []

    with pytest.raises(dashboards.ProvisionError, match="no approved guarded adapter"):
        dashboards.run_direct_sql_fallback(
            allow_direct_sql=True, write=lambda: calls.append("write")
        )

    assert calls == []


def test_dry_run_validates_fixture_without_opening_network_or_writing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("dry-run must not create a transport")

    monkeypatch.setattr(dashboards, "TrpcClient", forbidden)

    assert dashboards.main(["--dry-run"]) == 0
    assert '"status": "fixture_ready"' in capsys.readouterr().out


def test_versioned_golden_fixture_preserves_the_sanitised_ui_shapes() -> None:
    fixture = dashboards.load_golden_fixture()

    assert fixture["source"]["langfuse_version"] == "3.224.0"
    assert fixture["source"]["revision"] == "d044f366816282235898a0673d5700e05ccbee8c"
    assert fixture["definition"] == {
        "widgets": [
            {
                "x": 0,
                "y": 0,
                "id": "<REDACTED_ID>",
                "type": "widget",
                "x_size": 6,
                "y_size": 6,
                "widgetId": "<REDACTED_ID>",
            }
        ]
    }
    assert fixture["dimensions"] == []
    assert fixture["metrics"] == [{"agg": "count", "measure": "count"}]
    assert fixture["chart_config"] == {"type": "LINE_TIME_SERIES"}


def test_all_dashboard_configs_are_fixture_shaped_and_cover_required_metrics() -> None:
    configs = dashboards.load_dashboard_configs(project_id="project_1")

    assert [config.name for config in configs] == [
        "Hermes North Star",
        "Hermes Reviewer Diagnose",
        "Hermes Effizienz",
    ]
    widget_names = {widget.name for config in configs for widget in config.widgets}
    assert {
        "Euro equivalent per done task",
        "Cost p50/p95",
        "Resolve rate",
        "First-pass approval",
        "Review submissions to approval",
        "Operator veto",
        "Cache-hit ratio",
        "Exclusive token buckets",
        "Model mix",
        "Queue latency seconds",
    } <= widget_names

    model_mix = next(
        widget for config in configs for widget in config.widgets if widget.name == "Model mix"
    )
    assert model_mix.view == "observations"
    assert model_mix.dimensions == [{"field": "providedModelName"}]
    assert model_mix.metrics == [{"agg": "count", "measure": "count"}]
    assert model_mix.chart_config == {"type": "LINE_TIME_SERIES"}
