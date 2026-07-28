"""Dynamischer Modell-Katalog für den Loop-Runner.

Bis 2026-07-28 kannte der Loops-Tab nur die ~30 handgepflegten Einträge aus
``loops/models.yaml``, während der Lanes-Tab den vollen Provider-Katalog anbot.
Ein dort wählbares Modell ließ sich im Loops-Tab nicht einstellen — und der
fail-closed Membership-Check wies es beim Start zusätzlich ab.

Kern der Lösung: die Engine→Provider-Zuordnung wird ABGELEITET (Namens-
verwandtschaft) oder als Daten deklariert (``inventory_providers`` in
models.yaml), nie in Python gepflegt. Ein neu konfigurierter Provider oder ein
neues Modell erscheint damit ohne Codeänderung.
"""

from __future__ import annotations

import pytest

from loops import model_catalog
from loops.model_catalog import (
    _slug_matches_engine,
    catalog_with_dynamic_models,
    models_for_engine,
)

FAKE_INVENTORY = {
    "providers": [
        {"slug": "openai-codex", "models": ["gpt-5.6-sol", "gpt-5.9-neu", "gpt-5.6-sol-pro"]},
        {"slug": "kimi-coding", "models": ["kimi-k3"]},
        {"slug": "kimi-coding-cn", "models": ["kimi-k3-cn"]},
        {"slug": "xai-oauth", "models": ["grok-4.5", "grok-neu"]},
        {"slug": "xiaomi", "models": ["mimo-7b"]},
        {"slug": "neuralwatt", "models": ["glm-5.2", "glm-neu"]},
        {"slug": "alibaba-token-plan", "models": ["qwen3.9", "qwen-image-2.0", "wan2.7-image"]},
        {"slug": "openrouter", "models": ["anthropic/claude-opus-5"]},
    ]
}


@pytest.fixture
def inventory(monkeypatch):
    monkeypatch.setattr(model_catalog, "_build_inventory_payload", lambda: FAKE_INVENTORY)
    model_catalog.reset_inventory_cache()
    yield
    model_catalog.reset_inventory_cache()


# --- Namensverwandtschaft statt gepflegter Map ----------------------------


@pytest.mark.parametrize(
    ("engine", "slug", "expected"),
    [
        ("kimi", "kimi-coding", True),
        ("kimi", "kimi-coding-cn", True),      # neuer Zwilling: automatisch drin
        ("xai", "xai-oauth", True),
        ("neuralwatt", "neuralwatt", True),
        ("alibaba-token-plan", "alibaba-token-plan", True),
        # Segmentgrenze: sonst finge "xai" auch "xiaomi".
        ("xai", "xiaomi", False),
        ("kimi", "kilocode", False),
        ("codex", "openai-codex", False),      # keine Verwandtschaft → Daten nötig
    ],
)
def test_slug_matching_is_segment_bound(engine: str, slug: str, expected: bool):
    assert _slug_matches_engine(engine, slug) is expected


def test_new_provider_of_a_known_family_appears_without_a_code_change(inventory):
    """Der eigentliche Punkt: kimi-coding-cn steht in KEINER Liste im Code."""
    models = models_for_engine("kimi", curated=["k3"])
    assert "kimi-k3-cn" in models


def test_new_model_of_a_known_provider_appears_automatically(inventory):
    assert "gpt-5.9-neu" in models_for_engine("codex", [], ["openai-codex"])
    assert "grok-neu" in models_for_engine("xai", [])
    assert "glm-neu" in models_for_engine("neuralwatt", [])


def test_declared_provider_covers_a_non_matching_name(inventory):
    """codex ↔ openai-codex ist Produktwissen, keine Namensverwandtschaft —
    es kommt deshalb als Daten aus models.yaml, nicht aus dem Code."""
    assert models_for_engine("codex", [], []) == []
    assert "gpt-5.6-sol" in models_for_engine("codex", [], ["openai-codex"])


def test_unrelated_providers_do_not_leak_into_an_engine(inventory):
    """Ein `claude -p` kann kein OpenRouter-Modell fahren."""
    for engine in ("kimi", "xai", "neuralwatt"):
        assert "anthropic/claude-opus-5" not in models_for_engine(engine, [])
    assert "mimo-7b" not in models_for_engine("xai", [])


# --- Ausschlüsse ----------------------------------------------------------


def test_image_and_video_generators_are_not_offered(inventory):
    """Sie können einen Prompt nicht beantworten — ein Loop mit ihnen
    produziert nur Fehlrunden (Lanes-Verifikation 2026-07-24)."""
    models = models_for_engine("alibaba-token-plan", [])
    assert "qwen3.9" in models
    assert "qwen-image-2.0" not in models
    assert "wan2.7-image" not in models


def test_codex_pro_ids_are_not_offered(inventory):
    """Der Codex-Endpoint serviert sie nicht direkt; ein Aufruf fällt still auf
    ein anderes Modell zurück — der Loop liefe unbemerkt anders als angezeigt."""
    assert "gpt-5.6-sol-pro" not in models_for_engine("codex", [], ["openai-codex"])


# --- Reihenfolge, Dedup, Robustheit --------------------------------------


def test_curated_models_stay_in_front(inventory):
    """Die bewährten Default-Routen bleiben oben im Dropdown."""
    models = models_for_engine("xai", curated=["grok-4.5"])
    assert models[0] == "grok-4.5"
    assert models.count("grok-4.5") == 1  # dedupliziert trotz Inventory-Treffer


def test_broken_inventory_falls_back_to_the_curated_list(monkeypatch):
    """Der Runner läuft als eigene systemd-Unit und darf nicht sterben, nur
    weil ein Provider-Probe hakt — er fällt auf die kuratierte Liste zurück."""
    import hermes_cli.inventory as inventory_module

    def boom(*_a, **_kw):
        raise RuntimeError("Provider-Probe kaputt")

    monkeypatch.setattr(inventory_module, "load_picker_context", boom)
    model_catalog.reset_inventory_cache()
    # Kein Absturz, und die kuratierte Route bleibt wählbar.
    assert model_catalog._build_inventory_payload() == {}
    assert models_for_engine("xai", curated=["grok-4.5"]) == ["grok-4.5"]


def test_catalog_with_dynamic_models_keeps_other_entry_fields(inventory):
    out = catalog_with_dynamic_models(
        {"xai": {"label": "xAI / Grok (Abo)", "models": ["grok-4.5"]}}
    )
    assert out["xai"]["label"] == "xAI / Grok (Abo)"
    assert "grok-neu" in out["xai"]["models"]


def test_inventory_is_cached_between_calls(monkeypatch):
    """Ungecacht war ein Pack-Listing eine messbare Bremse (Suite 8s → 334s)."""
    calls = []

    def counted():
        calls.append(1)
        return FAKE_INVENTORY

    monkeypatch.setattr(model_catalog, "_build_inventory_payload", counted)
    model_catalog.reset_inventory_cache()
    for _ in range(5):
        models_for_engine("xai", [])
    assert len(calls) == 1
    model_catalog.reset_inventory_cache()
    models_for_engine("xai", [])
    assert len(calls) == 2
