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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_FIXTURE_PATH = REPOSITORY_ROOT / "tests/scripts/fixtures/langfuse-dashboard-golden.json"
CONFIGURATION_PATHS = (
    REPOSITORY_ROOT / "config/langfuse-dashboards/north-star.json",
    REPOSITORY_ROOT / "config/langfuse-dashboards/reviewer-diagnose.json",
    REPOSITORY_ROOT / "config/langfuse-dashboards/effizienz.json",
)
FIXTURE_VERSION = 1
EXPECTED_LANGFUSE_VERSION = "3.224.0"
EXPECTED_LANGFUSE_REVISION = "d044f366816282235898a0673d5700e05ccbee8c"


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


@dataclass(frozen=True)
class DashboardInput:
    """The source-grounded inputs required for one dashboard mutation flow."""

    project_id: str
    name: str
    description: str
    widgets: tuple[WidgetInput, ...]


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
    required = ("definition", "dimensions", "metrics", "chart_config")
    if any(field not in fixture for field in required):
        raise ProvisionError("golden fixture omits a required UI-export field")
    if not isinstance(fixture["definition"], dict) or not isinstance(fixture["dimensions"], list):
        raise ProvisionError("golden fixture has an invalid definition or dimensions shape")
    if not isinstance(fixture["metrics"], list) or not isinstance(fixture["chart_config"], dict):
        raise ProvisionError("golden fixture has an invalid metrics or chart_config shape")
    return fixture


def load_dashboard_configs(*, project_id: str) -> list[DashboardInput]:
    """Load all immutable configs and reject schema or fixture-version drift."""
    if not project_id:
        raise ProvisionError("a project id is required to load dashboard configurations")
    fixture = load_golden_fixture()
    configured: list[DashboardInput] = []
    for path in CONFIGURATION_PATHS:
        config = _read_json_object(path)
        if config.get("fixture_version") != fixture["fixture_version"]:
            raise ProvisionError(f"{path} is not derived from the current golden fixture version")
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
            )
            if any(field not in raw_widget for field in required):
                raise ProvisionError(f"{path} widget {index} omits a required field")
            view = raw_widget["view"]
            chart_type = raw_widget["chart_type"]
            chart_config = raw_widget["chart_config"]
            if not isinstance(view, str) or not isinstance(chart_type, str) or not isinstance(chart_config, dict):
                raise ProvisionError(f"{path} widget {index} has invalid view or chart configuration")
            if chart_config.get("type") != chart_type:
                raise ProvisionError(f"{path} widget {index} chart_config.type must equal chart_type")
            if not isinstance(raw_widget["denominator"], str) or not raw_widget["denominator"]:
                raise ProvisionError(f"{path} widget {index} silently omits its denominator")
            for field in ("name", "description"):
                if not isinstance(raw_widget[field], str) or not raw_widget[field]:
                    raise ProvisionError(f"{path} widget {index} has an invalid {field}")
            for field in ("dimensions", "metrics", "filters"):
                if not isinstance(raw_widget[field], list):
                    raise ProvisionError(f"{path} widget {index} has an invalid {field}")
            widget_inputs.append(
                WidgetInput(
                    name=raw_widget["name"],
                    description=raw_widget["description"],
                    view=view.lower(),
                    dimensions=raw_widget["dimensions"],
                    metrics=raw_widget["metrics"],
                    filters=raw_widget["filters"],
                    chart_type=chart_type,
                    chart_config=chart_config,
                )
            )
        configured.append(
            DashboardInput(
                project_id=project_id,
                name=name,
                description=description,
                widgets=tuple(widget_inputs),
            )
        )
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

    placements: list[dict[str, Any]] = []
    for index, widget in enumerate(dashboard.widgets):
        created_widget = client.create_widget(project_id=dashboard.project_id, widget=widget)
        widget_id = created_widget.get("id")
        if not isinstance(widget_id, str) or not widget_id:
            raise ProvisionError("dashboardWidgets.create did not return a widget id")
        placements.append(
            {
                "type": "widget",
                "id": f"pending-fixture-placement-{index}",
                "widgetId": widget_id,
                "x": 0,
                "y": index * 6,
                "x_size": 6,
                "y_size": 6,
            }
        )

    updated_dashboard = client.update_dashboard_definition(
        project_id=dashboard.project_id,
        dashboard_id=dashboard_id,
        definition={"widgets": placements},
    )
    if updated_dashboard.get("id") != dashboard_id:
        raise ProvisionError("dashboard.updateDashboardDefinition returned an unexpected dashboard")
    read_back = client.get_dashboard(project_id=dashboard.project_id, dashboard_id=dashboard_id)
    if read_back.get("id") != dashboard_id or not isinstance(read_back.get("definition"), dict):
        raise ProvisionError("app read-back did not return the created dashboard definition")
    return read_back


def run_direct_sql_fallback(*, allow_direct_sql: bool, write: Callable[[], None]) -> None:
    """Keep direct SQL unreachable until a fully guarded adapter is approved."""
    if not allow_direct_sql:
        raise ProvisionError("direct SQL is disabled; pass --allow-direct-sql explicitly")
    del write
    raise ProvisionError(
        "direct SQL has no approved guarded adapter; no connection or write was attempted"
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
                {"status": "fixture_ready", "changes": 0, "dashboard_count": len(dashboards)},
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
