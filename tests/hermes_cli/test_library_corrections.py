"""P6b — operator-bestätigtes Korrektur-Overlay (Store/Validierung/Audit).

Testet den profil-lokalen JSON-Store aus hermes_cli/library_corrections.py:
fail-closed confirm-Gate, Pflicht-Grund, Alias-/Weg-Validierung gegen die
P6a-Regelwelt, Merge-pro-Feld-Semantik, append-only History, unveränderlicher
Originalsnapshot und die reine Apply-/Preview-Mathematik. Kein Raten, keine
freien Werte; ein korrupter Store degradiert beim Lesen fail-soft und verweigert
verlustträchtige Schreibvorgänge.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import library_corrections as lc

ITEM_ID = "cron::main::16dd6ac01fc0::2026-06-10_07-31-09.md"


@pytest.fixture
def corrections_home(tmp_path, monkeypatch):
    """Isolierter HERMES_HOME — der Store liegt unter <home>/control/."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _provenance(
    producer: str = "Hermes-System",
    path: str = "Cron",
    **chain_overrides: str,
) -> dict:
    """Abgeleiteter P6a-Vertrag (Testbasis), Spiegel von _build_provenance."""
    chain = {
        "auftraggeber": "Unbekannt",
        "delegation": "Unbekannt",
        "autor": producer,
        "review": "Unbekannt",
        "ablage": "cron:16dd6ac01fc0",
    }
    chain.update(chain_overrides)
    evidenced = sum(1 for value in chain.values() if value != "Unbekannt")
    if evidenced == 0:
        status = "unknown"
    elif evidenced == len(chain):
        status = "evidenced"
    else:
        status = "partial"
    return {
        "producer": producer,
        "path": path,
        "status": status,
        "chain": chain,
        "refs": ["cron:16dd6ac01fc0"],
    }


# ---------------------------------------------------------------------------
# Store-Pfad + Validierung
# ---------------------------------------------------------------------------

def test_storage_path_is_profile_local(corrections_home):
    assert lc.storage_path() == (
        corrections_home / "control" / "library_provenance_corrections.json"
    )


def test_validate_item_id_accepts_known_kinds_and_rejects_others():
    assert lc.validate_item_id(ITEM_ID) == ITEM_ID
    assert lc.validate_item_id("research::t_abc") == "research::t_abc"
    assert lc.validate_item_id("deliverable::t_abc::rel/file.md")
    assert lc.validate_item_id("receipt::Hermes::x.md")
    for bad in ("", "task::x", "cron::" + "a" * 330, "cron::a b", None, 42):
        with pytest.raises(ValueError):
            lc.validate_item_id(bad)


def test_validate_fields_aliases_normalizes_and_rejects_free_values():
    # producer ist ein Alias für autor und läuft durch dieselbe Normalisierung.
    assert lc.validate_fields({"producer": "codex"}) == {"autor": "Codex"}
    assert lc.validate_fields({"path": "Task"}) == {"path": "Task"}
    # Leerer Wert = "Override entfernen" (None).
    assert lc.validate_fields({"autor": "   "}) == {"autor": None}
    with pytest.raises(ValueError):  # unbekanntes Feld
        lc.validate_fields({"serie": "X"})
    with pytest.raises(ValueError):  # freier Wegtyp verboten
        lc.validate_fields({"path": "E-Mail"})
    with pytest.raises(ValueError):  # leeres Dict
        lc.validate_fields({})
    with pytest.raises(ValueError):  # kein Objekt
        lc.validate_fields(["autor"])
    with pytest.raises(ValueError):  # Ablage gedeckelt
        lc.validate_fields({"ablage": "x" * 201})
    with pytest.raises(ValueError, match="string or null"):
        lc.validate_fields({"autor": 42})
    with pytest.raises(ValueError, match="too long"):
        lc.validate_fields({"review": "x" * 161})
    with pytest.raises(ValueError, match="must agree"):
        lc.validate_fields({"producer": "Codex", "autor": "Claude"})
    # Semantisch identische Aliaswerte bleiben erlaubt.
    assert lc.validate_fields({"producer": "codex", "autor": "Codex"}) == {
        "autor": "Codex",
    }
    # Unknown-Sentinels bleiben kanonisch exakt "Unbekannt"; sonst würde die
    # exakte Statuszählung lowercase-Werte fälschlich als belegt werten.
    for sentinel in ("unbekannt", "UNKNOWN", "none", "null", "-"):
        assert lc.validate_fields({"producer": sentinel}) == {
            "autor": "Unbekannt",
        }


def test_unknown_role_override_keeps_status_unproven_and_producer_canonical():
    fields = lc.validate_fields({"autor": "unknown"})
    effective, _ = lc.apply(
        _provenance(),
        {"item_id": ITEM_ID, "fields": fields, "original": _provenance()},
    )
    assert effective["producer"] == "Unbekannt"
    assert effective["chain"]["autor"] == "Unbekannt"
    assert effective["status"] == "partial"  # nur die technische Ablage belegt


# ---------------------------------------------------------------------------
# Bestätigte Mutation — fail-closed
# ---------------------------------------------------------------------------

def test_set_fail_closed_without_confirm(corrections_home):
    for bad_confirm in (False, None, 0, "true"):
        with pytest.raises(ValueError, match="confirm"):
            lc.set_correction(
                ITEM_ID, {"autor": "Codex"}, "Grund", confirm=bad_confirm,
            )
    assert lc.read(ITEM_ID) is None  # nichts geschrieben
    assert lc.load_active() == {}


def test_set_requires_reason(corrections_home):
    for bad_reason in ("", "   ", None):
        with pytest.raises(ValueError, match="reason"):
            lc.set_correction(ITEM_ID, {"autor": "Codex"}, bad_reason, confirm=True)
    with pytest.raises(ValueError, match="reason"):
        lc.set_correction(ITEM_ID, {"autor": "Codex"}, "x" * 601, confirm=True)
    assert lc.read(ITEM_ID) is None


def test_set_creates_record_with_original_snapshot_and_history(corrections_home):
    derived = _provenance()
    record = lc.set_correction(
        ITEM_ID, {"autor": "codex", "path": "Task"}, "Falsche Attribution",
        confirm=True, derived_provenance=derived,
    )
    assert record["item_id"] == ITEM_ID
    assert record["fields"] == {"autor": "Codex", "path": "Task"}
    assert record["actor"] == lc.ACTOR_OPERATOR
    assert record["reason"] == "Falsche Attribution"
    assert record["created_at"] > 0
    assert record["updated_at"] >= record["created_at"]
    # Originalsnapshot = der ABGELEITETE Vertrag vor der Korrektur.
    assert record["original"]["producer"] == "Hermes-System"
    assert record["original"]["path"] == "Cron"
    assert record["original"]["chain"]["autor"] == "Hermes-System"
    assert len(record["history"]) == 1
    assert record["history"][0]["action"] == "set"
    assert record["history"][0]["fields"] == {"autor": "Codex", "path": "Task"}
    # Persistiert und lesbar.
    assert lc.read(ITEM_ID)["fields"] == {"autor": "Codex", "path": "Task"}


def test_original_snapshot_is_immutable_across_updates(corrections_home):
    lc.set_correction(
        ITEM_ID, {"autor": "Codex"}, "Erstkorrektur",
        confirm=True, derived_provenance=_provenance(),
    )
    # Zweites Set mit ANDEREM abgeleiteten Vertrag darf den Originalsnapshot
    # nicht fortschreiben (er belegt den Zustand VOR der ersten Korrektur).
    record = lc.set_correction(
        ITEM_ID, {"review": "Claude"}, "Nachbesserung",
        confirm=True, derived_provenance=_provenance(producer="Qwen"),
    )
    assert record["original"]["producer"] == "Hermes-System"
    assert record["original"]["chain"]["autor"] == "Hermes-System"


def test_set_merges_per_field_and_empty_value_removes_field(corrections_home):
    lc.set_correction(
        ITEM_ID, {"autor": "Codex", "path": "Task"}, "Korrektur 1",
        confirm=True, derived_provenance=_provenance(),
    )
    record = lc.set_correction(
        ITEM_ID, {"autor": "", "review": "Claude"}, "Korrektur 2",
        confirm=True, derived_provenance=_provenance(),
    )
    # autor entfernt (leer), path behält den Override, review neu.
    assert record["fields"] == {"path": "Task", "review": "Claude"}
    assert [h["action"] for h in record["history"]] == ["set", "set"]


# ---------------------------------------------------------------------------
# Revert — fail-closed, append-only Audit
# ---------------------------------------------------------------------------

def test_revert_fail_closed_without_confirm(corrections_home):
    lc.set_correction(
        ITEM_ID, {"autor": "Codex"}, "Korrektur",
        confirm=True, derived_provenance=_provenance(),
    )
    with pytest.raises(ValueError, match="confirm"):
        lc.revert(ITEM_ID, "Rücknahme", confirm=False)
    assert lc.read(ITEM_ID)["fields"] == {"autor": "Codex"}  # unverändert


def test_revert_on_never_corrected_item_returns_none(corrections_home):
    assert lc.revert(ITEM_ID, "Rücknahme", confirm=True) is None


def test_revert_per_field_and_whole_keep_append_only_history(corrections_home):
    lc.set_correction(
        ITEM_ID, {"autor": "Codex", "path": "Task", "review": "Claude"},
        "Korrektur", confirm=True, derived_provenance=_provenance(),
    )
    record = lc.revert(
        ITEM_ID, "Nur Review war falsch", fields=["review"], confirm=True,
    )
    assert record is not None
    assert record["fields"] == {"autor": "Codex", "path": "Task"}
    record = lc.revert(ITEM_ID, "Komplett zurücknehmen", confirm=True)
    assert record["fields"] == {}
    # Record bleibt für das Audit erhalten; History ist append-only.
    assert lc.read(ITEM_ID) is not None
    actions = [h["action"] for h in record["history"]]
    assert actions == ["set", "revert", "revert"]
    assert record["history"][-1]["reason"] == "Komplett zurücknehmen"


def test_revert_rejects_empty_explicit_field_list(corrections_home):
    lc.set_correction(
        ITEM_ID, {"autor": "Codex"}, "Korrektur",
        confirm=True, derived_provenance=_provenance(),
    )
    with pytest.raises(ValueError, match="fields must not be empty"):
        lc.revert(ITEM_ID, "Keine Auswahl", fields=[], confirm=True)
    assert lc.read(ITEM_ID)["fields"] == {"autor": "Codex"}


def test_load_active_only_returns_records_with_active_fields(corrections_home):
    lc.set_correction(
        ITEM_ID, {"autor": "Codex"}, "Korrektur",
        confirm=True, derived_provenance=_provenance(),
    )
    other = "research::t_abc"
    lc.set_correction(
        other, {"delegation": "Kimi"}, "Korrektur 2",
        confirm=True, derived_provenance=_provenance(producer="Unbekannt"),
    )
    lc.revert(other, "Irrtum", confirm=True)
    active = lc.load_active()
    assert set(active) == {ITEM_ID}  # zurückgenommener Record nicht aktiv


# ---------------------------------------------------------------------------
# Reine Apply-/Preview-Mathematik
# ---------------------------------------------------------------------------

def test_apply_overrides_effective_contract_and_adds_block(corrections_home):
    record = lc.set_correction(
        ITEM_ID,
        {"autor": "Codex", "auftraggeber": "Piet", "delegation": "Kimi",
         "review": "Claude", "path": "Task"},
        "Vollständig korrigiert", confirm=True, derived_provenance=_provenance(),
    )
    effective, block = lc.apply(_provenance(), record)
    assert effective["producer"] == "Codex"  # Erzeuger folgt der Autor-Rolle
    assert effective["path"] == "Task"
    assert effective["chain"]["auftraggeber"] == "Piet"
    assert effective["chain"]["ablage"] == "cron:16dd6ac01fc0"  # unangetastet
    # Alle fünf Rollen belegt → Status steigt von partial auf evidenced.
    assert effective["status"] == "evidenced"
    assert block is not None
    assert block["active"] is True
    assert block["item_id"] == ITEM_ID
    assert block["original"]["producer"] == "Hermes-System"
    assert block["fields"]["autor"] == "Codex"
    assert block["actor"] == lc.ACTOR_OPERATOR
    assert isinstance(block["history"], list) and block["history"]


def test_apply_exposes_current_derived_separately_from_immutable_original(
    corrections_home,
):
    first_derived = _provenance(delegation="Hermes-System")
    record = lc.set_correction(
        ITEM_ID, {"path": "Task"}, "Weg korrigiert", confirm=True,
        derived_provenance=first_derived,
    )
    current_derived = _provenance(delegation="Qwen")
    effective, block = lc.apply(current_derived, record)
    assert block is not None
    assert block["original"]["chain"]["delegation"] == "Hermes-System"
    assert block["derived"]["chain"]["delegation"] == "Qwen"
    assert effective["chain"]["delegation"] == "Qwen"
    # Der aktuelle Zusatz ist Response-Metadatum, kein Persistenz-Touch.
    assert "derived" not in lc.read(ITEM_ID)


def test_apply_without_record_is_identity():
    provenance = _provenance()
    effective, block = lc.apply(provenance, None)
    assert effective == provenance
    assert block is None


def test_apply_ignores_inactive_record(corrections_home):
    record = {"item_id": ITEM_ID, "fields": {}, "original": {}}
    effective, block = lc.apply(_provenance(), record)
    assert block is None  # leere Felder = kein Overlay


def test_preview_is_pure_and_never_persists(corrections_home):
    effective = lc.preview(
        _provenance(), {"autor": "Codex", "path": "Task", "review": None},
    )
    assert effective["producer"] == "Codex"
    assert effective["path"] == "Task"
    assert lc.read(ITEM_ID) is None  # Vorschau schreibt nichts
    assert lc.load_active() == {}


def test_preview_uses_set_merge_semantics_without_persisting(corrections_home):
    record = lc.set_correction(
        ITEM_ID, {"path": "Task", "autor": "Codex"}, "Bestehend",
        confirm=True, derived_provenance=_provenance(),
    )
    before = lc.read(ITEM_ID)
    effective = lc.preview(
        _provenance(), {"review": "Claude", "autor": None}, record,
    )
    assert effective["path"] == "Task"  # disjunkter Override bleibt
    assert effective["producer"] == "Hermes-System"  # autor entfernt
    assert effective["chain"]["review"] == "Claude"
    assert lc.read(ITEM_ID) == before


# ---------------------------------------------------------------------------
# Fail-soft bei korruptem Store
# ---------------------------------------------------------------------------

def test_corrupt_store_is_fail_soft_but_write_refuses_data_loss(corrections_home):
    lc.storage_path().parent.mkdir(parents=True, exist_ok=True)
    corrupt = "{ not json"
    lc.storage_path().write_text(corrupt, encoding="utf-8")
    assert lc.load_active() == {}  # kein 500, leere Bibliothek bleibt heil
    assert lc.read(ITEM_ID) is None
    # Eine Mutation darf den unlesbaren Store samt fremder Historie nicht
    # stillschweigend ersetzen. Reparatur/Quarantäne ist ein separater Akt.
    with pytest.raises(lc.CorrectionStoreError, match="write refused"):
        lc.set_correction(
            ITEM_ID, {"autor": "Codex"}, "Reparatur",
            confirm=True, derived_provenance=_provenance(),
        )
    assert lc.storage_path().read_text(encoding="utf-8") == corrupt


def test_malformed_state_entries_are_dropped(corrections_home):
    lc.storage_path().parent.mkdir(parents=True, exist_ok=True)
    lc.storage_path().write_text(json.dumps({
        "version": 1,
        "corrections": {"cron::main::a::b.md": "not-a-dict", 5: {}},
    }), encoding="utf-8")
    assert lc.load_active() == {}


def test_unknown_store_version_fails_soft(corrections_home):
    lc.storage_path().parent.mkdir(parents=True, exist_ok=True)
    lc.storage_path().write_text(json.dumps({
        "version": 999,
        "corrections": {ITEM_ID: {"fields": {"autor": "Codex"}}},
    }), encoding="utf-8")
    assert lc.load_active() == {}
    with pytest.raises(lc.CorrectionStoreError, match="write refused"):
        lc.set_correction(
            ITEM_ID, {"autor": "Claude"}, "Nicht überschreiben",
            confirm=True, derived_provenance=_provenance(),
        )
