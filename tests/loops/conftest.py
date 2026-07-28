"""Loop-Tests vom Live-Provider-Inventory entkoppeln.

Der Modell-Katalog des Loop-Runners ergänzt die kuratierte ``models.yaml`` seit
2026-07-28 dynamisch um alles, was ``hermes_cli.inventory`` kennt. Ohne diese
Fixture würde jeder Test die echten Provider dieses Hosts proben: die Suite
ging dadurch von 8s auf 334s hoch, und ein Testergebnis hinge davon ab, welche
Abos gerade konfiguriert sind.

Tests, die den dynamischen Pfad prüfen WOLLEN, überschreiben
``_build_inventory_payload`` selbst mit einem festen Fake-Payload.
"""

from __future__ import annotations

import pytest

from loops import model_catalog


@pytest.fixture(autouse=True)
def _hermetic_model_catalog(monkeypatch):
    monkeypatch.setattr(model_catalog, "_build_inventory_payload", lambda: {})
    monkeypatch.setattr(model_catalog, "_claude_cli_models", list)
    monkeypatch.setattr(model_catalog, "_hermes_profiles", list)
    model_catalog.reset_inventory_cache()
    yield
    model_catalog.reset_inventory_cache()
