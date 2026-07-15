"""Declarative ownership for Kanban dashboard routes.

The plugin still exposes one ``APIRouter`` to the dashboard loader. Route
namespaces are lightweight decorator proxies: they preserve declaration order
on that public router while recording whether a handler belongs to the stable
upstream core or to a local extension edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter


@dataclass(frozen=True)
class RouteRecord:
    owner: str
    method: str
    path: str

    @property
    def key(self) -> tuple[str, str]:
        return self.method, self.path


class RouteNamespace:
    """Register routes on the shared router and stamp one explicit owner."""

    def __init__(self, contract: "DashboardRouteContract", owner: str) -> None:
        self._contract = contract
        self.owner = owner

    def _decorator(
        self,
        method: str,
        registrar: Callable[..., Callable],
        path: str,
        *args: Any,
        **kwargs: Any,
    ) -> Callable:
        decorate = registrar(path, *args, **kwargs)

        def register(endpoint: Callable) -> Callable:
            registered = decorate(endpoint)
            self._contract.records.append(RouteRecord(self.owner, method, path))
            return registered

        return register

    def get(self, path: str, *args: Any, **kwargs: Any) -> Callable:
        return self._decorator("GET", self._contract.router.get, path, *args, **kwargs)

    def post(self, path: str, *args: Any, **kwargs: Any) -> Callable:
        return self._decorator("POST", self._contract.router.post, path, *args, **kwargs)

    def put(self, path: str, *args: Any, **kwargs: Any) -> Callable:
        return self._decorator("PUT", self._contract.router.put, path, *args, **kwargs)

    def patch(self, path: str, *args: Any, **kwargs: Any) -> Callable:
        return self._decorator("PATCH", self._contract.router.patch, path, *args, **kwargs)

    def delete(self, path: str, *args: Any, **kwargs: Any) -> Callable:
        return self._decorator(
            "DELETE", self._contract.router.delete, path, *args, **kwargs
        )

    def websocket(self, path: str, *args: Any, **kwargs: Any) -> Callable:
        return self._decorator(
            "WEBSOCKET", self._contract.router.websocket, path, *args, **kwargs
        )


class DashboardRouteContract:
    """One public router plus auditable core/edge ownership metadata."""

    def __init__(self) -> None:
        self.router = APIRouter()
        self.records: list[RouteRecord] = []
        self._namespaces: dict[str, RouteNamespace] = {}

    def namespace(self, owner: str) -> RouteNamespace:
        normalized = str(owner or "").strip()
        if not normalized:
            raise ValueError("route namespace owner must not be empty")
        namespace = self._namespaces.get(normalized)
        if namespace is None:
            namespace = RouteNamespace(self, normalized)
            self._namespaces[normalized] = namespace
        return namespace

    def route_keys(self, owner: str) -> set[tuple[str, str]]:
        return {record.key for record in self.records if record.owner == owner}

    def owner_by_key(self) -> dict[tuple[str, str], str]:
        return {record.key: record.owner for record in self.records}

