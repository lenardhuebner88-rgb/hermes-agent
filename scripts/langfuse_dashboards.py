#!/usr/bin/env python3
"""Safely call the Langfuse 3.224.0 dashboard tRPC procedures.

This module deliberately has no ambient credential, database, or fixture discovery.
A caller must inject an already-authorised browser-session transport and a
versioned UI export before it can create a dashboard.  That keeps dry-runs and
tests hermetic and prevents accidental PostgreSQL fallback writes.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class ProvisionError(RuntimeError):
    """Raised when a version-pinned Langfuse dashboard contract is violated."""


@dataclass(frozen=True)
class WidgetInput:
    """A widget payload derived from a future, versioned UI-export fixture."""

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


class TrpcClient:
    """Small, injectable tRPC client for Langfuse revision d044f366.

    Authentication is intentionally delegated to ``opener``.  The production
    runner may only inject an opener backed by an already human-authenticated
    NextAuth browser session; this module never reads or prints credentials.
    """

    def __init__(
        self,
        base_url: str,
        *,
        opener: Any | None = None,
        timeout: float = 10.0,
    ) -> None:
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
            {
                "projectId": project_id,
                "dashboardId": dashboard_id,
                "definition": dict(definition),
            },
        )

    def get_dashboard(self, *, project_id: str, dashboard_id: str) -> dict[str, Any]:
        return self._post(
            "dashboard.getDashboard",
            {"projectId": project_id, "dashboardId": dashboard_id},
        )


def provision_dashboard(client: TrpcClient, dashboard: DashboardInput) -> dict[str, Any]:
    """Create widgets, attach them, then verify the app's read-back response."""
    created_dashboard = client.create_dashboard(
        project_id=dashboard.project_id,
        name=dashboard.name,
        description=dashboard.description,
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
                "y": index * 4,
                "x_size": 12,
                "y_size": 4,
            }
        )

    client.update_dashboard_definition(
        project_id=dashboard.project_id,
        dashboard_id=dashboard_id,
        definition={"widgets": placements},
    )
    read_back = client.get_dashboard(project_id=dashboard.project_id, dashboard_id=dashboard_id)
    if read_back.get("id") != dashboard_id or not isinstance(read_back.get("definition"), dict):
        raise ProvisionError("app read-back did not return the created dashboard definition")
    return read_back


def run_direct_sql_fallback(*, allow_direct_sql: bool, write: Callable[[], None]) -> None:
    """Keep direct SQL physically unreachable unless an explicit caller opts in.

    The guarded adapter and UI-export fixture are intentionally not supplied
    until the authorised human creation establishes the required golden data.
    """
    if not allow_direct_sql:
        raise ProvisionError("direct SQL is disabled; pass --allow-direct-sql explicitly")
    write()


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report fixture readiness only; never creates a transport or performs a write.",
    )
    parser.add_argument(
        "--allow-direct-sql",
        action="store_true",
        help="Reserved for the separately guarded fallback adapter; has no effect without it.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    if args.dry_run:
        print(json.dumps({"status": "requires_golden_fixture", "changes": 0}, sort_keys=True))
        return 0
    raise ProvisionError(
        "no authorised browser-session adapter or UI-export golden fixture is configured; "
        "perform the one-time human UI creation and supply its sanitised export first"
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProvisionError as exc:
        print(f"langfuse-dashboard-provisioning: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
