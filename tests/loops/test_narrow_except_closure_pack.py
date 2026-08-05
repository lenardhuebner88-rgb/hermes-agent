"""Vertragstests für den siteweiten Pack ``narrow-except-closure``.

Der Pack darf die belegte Fehlerklasse nicht in einzelne Nachtfunde zerlegen und
seinen eigenen Such- oder Testmaßstab nicht abschwächen.
"""

from __future__ import annotations

import re

import pytest

from loops.runner import PACKS_DIR, load_pack

PACK_NAME = "narrow-except-closure"


@pytest.fixture(scope="module")
def pack():
    return load_pack(PACKS_DIR, PACK_NAME)


@pytest.fixture(scope="module")
def prompt(pack) -> str:
    return (pack.pack_dir / pack.phases["round"].prompt).read_text(encoding="utf-8")


def test_manifest_loads_as_one_bounded_sweep_round(pack):
    assert pack.type == "sweep"
    assert list(pack.phases) == ["round"]
    assert pack.repo.is_dir(), f"Pack-Repo existiert nicht: {pack.repo}"
    assert pack.phases["round"].timeout >= 600
    assert pack.stop["max_rounds"] == 1
    assert pack.autoland is False


def test_prompt_closes_the_complete_remaining_set_in_one_round(prompt):
    """AC-S2-1: Eine Fehlerklasse wird sitewide statt instanzweise geschlossen."""
    assert "GESAMTE Restmenge" in prompt
    assert "in EINER Runde" in prompt
    assert "mehrere Stellen" in prompt
    assert "EINEM Commit" in prompt
    assert "eine Instanz pro Nacht" in prompt
    assert "GENAU EINEN Fund" not in prompt


def test_prompt_uses_rg_before_and_after_with_the_same_measure(prompt):
    """Die vollständige Restmenge wird mechanisch erhoben und nachgemessen."""
    commands = re.findall(r"^\s{4}(rg -n --glob '\*\.py'.+)$", prompt, re.MULTILINE)
    assert commands, "ein konkretes siteweites rg-Kommando fehlt"
    assert len(set(commands)) == 1, "Vorher/Nachher müssen dasselbe rg-Kommando nutzen"
    assert len(commands) >= 2, "die identische Suche muss vor und nach dem Fix laufen"
    assert "vor der ersten Änderung" in prompt
    assert "nach dem Fix" in prompt


def test_only_handlers_whose_try_block_contains_a_real_read_path_are_changed(prompt):
    """AC-S2-2: Enge OSError-Handler ohne Read bleiben unverändert."""
    assert "try-Block" in prompt
    assert "wirklich ein Lesepfad" in prompt
    assert "ohne Lesepfad bleibt unverändert" in prompt
    for excluded in ("Schreibpfade", "mkdir", "unlink", "subprocess"):
        assert excluded in prompt


def test_every_changed_site_requires_its_own_red_reproduction(prompt):
    assert "Jede geänderte Stelle" in prompt
    assert "eigenen Repro-Test" in prompt
    assert "UNVERÄNDERTEN Produktionscode" in prompt
    assert "rot" in prompt
    assert "UnicodeDecodeError" in prompt


def test_optimizer_cannot_weaken_its_own_contract(prompt):
    """AC-S2-3: Suchmaßstab und Repro-Beweise sind immutable Invarianten."""
    assert "Maßstab nicht abschwächen" in prompt
    assert "Suchkommando nicht ändern" in prompt
    assert "ROUND-PROMPT.md" in prompt
    assert "pack.yaml" in prompt
    assert "test_narrow_except_closure_pack.py" in prompt
    assert "keine Assertion" in prompt
    assert "keinen Test löschen" in prompt


def test_prompt_requires_targeted_green_gate_and_forbids_destructive_actions(prompt):
    assert "scripts/run_tests.sh" in prompt
    verbote = prompt[prompt.index("## Verbote") :]
    for forbidden in ("push", "merge", "deploy", "Vollsuite", "git stash", "git clean"):
        assert forbidden in verbote


def test_prompt_placeholders_are_known(pack, prompt):
    known = {
        "{{WT}}",
        "{{STATE_DIR}}",
        "{{PARAMS}}",
        "{{HAS_WEB}}",
        "{{PLAN_PATH}}",
        "{{RANGE}}",
        "{{BUILD_PROVENANCE}}",
    }
    found = set(re.findall(r"{{[^{}]+}}", prompt))
    assert found <= known, f"unbekannte Platzhalter: {sorted(found - known)}"
    assert {"{{WT}}", "{{STATE_DIR}}"} <= found
