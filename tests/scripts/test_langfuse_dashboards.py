from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from scripts import langfuse_dashboards as dashboards


class _Response:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self._payload = payload
        self.status = status

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
        response = next(self._responses)
        if isinstance(response, _Response):
            return response
        return _Response(response)


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


@pytest.mark.parametrize("status", [401, 405])
def test_non_success_http_status_fails_before_a_follow_up_mutation(status: int) -> None:
    opener = _Opener(
        [
            _Response([{"result": {"data": {"json": {"id": "dashboard_1"}}}}], status=status),
            [{"result": {"data": {"json": {"id": "dashboard_2"}}}}],
        ]
    )
    client = dashboards.TrpcClient("http://langfuse.test", opener=opener)

    with pytest.raises(dashboards.ProvisionError, match=f"HTTP {status}"):
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


class _SqlAdapter:
    def __init__(
        self, *, extra_column: bool = False, row_count: int = 1, changes: int = 1,
        mutate_home: bool = False, read_back_widget_count: int = 10,
    ) -> None:
        self.calls: list[str] = []
        self.dashboard_upsert_params: list[tuple[object, ...]] = []
        self.row_count = row_count
        self.changes = changes
        self.mutate_home = mutate_home
        self.read_back_widget_count = read_back_widget_count
        self.home_dashboard_id = "home_unchanged"
        self.columns = {
            "dashboards": (("id", False, None, "text"),),
            "dashboard_widgets": (("id", False, None, "text"),),
        }
        if extra_column:
            self.columns["dashboards"] += (("new_required", False, None, "text"),)

    def health_version(self) -> str:
        self.calls.append("health")
        return dashboards.EXPECTED_LANGFUSE_VERSION

    def migration_ids(self) -> tuple[str, ...]:
        self.calls.append("migrations")
        return ("migration_1", "migration_2")

    def table_columns(self, table: str) -> tuple[tuple[str, bool, object, str], ...]:
        self.calls.append(f"columns:{table}")
        return self.columns[table]

    def enum_labels(self, enum_name: str) -> set[str]:
        self.calls.append(f"enum:{enum_name}")
        return {"NORMAL"}

    def projects_named(self, slug: str) -> list[dict[str, str]]:
        self.calls.append("project")
        return [{"id": "project_1", "name": slug}]

    def widget_min_version(self) -> int:
        self.calls.append("min_version")
        return 1

    def dump_tables(self, tables: tuple[str, ...]) -> None:
        assert tables == ("dashboards", "dashboard_widgets")
        self.calls.append("dump")

    def begin(self) -> None:
        self.calls.append("begin")

    def execute(self, sql: str, params: tuple[object, ...]) -> dashboards.SqlWriteResult:
        if "INSERT INTO dashboards" in sql:
            self.calls.append("dashboard_upsert")
            self.dashboard_upsert_params.append(params)
        else:
            self.calls.append("widget_upsert")
        assert "ON CONFLICT (id) DO UPDATE" in sql
        return dashboards.SqlWriteResult(rows=self.row_count, changes=self.changes)

    def commit(self) -> None:
        self.calls.append("commit")

    def rollback(self) -> None:
        self.calls.append("rollback")

    def project_home_dashboard_id(self, project_id: str) -> str:
        self.calls.append("home")
        if self.mutate_home and self.calls.count("home") == 2:
            return "home_changed"
        return self.home_dashboard_id

    def read_back_via_app(self, project_id: str, dashboard_id: str) -> dict[str, object]:
        self.calls.append("app_read_back")
        return {
            "id": dashboard_id,
            "definition": {"widgets": [{"type": "widget"}] * self.read_back_widget_count},
        }


def _sql_contract() -> dashboards.SqlGuardContract:
    return dashboards.SqlGuardContract(
        expected_migrations=frozenset({"migration_1", "migration_2"}),
        newest_migration="migration_2",
        table_fingerprints={
            "dashboards": (("id", False, None, "text"),),
            "dashboard_widgets": (("id", False, None, "text"),),
        },
        enum_labels={"DashboardType": frozenset({"NORMAL"})},
        expected_min_version=1,
    )


def _sql_rows() -> tuple[dashboards.SqlDashboardInput, ...]:
    return tuple(
        dashboards.SqlDashboardInput(
            id=f"dashboard_{dashboard_index}",
            dashboard=plan,
            widget_ids=tuple(
                f"widget_{dashboard_index}_{widget_index}" for widget_index in range(len(plan.widgets))
            ),
        )
        for dashboard_index, plan in enumerate(dashboards.load_dashboard_configs(project_id="project_1"))
    )


def test_direct_sql_is_unreachable_without_the_explicit_flag() -> None:
    adapter = _SqlAdapter()
    with pytest.raises(dashboards.ProvisionError, match="--allow-direct-sql"):
        dashboards.run_direct_sql_fallback(
            allow_direct_sql=False, adapter=adapter, contract=_sql_contract(), rows=_sql_rows()
        )
    assert adapter.calls == []


def test_direct_sql_guards_then_upserts_once_and_receipts_app_readback() -> None:
    adapter = _SqlAdapter()
    rows = _sql_rows()
    result = dashboards.run_direct_sql_fallback(
        allow_direct_sql=True, adapter=adapter, contract=_sql_contract(), rows=rows
    )
    assert adapter.calls[:10] == [
        "health", "migrations", "columns:dashboards", "columns:dashboard_widgets",
        "enum:DashboardType", "project", "min_version", "dump", "home", "begin",
    ]
    assert adapter.calls.count("begin") == adapter.calls.count("commit") == 1
    assert adapter.calls.count("rollback") == 0
    assert adapter.calls.count("dashboard_upsert") == 3
    assert adapter.calls.count("widget_upsert") == 10
    assert adapter.calls.count("app_read_back") == 3
    dashboard_definitions = []
    for params in adapter.dashboard_upsert_params:
        definition = params[4]
        assert isinstance(definition, str)
        dashboard_definitions.append(json.loads(definition))
    assert [
        [placement["widgetId"] for placement in definition["widgets"]]
        for definition in dashboard_definitions
    ] == [list(row.widget_ids) for row in rows]
    assert result.receipt["path"] == "direct_sql"
    assert result.receipt["changes"] == 13
    assert result.receipt["visible_export_evidence"]
    assert result.receipt["model_mix"]["denominator"] == "OBSERVATIONS with non-empty providedModelName"


def test_direct_sql_fails_when_app_readback_omits_configured_widget_placements() -> None:
    adapter = _SqlAdapter(read_back_widget_count=0)

    with pytest.raises(dashboards.ProvisionError, match="widget placements"):
        dashboards.run_direct_sql_fallback(
            allow_direct_sql=True, adapter=adapter, contract=_sql_contract(), rows=_sql_rows()
        )


def test_direct_sql_rejects_new_not_null_column_before_dump_or_write() -> None:
    adapter = _SqlAdapter(extra_column=True)
    with pytest.raises(dashboards.ProvisionError, match="new NOT NULL column"):
        dashboards.run_direct_sql_fallback(
            allow_direct_sql=True, adapter=adapter, contract=_sql_contract(), rows=_sql_rows()
        )
    assert "dump" not in adapter.calls
    assert "begin" not in adapter.calls
    assert "dashboard_upsert" not in adapter.calls


def test_direct_sql_rolls_back_when_an_upsert_has_an_unexpected_row_count() -> None:
    adapter = _SqlAdapter(row_count=2)
    with pytest.raises(dashboards.ProvisionError, match="unexpected row count"):
        dashboards.run_direct_sql_fallback(
            allow_direct_sql=True, adapter=adapter, contract=_sql_contract(), rows=_sql_rows()
        )
    assert adapter.calls[-1] == "rollback"


def test_direct_sql_rolls_back_if_home_dashboard_id_changes() -> None:
    adapter = _SqlAdapter(mutate_home=True)
    with pytest.raises(dashboards.ProvisionError, match="home_dashboard_id changed"):
        dashboards.run_direct_sql_fallback(
            allow_direct_sql=True, adapter=adapter, contract=_sql_contract(), rows=_sql_rows()
        )
    assert adapter.calls[-1] == "rollback"


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


def test_all_dashboard_configs_structurally_preserve_the_four_golden_fixture_fields() -> None:
    fixture = dashboards.load_golden_fixture()
    for path in dashboards.CONFIGURATION_PATHS:
        config = dashboards._read_json_object(path)
        for field in ("definition", "dimensions", "metrics", "chart_config"):
            assert config[field] == fixture[field]


@pytest.mark.parametrize("field", ["definition", "dimensions", "metrics", "chart_config"])
def test_config_fixture_drift_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: object, field: str) -> None:
    config = dashboards._read_json_object(dashboards.CONFIGURATION_PATHS[0])
    config[field] = {} if field in {"definition", "chart_config"} else [{"drift": True}]
    path = tmp_path / "drift.json"  # type: ignore[operator]
    path.write_text(json.dumps(config), encoding="utf-8")  # type: ignore[union-attr]
    monkeypatch.setattr(dashboards, "CONFIGURATION_PATHS", (path,))
    with pytest.raises(dashboards.ProvisionError, match="fixture shape"):
        dashboards.load_dashboard_configs(project_id="project_1")


def test_receipt_is_secret_free_and_a_deterministic_second_equivalent_run_has_zero_changes() -> None:
    first = dashboards.run_direct_sql_fallback(
        allow_direct_sql=True, adapter=_SqlAdapter(changes=1), contract=_sql_contract(), rows=_sql_rows()
    )
    second = dashboards.run_direct_sql_fallback(
        allow_direct_sql=True, adapter=_SqlAdapter(changes=0), contract=_sql_contract(), rows=_sql_rows()
    )
    receipt = second.receipt
    assert first.receipt["changes"] == 13
    assert receipt["changes"] == 0
    assert set(receipt) >= {"path", "understood_definition", "visible_export_evidence", "model_mix", "changes"}
    assert "project_1" not in json.dumps(receipt)


def test_trpc_receipt_is_built_from_app_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    plans = dashboards.load_dashboard_configs(project_id="project_1")

    def read_back(_client: object, plan: dashboards.DashboardInput) -> dict[str, object]:
        return {
            "id": f"readback_{plan.name}",
            "definition": {"widgets": [{"type": "widget"}] * len(plan.widgets)},
        }

    monkeypatch.setattr(dashboards, "provision_dashboard", read_back)
    receipt = dashboards.provision_dashboards_with_receipt(object(), plans)  # type: ignore[arg-type]
    assert receipt["path"] == "trpc"
    assert receipt["changes"] == 13
    assert receipt["understood_definition"]
