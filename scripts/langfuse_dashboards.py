#!/usr/bin/env python3
"""Safely call the Langfuse 3.224.0 dashboard tRPC procedures.

The production mutation path requires an injected opener backed by an already
human-authenticated browser session. This module never discovers, reads, or
prints credentials, and it never enables direct SQL implicitly.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_FIXTURE_PATH = REPOSITORY_ROOT / "tests/scripts/fixtures/langfuse-dashboard-golden.json"
SOURCE_CONTRACT_PATH = (
    REPOSITORY_ROOT
    / "tests/scripts/fixtures/langfuse-dashboard-contract-3.224.0.json"
)
CONFIGURATION_PATHS = (
    REPOSITORY_ROOT / "config/langfuse-dashboards/control-center.json",
)
FIXTURE_VERSION = 1
SOURCE_CONTRACT_VERSION = 1
DASHBOARD_SCHEMA_VERSION = 2
EXPECTED_LANGFUSE_VERSION = "3.224.0"
EXPECTED_LANGFUSE_REVISION = "d044f366816282235898a0673d5700e05ccbee8c"
SUPPORTED_VIEWS = frozenset(
    {
        "traces",
        "observations",
        "scores-numeric",
        "scores-boolean",
        "scores-categorical",
    }
)
SUPPORTED_CHART_TYPES = frozenset(
    {
        "LINE_TIME_SERIES",
        "BAR_TIME_SERIES",
        "HORIZONTAL_BAR",
        "VERTICAL_BAR",
        "PIE",
        "NUMBER",
        "HISTOGRAM",
        "PIVOT_TABLE",
    }
)
# Source-grounded subset from Langfuse v3.224.0
# packages/shared/src/features/query/dataModel.ts. Keeping this narrow makes
# config drift fail before a dashboard mutation instead of rendering an empty
# or semantically wrong tile.
VIEW_CONTRACTS: dict[str, dict[str, Any]] = {
    "traces": {
        "dimensions": {"tags"},
        "metrics": {"count": {"count"}},
    },
    "observations": {
        "dimensions": {"tags", "providedModelName"},
        "metrics": {
            "count": {"count"},
            "totalCost": {"sum", "avg", "p50", "p95"},
            "latency": {"avg", "p50", "p95", "max"},
            "timeToFirstToken": {"avg", "p50", "p95", "max"},
        },
    },
    "scores-numeric": {
        "dimensions": {"name", "value"},
        "metrics": {
            "count": {"count"},
            "value": {"avg", "p50", "p95", "sum", "min", "max", "histogram"},
        },
    },
    "scores-boolean": {
        "dimensions": {"name", "booleanValue"},
        "metrics": {"count": {"count"}, "value": {"avg", "p50", "p95"}},
    },
    "scores-categorical": {
        "dimensions": {"name", "stringValue"},
        "metrics": {"count": {"count"}},
    },
}


class ProvisionError(RuntimeError):
    """Raised when a version-pinned Langfuse dashboard contract is violated."""


@dataclass(frozen=True)
class WidgetInput:
    """A tRPC widget payload derived from the versioned UI-export fixture."""

    name: str
    description: str
    view: str
    dimensions: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    filters: list[dict[str, Any]]
    chart_type: str
    chart_config: dict[str, Any]
    min_version: int = 1
    denominator: str = "configured source rows"
    x: int = 0
    y: int = 0
    x_size: int = 6
    y_size: int = 5


@dataclass(frozen=True)
class DashboardInput:
    """The source-grounded inputs required for one dashboard mutation flow."""

    project_id: str
    name: str
    description: str
    widgets: tuple[WidgetInput, ...]


ColumnFingerprint = tuple[str, bool, object | None, str]


@dataclass(frozen=True)
class SqlWriteResult:
    """The adapter's row-count result; ``changes`` may be zero for an idempotent upsert."""

    rows: int
    changes: int


@dataclass(frozen=True)
class SqlDashboardInput:
    """Caller-owned stable IDs for the explicit SQL-only fallback path."""

    id: str
    dashboard: DashboardInput
    widget_ids: tuple[str, ...]


@dataclass(frozen=True)
class SqlGuardContract:
    """Pinned guard expectations supplied by the authorised SQL integration."""

    expected_migrations: frozenset[str]
    newest_migration: str
    table_fingerprints: Mapping[str, tuple[ColumnFingerprint, ...]]
    enum_labels: Mapping[str, frozenset[str]]
    expected_min_version: int
    allowed_versions: frozenset[str] = frozenset({EXPECTED_LANGFUSE_VERSION})


@dataclass(frozen=True)
class SqlProvisionResult:
    receipt: dict[str, Any]


class DirectSqlAdapter(Protocol):
    """Narrow dependency-injection boundary; no DSN or driver is bundled here."""

    def health_version(self) -> str: ...
    def migration_ids(self) -> Sequence[str]: ...
    def table_columns(self, table: str) -> Sequence[ColumnFingerprint]: ...
    def enum_labels(self, enum_name: str) -> set[str]: ...
    def projects_named(self, slug: str) -> Sequence[Mapping[str, Any]]: ...
    def widget_min_version(self) -> int: ...
    def dump_tables(self, tables: tuple[str, ...]) -> None: ...
    def begin(self) -> None: ...
    def execute(self, sql: str, params: tuple[object, ...]) -> SqlWriteResult: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def project_home_dashboard_id(self, project_id: str) -> object | None: ...
    def read_back_via_app(self, project_id: str, dashboard_id: str) -> Mapping[str, Any]: ...


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvisionError(f"could not load JSON object from {path}") from exc
    if not isinstance(value, dict):
        raise ProvisionError(f"{path} must contain a JSON object")
    return value


def load_golden_fixture(path: Path = GOLDEN_FIXTURE_PATH) -> dict[str, Any]:
    """Load the human-created, consistently ID-sanitised widget export."""
    fixture = _read_json_object(path)
    source = fixture.get("source")
    if fixture.get("fixture_version") != FIXTURE_VERSION or not isinstance(source, dict):
        raise ProvisionError("golden fixture has an unsupported version or source metadata")
    if (
        source.get("langfuse_version") != EXPECTED_LANGFUSE_VERSION
        or source.get("revision") != EXPECTED_LANGFUSE_REVISION
    ):
        raise ProvisionError("golden fixture is not pinned to the supported Langfuse release")
    required = (
        "definition",
        "view",
        "dimensions",
        "metrics",
        "filters",
        "chart_type",
        "chart_config",
    )
    if any(field not in fixture for field in required):
        raise ProvisionError("golden fixture omits a required UI-export field")
    if (
        not isinstance(fixture["definition"], dict)
        or not isinstance(fixture["view"], str)
        or not isinstance(fixture["dimensions"], list)
    ):
        raise ProvisionError("golden fixture has an invalid definition or dimensions shape")
    if (
        not isinstance(fixture["metrics"], list)
        or not isinstance(fixture["filters"], list)
        or not isinstance(fixture["chart_type"], str)
        or not isinstance(fixture["chart_config"], dict)
        or fixture["chart_config"].get("type") != fixture["chart_type"]
    ):
        raise ProvisionError("golden fixture has an invalid metrics or chart_config shape")
    return fixture


def load_source_contract(path: Path = SOURCE_CONTRACT_PATH) -> dict[str, Any]:
    """Load the checked-in extraction of the pinned Langfuse UI/query contract."""
    contract = _read_json_object(path)
    source = contract.get("source")
    if (
        contract.get("contract_version") != SOURCE_CONTRACT_VERSION
        or not isinstance(source, dict)
        or source.get("langfuse_version") != EXPECTED_LANGFUSE_VERSION
        or source.get("revision") != EXPECTED_LANGFUSE_REVISION
    ):
        raise ProvisionError("source contract is not pinned to the supported Langfuse release")
    source_files = source.get("files")
    if (
        source.get("repository") != "langfuse/langfuse"
        or not isinstance(source_files, list)
        or not source_files
        or any(not isinstance(item, str) or not item for item in source_files)
    ):
        raise ProvisionError("source contract lacks auditable upstream file provenance")

    chart_types = contract.get("supported_chart_types")
    if (
        not isinstance(chart_types, list)
        or any(not isinstance(item, str) for item in chart_types)
        or frozenset(chart_types) != SUPPORTED_CHART_TYPES
    ):
        raise ProvisionError("source contract chart types drifted from the validator")
    chart_fields = contract.get("chart_config_fields")
    if not isinstance(chart_fields, dict) or set(chart_fields) != SUPPORTED_CHART_TYPES:
        raise ProvisionError("source contract omits chart configuration field contracts")
    if any(
        not isinstance(fields, list)
        or "type" not in fields
        or any(not isinstance(field, str) for field in fields)
        for fields in chart_fields.values()
    ):
        raise ProvisionError("source contract has invalid chart configuration fields")

    raw_views = contract.get("views")
    if not isinstance(raw_views, dict):
        raise ProvisionError("source contract omits view contracts")
    normalized_views: dict[str, dict[str, Any]] = {}
    for view_name, raw_view in raw_views.items():
        if (
            not isinstance(view_name, str)
            or not isinstance(raw_view, dict)
            or not isinstance(raw_view.get("dimensions"), list)
            or any(
                not isinstance(dimension, str)
                for dimension in raw_view.get("dimensions", [])
            )
            or not isinstance(raw_view.get("metrics"), dict)
        ):
            raise ProvisionError("source contract has an invalid view shape")
        metrics: dict[str, set[str]] = {}
        for measure, aggregations in raw_view["metrics"].items():
            if (
                not isinstance(measure, str)
                or not isinstance(aggregations, list)
                or any(not isinstance(item, str) for item in aggregations)
            ):
                raise ProvisionError("source contract has an invalid metric shape")
            metrics[measure] = set(aggregations)
        normalized_views[view_name] = {
            "dimensions": set(raw_view["dimensions"]),
            "metrics": metrics,
        }
    if normalized_views != VIEW_CONTRACTS:
        raise ProvisionError("source contract views drifted from the validator")

    filter_operators = contract.get("filter_operators")
    if not isinstance(filter_operators, dict) or any(
        not isinstance(filter_type, str)
        or not isinstance(operators, list)
        or not operators
        or any(not isinstance(operator, str) for operator in operators)
        for filter_type, operators in filter_operators.items()
    ):
        raise ProvisionError("source contract has invalid filter operators")
    return contract


def load_dashboard_configs(*, project_id: str) -> list[DashboardInput]:
    """Load the one version-pinned control center and reject contract drift."""
    if not project_id:
        raise ProvisionError("a project id is required to load dashboard configurations")
    load_golden_fixture()
    source_contract = load_source_contract()
    chart_config_fields = source_contract["chart_config_fields"]
    filter_operators = source_contract["filter_operators"]
    configured: list[DashboardInput] = []
    for path in CONFIGURATION_PATHS:
        config = _read_json_object(path)
        source = config.get("source")
        if config.get("schema_version") != DASHBOARD_SCHEMA_VERSION:
            raise ProvisionError(f"{path} has an unsupported dashboard schema version")
        if not isinstance(source, dict) or (
            source.get("langfuse_version") != EXPECTED_LANGFUSE_VERSION
            or source.get("revision") != EXPECTED_LANGFUSE_REVISION
        ):
            raise ProvisionError(f"{path} is not pinned to the supported Langfuse source")
        name, description, widgets = config.get("name"), config.get("description"), config.get("widgets")
        if not isinstance(name, str) or not name or not isinstance(description, str):
            raise ProvisionError(f"{path} lacks a valid dashboard name or description")
        if not isinstance(widgets, list) or not widgets:
            raise ProvisionError(f"{path} must define at least one widget")
        widget_inputs: list[WidgetInput] = []
        for index, raw_widget in enumerate(widgets):
            if not isinstance(raw_widget, dict):
                raise ProvisionError(f"{path} widget {index} is not an object")
            required = (
                "name",
                "description",
                "view",
                "dimensions",
                "metrics",
                "filters",
                "chart_type",
                "chart_config",
                "denominator",
                "layout",
            )
            if any(field not in raw_widget for field in required):
                raise ProvisionError(f"{path} widget {index} omits a required field")
            view = raw_widget["view"]
            chart_type = raw_widget["chart_type"]
            chart_config = raw_widget["chart_config"]
            if not isinstance(view, str) or not isinstance(chart_type, str) or not isinstance(chart_config, dict):
                raise ProvisionError(f"{path} widget {index} has invalid view or chart configuration")
            view = view.lower()
            if view not in SUPPORTED_VIEWS:
                raise ProvisionError(f"{path} widget {index} uses unsupported view {view!r}")
            if chart_type not in SUPPORTED_CHART_TYPES:
                raise ProvisionError(
                    f"{path} widget {index} uses unsupported chart type {chart_type!r}"
                )
            if chart_config.get("type") != chart_type:
                raise ProvisionError(f"{path} widget {index} chart_config.type must equal chart_type")
            unsupported_chart_fields = set(chart_config) - set(
                chart_config_fields[chart_type]
            )
            if unsupported_chart_fields:
                raise ProvisionError(
                    f"{path} widget {index} uses unsupported chart_config fields: "
                    + ", ".join(sorted(unsupported_chart_fields))
                )
            row_limit = chart_config.get("row_limit")
            if row_limit is not None and (
                not isinstance(row_limit, int) or not 1 <= row_limit <= 1000
            ):
                raise ProvisionError(f"{path} widget {index} has invalid row_limit")
            bins = chart_config.get("bins")
            if chart_type == "HISTOGRAM":
                if isinstance(bins, bool) or not isinstance(bins, int) or not 1 <= bins <= 100:
                    raise ProvisionError(f"{path} widget {index} has invalid histogram bins")
            elif bins is not None:
                raise ProvisionError(f"{path} widget {index} sets bins outside a histogram")
            if not isinstance(raw_widget["denominator"], str) or not raw_widget["denominator"]:
                raise ProvisionError(f"{path} widget {index} silently omits its denominator")
            for field in ("name", "description"):
                if not isinstance(raw_widget[field], str) or not raw_widget[field]:
                    raise ProvisionError(f"{path} widget {index} has an invalid {field}")
            for field in ("dimensions", "metrics", "filters"):
                if not isinstance(raw_widget[field], list):
                    raise ProvisionError(f"{path} widget {index} has an invalid {field}")
            if not raw_widget["metrics"] or any(
                not isinstance(metric, dict)
                or not isinstance(metric.get("measure"), str)
                or not isinstance(metric.get("agg"), str)
                for metric in raw_widget["metrics"]
            ):
                raise ProvisionError(f"{path} widget {index} has invalid metrics")
            if any(
                not isinstance(dimension, dict)
                or not isinstance(dimension.get("field"), str)
                for dimension in raw_widget["dimensions"]
            ):
                raise ProvisionError(f"{path} widget {index} has invalid dimensions")
            if any(
                not isinstance(item, dict)
                or not all(key in item for key in ("column", "operator", "type", "value"))
                for item in raw_widget["filters"]
            ):
                raise ProvisionError(f"{path} widget {index} has invalid filters")
            for item in raw_widget["filters"]:
                filter_type = item["type"]
                operator = item["operator"]
                allowed_operators = filter_operators.get(filter_type)
                if (
                    not isinstance(filter_type, str)
                    or not isinstance(operator, str)
                    or not isinstance(allowed_operators, list)
                    or operator not in allowed_operators
                ):
                    raise ProvisionError(
                        f"{path} widget {index} uses an unsupported filter operator"
                    )
                value = item["value"]
                if filter_type == "string" and not isinstance(value, str):
                    raise ProvisionError(f"{path} widget {index} has an invalid string filter")
                if filter_type == "arrayOptions" and (
                    not isinstance(value, list)
                    or any(not isinstance(entry, str) for entry in value)
                    or (operator == "any of" and not value)
                ):
                    raise ProvisionError(f"{path} widget {index} has an invalid array filter")
            contract = VIEW_CONTRACTS[view]
            dimension_fields = {
                str(dimension["field"]) for dimension in raw_widget["dimensions"]
            }
            filter_fields = {str(item["column"]) for item in raw_widget["filters"]}
            unsupported_dimensions = (
                dimension_fields | filter_fields
            ) - set(contract["dimensions"])
            if unsupported_dimensions:
                raise ProvisionError(
                    f"{path} widget {index} uses unsupported {view} dimensions: "
                    + ", ".join(sorted(unsupported_dimensions))
                )
            for metric in raw_widget["metrics"]:
                measure = str(metric["measure"])
                aggregation = str(metric["agg"])
                allowed = contract["metrics"].get(measure)
                if allowed is None or aggregation not in allowed:
                    raise ProvisionError(
                        f"{path} widget {index} uses unsupported metric "
                        f"{aggregation}({measure}) for {view}"
                    )
            if chart_type == "HISTOGRAM" and (
                len(raw_widget["metrics"]) != 1
                or raw_widget["metrics"][0] != {"agg": "histogram", "measure": "value"}
            ):
                raise ProvisionError(
                    f"{path} widget {index} histogram must use histogram(value)"
                )
            layout = raw_widget["layout"]
            if not isinstance(layout, dict):
                raise ProvisionError(f"{path} widget {index} has invalid layout")
            layout_values = tuple(layout.get(key) for key in ("x", "y", "x_size", "y_size"))
            if (
                any(isinstance(value, bool) or not isinstance(value, int) for value in layout_values)
                or layout_values[0] < 0
                or layout_values[1] < 0
                or layout_values[2] < 1
                or layout_values[3] < 1
                or layout_values[0] + layout_values[2] > 12
            ):
                raise ProvisionError(f"{path} widget {index} exceeds the 12-column layout")
            widget_inputs.append(
                WidgetInput(
                    name=raw_widget["name"],
                    description=raw_widget["description"],
                    view=view,
                    dimensions=raw_widget["dimensions"],
                    metrics=raw_widget["metrics"],
                    filters=raw_widget["filters"],
                    chart_type=chart_type,
                    chart_config=chart_config,
                    min_version=int(raw_widget.get("min_version", 1)),
                    denominator=raw_widget["denominator"],
                    x=layout_values[0],
                    y=layout_values[1],
                    x_size=layout_values[2],
                    y_size=layout_values[3],
                )
            )
        for left_index, left in enumerate(widget_inputs):
            for right in widget_inputs[left_index + 1 :]:
                overlaps_x = left.x < right.x + right.x_size and right.x < left.x + left.x_size
                overlaps_y = left.y < right.y + right.y_size and right.y < left.y + left.y_size
                if overlaps_x and overlaps_y:
                    raise ProvisionError(
                        f"{path} widgets {left.name!r} and {right.name!r} overlap"
                    )
        configured.append(
            DashboardInput(
                project_id=project_id,
                name=name,
                description=description,
                widgets=tuple(widget_inputs),
            )
        )
    if len(configured) != 1:
        raise ProvisionError("exactly one native Hermes control center must be configured")
    return configured


class TrpcClient:
    """Small injectable tRPC client for Langfuse revision d044f366.

    Authentication is delegated to ``opener``. A production caller may inject
    only a browser-session adapter; this module never reads or prints cookies.
    """

    def __init__(self, base_url: str, *, opener: Any | None = None, timeout: float = 10.0) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ProvisionError("Langfuse base URL must use http or https")
        self._base_url = base_url.rstrip("/")
        self._opener = opener or urllib.request.build_opener()
        self._timeout = timeout

    def _post(self, procedure: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        envelope = {"0": {"json": dict(payload)}}
        request = urllib.request.Request(
            f"{self._base_url}/api/trpc/{procedure}?batch=1",
            data=json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = getattr(response, "status", None)
                if status is None:
                    getcode = getattr(response, "getcode", None)
                    status = getcode() if callable(getcode) else None
                if status != 200:
                    raise ProvisionError(f"{procedure} returned HTTP {status!r}")
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise ProvisionError(f"{procedure} returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ProvisionError(f"{procedure} could not be reached: {exc.reason}") from exc
        except OSError as exc:
            raise ProvisionError(f"{procedure} transport failed") from exc

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProvisionError(f"{procedure} returned invalid JSON") from exc
        if not isinstance(decoded, list) or len(decoded) != 1 or not isinstance(decoded[0], dict):
            raise ProvisionError(f"{procedure} returned an unexpected tRPC batch response")
        entry = decoded[0]
        error = entry.get("error")
        if isinstance(error, dict):
            error_json = error.get("json")
            if isinstance(error_json, dict):
                message = error_json.get("message")
                path = (error_json.get("data") or {}).get("path")
                if isinstance(message, str):
                    raise ProvisionError(f"{procedure} rejected the request: {message} ({path})")
            raise ProvisionError(f"{procedure} returned an unrecognised tRPC error")
        result = entry.get("result")
        if not isinstance(result, dict):
            raise ProvisionError(f"{procedure} returned neither result nor error")
        data = result.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("json"), dict):
            raise ProvisionError(f"{procedure} result does not contain data.json")
        return data["json"]

    def create_dashboard(self, *, project_id: str, name: str, description: str) -> dict[str, Any]:
        return self._post(
            "dashboard.createDashboard",
            {"projectId": project_id, "name": name, "description": description},
        )

    def create_widget(self, *, project_id: str, widget: WidgetInput) -> dict[str, Any]:
        result = self._post(
            "dashboardWidgets.create",
            {
                "projectId": project_id,
                "name": widget.name,
                "description": widget.description,
                "view": widget.view,
                "dimensions": widget.dimensions,
                "metrics": widget.metrics,
                "filters": widget.filters,
                "chartType": widget.chart_type,
                "chartConfig": widget.chart_config,
                "minVersion": widget.min_version,
            },
        )
        if result.get("success") is not True or not isinstance(result.get("widget"), dict):
            raise ProvisionError("dashboardWidgets.create returned an unexpected success response")
        return result["widget"]

    def update_dashboard_definition(
        self, *, project_id: str, dashboard_id: str, definition: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._post(
            "dashboard.updateDashboardDefinition",
            {"projectId": project_id, "dashboardId": dashboard_id, "definition": dict(definition)},
        )

    def get_dashboard(self, *, project_id: str, dashboard_id: str) -> dict[str, Any]:
        return self._post(
            "dashboard.getDashboard",
            {"projectId": project_id, "dashboardId": dashboard_id},
        )


def provision_dashboard(client: TrpcClient, dashboard: DashboardInput) -> dict[str, Any]:
    """Create widgets, attach them, and verify the app's read-back response."""
    created_dashboard = client.create_dashboard(
        project_id=dashboard.project_id, name=dashboard.name, description=dashboard.description
    )
    dashboard_id = created_dashboard.get("id")
    if not isinstance(dashboard_id, str) or not dashboard_id:
        raise ProvisionError("dashboard.createDashboard did not return a dashboard id")

    widget_ids: list[str] = []
    for widget in dashboard.widgets:
        created_widget = client.create_widget(project_id=dashboard.project_id, widget=widget)
        widget_id = created_widget.get("id")
        if not isinstance(widget_id, str) or not widget_id:
            raise ProvisionError("dashboardWidgets.create did not return a widget id")
        widget_ids.append(widget_id)

    updated_dashboard = client.update_dashboard_definition(
        project_id=dashboard.project_id,
        dashboard_id=dashboard_id,
        definition={"widgets": _widget_placements(widget_ids, dashboard.widgets)},
    )
    if updated_dashboard.get("id") != dashboard_id:
        raise ProvisionError("dashboard.updateDashboardDefinition returned an unexpected dashboard")
    read_back = client.get_dashboard(project_id=dashboard.project_id, dashboard_id=dashboard_id)
    if read_back.get("id") != dashboard_id or not isinstance(read_back.get("definition"), dict):
        raise ProvisionError("app read-back did not return the created dashboard definition")
    return read_back


def provision_dashboards_with_receipt(
    client: TrpcClient, dashboards: Sequence[DashboardInput]
) -> dict[str, Any]:
    """Primary tRPC path with a secret-free receipt built from required app read-back."""
    read_backs = [provision_dashboard(client, dashboard) for dashboard in dashboards]
    rows: list[SqlDashboardInput] = []
    for dashboard, read_back in zip(dashboards, read_backs, strict=True):
        dashboard_id = read_back.get("id")
        if not isinstance(dashboard_id, str) or not dashboard_id:
            raise ProvisionError("app read-back omitted the created dashboard id")
        rows.append(SqlDashboardInput(id=dashboard_id, dashboard=dashboard, widget_ids=()))
    return _app_readback_receipt(
        path="trpc",
        rows=rows,
        read_backs=read_backs,
        changes=sum(1 + len(dashboard.widgets) for dashboard in dashboards),
    )


_TABLES = ("dashboards", "dashboard_widgets")
_DASHBOARD_UPSERT = """INSERT INTO dashboards (id, project_id, name, description, definition)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description,
definition = EXCLUDED.definition"""
_WIDGET_UPSERT = """INSERT INTO dashboard_widgets
(id, project_id, name, description, view, dimensions, metrics, filters, chart_type, chart_config, min_version)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description,
view = EXCLUDED.view, dimensions = EXCLUDED.dimensions, metrics = EXCLUDED.metrics,
filters = EXCLUDED.filters, chart_type = EXCLUDED.chart_type, chart_config = EXCLUDED.chart_config,
min_version = EXCLUDED.min_version"""


def _widget_placements(
    widget_ids: Sequence[str],
    widgets: Sequence[WidgetInput] | None = None,
) -> list[dict[str, Any]]:
    """Build stable placements from the reviewed 12-column control-center layout."""
    if widgets is not None and len(widget_ids) != len(widgets):
        raise ProvisionError("widget placement count does not match widget configuration")
    return [
        {
            "type": "widget",
            "id": f"hermes-control-center-placement-{index}",
            "widgetId": widget_id,
            "x": widgets[index].x if widgets is not None else 0,
            "y": widgets[index].y if widgets is not None else index * 6,
            "x_size": widgets[index].x_size if widgets is not None else 6,
            "y_size": widgets[index].y_size if widgets is not None else 6,
        }
        for index, widget_id in enumerate(widget_ids)
    ]


def _validate_sql_guards(adapter: DirectSqlAdapter, contract: SqlGuardContract) -> str:
    """Run all read-only guards in the required order, before dumping or writing."""
    version = adapter.health_version()
    if version not in contract.allowed_versions:
        raise ProvisionError("Langfuse health version is not in the direct-SQL allowlist")

    migrations = tuple(adapter.migration_ids())
    if frozenset(migrations) != contract.expected_migrations or not migrations:
        raise ProvisionError("_prisma_migrations does not match the complete expected set")
    if migrations[-1] != contract.newest_migration:
        raise ProvisionError("_prisma_migrations newest migration does not match the contract")

    for table in _TABLES:
        actual = tuple(adapter.table_columns(table))
        expected = contract.table_fingerprints.get(table)
        if expected is None:
            raise ProvisionError(f"missing expected information_schema fingerprint for {table}")
        expected_names = {column[0] for column in expected}
        for name, nullable, default, _type in actual:
            if name not in expected_names and not nullable and default is None:
                raise ProvisionError(f"{table} has a new NOT NULL column without a default: {name}")
        if actual != expected:
            raise ProvisionError(f"{table} information_schema fingerprint does not match the contract")

    for enum_name, expected_labels in contract.enum_labels.items():
        if adapter.enum_labels(enum_name) != set(expected_labels):
            raise ProvisionError(f"pg_enum labels for {enum_name} do not match the contract")

    projects = tuple(adapter.projects_named("hermes-agent"))
    if len(projects) != 1 or not isinstance(projects[0].get("id"), str):
        raise ProvisionError("expected exactly one hermes-agent project row")
    if adapter.widget_min_version() != contract.expected_min_version:
        raise ProvisionError("dashboard widget min_version does not match the contract")
    return str(projects[0]["id"])


def _app_readback_receipt(
    *, path: str, rows: Sequence[SqlDashboardInput], read_backs: Sequence[Mapping[str, Any]], changes: int
) -> dict[str, Any]:
    """Create a secret-free record from app read-back plus explicit visible export evidence."""
    if len(rows) != len(read_backs):
        raise ProvisionError("app read-back count does not match the provisioned dashboards")
    understood: list[dict[str, Any]] = []
    evidence: list[dict[str, str]] = []
    model_mix: dict[str, str] | None = None
    for row, read_back in zip(rows, read_backs, strict=True):
        if read_back.get("id") != row.id or not isinstance(read_back.get("definition"), dict):
            raise ProvisionError("app read-back did not return the expected dashboard definition")
        definition = read_back["definition"]
        placements = definition.get("widgets")
        if not isinstance(placements, list):
            raise ProvisionError("app read-back definition omitted widget placements")
        if len(placements) < len(row.dashboard.widgets):
            raise ProvisionError("app read-back definition has fewer widget placements than configured widgets")
        understood.append({"dashboard": row.dashboard.name, "widget_placements": len(placements)})
        for widget in row.dashboard.widgets:
            source = (
                "observations"
                if widget.view == "observations"
                else "traces"
                if widget.view == "traces"
                else "exported_score"
            )
            evidence.append(
                {"dashboard": row.dashboard.name, "widget": widget.name, "source": source,
                 "denominator": _widget_denominator(row.dashboard, widget.name)}
            )
            if model_mix is None and widget.view == "observations" and any(
                dimension.get("field") == "providedModelName"
                for dimension in widget.dimensions
            ):
                model_mix = {
                    "source": "OBSERVATIONS", "dimension": "providedModelName",
                    "denominator": widget.denominator,
                }
    if not any(item["source"] == "exported_score" for item in evidence) or not any(
        item["source"] == "observations" for item in evidence
    ):
        raise ProvisionError("app receipt requires visible exported score and observation evidence")
    if model_mix is None:
        raise ProvisionError("app receipt requires a model observation denominator")
    return {
        "receipt_version": 2, "path": path, "understood_definition": understood,
        "visible_export_evidence": evidence, "model_mix": model_mix, "changes": changes,
    }


def _widget_denominator(dashboard: DashboardInput, name: str) -> str:
    for widget in dashboard.widgets:
        if widget.name == name:
            return widget.denominator
    raise ProvisionError(f"no explicit denominator found for {dashboard.name}/{name}")


def run_direct_sql_fallback(
    *, allow_direct_sql: bool, adapter: DirectSqlAdapter, contract: SqlGuardContract,
    rows: Sequence[SqlDashboardInput],
) -> SqlProvisionResult:
    """Run an opt-in guarded upsert; tRPC callers never invoke this function automatically."""
    if not allow_direct_sql:
        raise ProvisionError("direct SQL is disabled; pass --allow-direct-sql explicitly")
    if not rows:
        raise ProvisionError("direct SQL requires at least one explicitly identified dashboard row")
    project_id = _validate_sql_guards(adapter, contract)
    if any(row.dashboard.project_id != project_id for row in rows):
        raise ProvisionError("SQL rows do not belong to the uniquely guarded hermes-agent project")
    adapter.dump_tables(_TABLES)
    before_home_dashboard_id = adapter.project_home_dashboard_id(project_id)
    started = False
    changes = 0
    try:
        adapter.begin()
        started = True
        for row in rows:
            if len(row.widget_ids) != len(row.dashboard.widgets):
                raise ProvisionError("each direct-SQL dashboard needs exactly one stable widget id per widget")
            result = adapter.execute(
                _DASHBOARD_UPSERT,
                (
                    row.id,
                    project_id,
                    row.dashboard.name,
                    row.dashboard.description,
                    json.dumps(
                        {
                            "widgets": _widget_placements(
                                row.widget_ids,
                                row.dashboard.widgets,
                            )
                        }
                    ),
                ),
            )
            if result.rows != 1:
                raise ProvisionError("dashboard upsert returned an unexpected row count")
            changes += result.changes
            for widget_id, widget in zip(row.widget_ids, row.dashboard.widgets, strict=True):
                result = adapter.execute(
                    _WIDGET_UPSERT,
                    (widget_id, project_id, widget.name, widget.description, widget.view,
                     json.dumps(widget.dimensions), json.dumps(widget.metrics), json.dumps(widget.filters),
                     widget.chart_type, json.dumps(widget.chart_config), widget.min_version),
                )
                if result.rows != 1:
                    raise ProvisionError("dashboard widget upsert returned an unexpected row count")
                changes += result.changes
        if adapter.project_home_dashboard_id(project_id) != before_home_dashboard_id:
            raise ProvisionError("projects.home_dashboard_id changed during direct-SQL provisioning")
        adapter.commit()
        started = False
    except Exception:
        if started:
            adapter.rollback()
        raise
    read_backs = [adapter.read_back_via_app(project_id, row.id) for row in rows]
    return SqlProvisionResult(
        receipt=_app_readback_receipt(path="direct_sql", rows=rows, read_backs=read_backs, changes=changes)
    )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate fixture/config readiness only; never creates a transport or writes.",
    )
    parser.add_argument(
        "--allow-direct-sql",
        action="store_true",
        help="Reserved for an independently guarded fallback adapter; no adapter is bundled.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    if args.dry_run:
        dashboards = load_dashboard_configs(project_id="dry-run-project")
        print(
            json.dumps(
                {
                    "status": "control_center_ready",
                    "changes": 0,
                    "dashboard_count": len(dashboards),
                    "widget_count": sum(len(item.widgets) for item in dashboards),
                },
                sort_keys=True,
            )
        )
        return 0
    raise ProvisionError(
        "no authorised browser-session adapter is configured; do not pass cookies or credentials "
        "to this CLI, and use an approved session adapter for the live mutation path"
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProvisionError as exc:
        print(f"langfuse-dashboard-provisioning: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
