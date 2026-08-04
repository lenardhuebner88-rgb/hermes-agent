"""Vertragstests für das Pack ``silent-failure-sweep``.

Ein Pack ist überwiegend Prosa, und Prosa driftet lautlos: eine gestrichene
Zeile im ROUND-PROMPT ändert das Verhalten des Nachtlaufs, ohne dass irgendein
Gate anschlägt. Jeder Test hier sichert genau eine Schiene, deren Verlust einen
in diesem Repo bereits bezahlten Vorfall wiederholen würde — die historische
Quelle steht jeweils im Docstring.

Präzedenz für dieses Test-Muster: die Guard-Tests in ``web/src/control/styles/``
und ``scripts/check_kanban_lifecycle_anchors.py``.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from loops.runner import PACKS_DIR, load_pack

PACK_NAME = "silent-failure-sweep"


@pytest.fixture(scope="module")
def pack():
    return load_pack(PACKS_DIR, PACK_NAME)


@pytest.fixture(scope="module")
def prompt(pack) -> str:
    return (pack.pack_dir / pack.phases["round"].prompt).read_text(encoding="utf-8")


def test_manifest_loads_as_a_single_round_sweep(pack):
    """Ein kaputtes Manifest fällt sonst erst beim Nachtstart auf.

    xai-hard-gate und loops-date-audit sahen im Dashboard normal aus und starben
    erst beim Start am nicht mehr existierenden Repo-Pfad (28.07.).
    """
    assert pack.type == "sweep"
    assert list(pack.phases) == ["round"]
    assert pack.repo.is_dir(), f"Pack-Repo existiert nicht: {pack.repo}"
    assert pack.phases["round"].timeout >= 600


def test_autoland_stays_off(pack):
    """Auto-Landung bleibt Operator-Entscheidung (v2-Leiter).

    Ein Sweep, der seine eigenen Fixes ungeprüft nach main pusht, umgeht die
    Morgen-Review — und dieser Pack fasst per Auftrag Fehlerbehandlungspfade an.
    """
    assert pack.autoland is False


def test_stop_rails_bound_the_night(pack):
    """Ohne Abbruchschienen läuft ein toter Loop die Nacht durch.

    error-sweep produzierte 18 DRY-Runden hintereinander, bevor er retired wurde.
    """
    for key in ("max_rounds", "max_hours", "fail_streak", "dry_rounds"):
        assert pack.stop.get(key), f"stop.{key} fehlt oder ist 0"
    assert pack.stop["dry_rounds"] <= 3, "DRY-Serie muss den Lauf früh beenden"


def test_prompt_demands_the_red_proof_before_the_fix(prompt):
    """Die Anti-Placebo-Schiene.

    Grüne Gates beweisen keinen Fix: ein Fix ohne vorher rot gemessenen Test hat
    hier schon einen Bug „behoben", den es nie gab (Placebo-Fix + tautologischer
    Test). Der Prompt muss den roten Messpunkt VOR dem Fix verlangen und den
    grünen Vorher-Test ausdrücklich als Nicht-Fund werten.
    """
    assert "RED-Beweis" in prompt
    assert "UNVERÄNDERTEN Code" in prompt
    assert "grün, bevor du irgendetwas fixt" in prompt
    assert "KEIN Bug" in prompt


def test_prompt_forbids_stash_and_clean(prompt):
    """Zwei Werkzeuge, die in diesem Checkout fremde Arbeit zerstören.

    ``git stash pop`` nimmt in einem geteilten Checkout den FREMDEN Stash;
    ``git clean`` ist selbst headless durch den Guard-Hook gesperrt — nur der
    Loop-Driver räumt.
    """
    assert "git stash" in prompt
    assert "git clean" in prompt
    verbote = prompt[prompt.index("## Verbote"):]
    assert "git clean" in verbote and "git stash" in verbote


def test_prompt_requires_ast_and_loud_unreadable_files(prompt):
    """grep zählt diese Fundklasse falsch, und ein unlesbares File blendet die Suche.

    Ein Monkeypatch-Sweep fand per grep 0 statt 470 Treffer; eine einzige
    BOM-Datei ließ AST-Tooling „0 Symbole" melden statt einen Fehler.
    """
    assert "per AST, nicht per grep" in prompt
    assert "ast.parse" in prompt
    assert "LAUT" in prompt, "unlesbare Dateien müssen laut gemeldet werden"
    assert "UNLESBAR" in prompt


def test_escalation_target_is_actually_read_by_the_dashboard(prompt):
    """Ein Fund, der nur im Ledger steht, ist ein toter Fund.

    Der Prompt darf ESCALATIONS.md nicht nur beauftragen — die Datei muss auch
    wirklich im Loops-Tab landen. hermes-hardening trug 137 Zeilen echter Funde,
    die niemand las, weil das Dashboard die Datei nicht kannte. Dieser Test
    prüft beide Enden, damit sie nicht wieder auseinanderlaufen.
    """
    assert "ESCALATIONS.md" in prompt

    from hermes_cli import control_loops

    source = inspect.getsource(control_loops)
    assert "ESCALATIONS.md" in source, "Backend liest die Eskalationen nicht"
    assert "escalations_tail" in source

    schema = Path(control_loops.__file__).parents[1] / (
        "web/src/control/lib/schemas/loops.ts"
    )
    # zod strippt jedes Feld, das im Schema fehlt — genau so erreichte das
    # `error`-Feld der Loops-API die UI nie (Eskalation E4).
    assert "escalations_tail" in schema.read_text(encoding="utf-8")


def test_prompt_keeps_the_targeted_test_scope_and_unpiped_gate(prompt):
    """Zwei Regeln, die je einmal einen ganzen Lauf gekostet haben.

    Die Vollsuite ist nachts reserviert (Test-Scope-Regel); und ein Gate in einer
    Pipe meldet ohne ``pipefail`` Exit 0, obwohl es rot ist — am 28.07. wurde so
    zweimal ein rotes Gate als grün berichtet.
    """
    assert "run-affected.sh" in prompt
    assert "Vollsuite" in prompt[prompt.index("## Verbote"):]
    assert "pipefail" in prompt
    assert "Exit-Code direkt" in prompt


def test_finding_definition_is_a_conjunction_not_a_vibe(prompt):
    """Ohne scharfe Definition wird der Sweep ein Rauschgenerator.

    Im Repo stehen 2.528 still schluckende Handler; die grosse Mehrheit ist
    bewusste Degradation MIT Fehlerkanal. Ohne die Vier-Punkte-Konjunktion und
    die ausdrückliche Nicht-Fund-Liste plant der Loop legitime Stellen um.
    """
    assert "alle vier Punkte" in prompt
    for punkt in ("Erreichbar", "Ununterscheidbar", "Plausibel", "Folgenreich"):
        assert f"**{punkt}.**" in prompt
    assert "**Kein Fund**" in prompt
    assert "DRY" in prompt


def test_prompt_placeholders_all_resolve(pack, prompt):
    """Ein Tippfehler im Platzhalter erreicht den Agenten als roher Text.

    Der Runner ersetzt nur die Platzhalter, die er kennt; ein unbekannter bleibt
    wörtlich stehen und der Agent liest '{{STATEDIR}}' als Pfad.
    """
    known = {"{{WT}}", "{{STATE_DIR}}", "{{PARAMS}}", "{{HAS_WEB}}",
             "{{PLAN_PATH}}", "{{RANGE}}", "{{BUILD_PROVENANCE}}"}
    found = set()
    rest = prompt
    while "{{" in rest:
        start = rest.index("{{")
        end = rest.index("}}", start) + 2
        found.add(rest[start:end])
        rest = rest[end:]
    assert found <= known, f"unbekannte Platzhalter: {sorted(found - known)}"
    assert "{{STATE_DIR}}" in found and "{{WT}}" in found


def test_the_ast_recipe_in_the_prompt_actually_runs(prompt):
    """Das Rezept im Prompt muss ausführbar sein, nicht nur plausibel aussehen.

    Ein Prompt-Snippet, das an einem SyntaxError stirbt, macht aus jeder Runde
    ein DRY — und das sieht im Ledger aus wie „nichts gefunden".
    """
    start = prompt.index("import ast, pathlib")
    snippet = prompt[start:prompt.index("PY", start)]
    snippet = snippet.replace("<revier>", "loops")
    ast.parse(inspect.cleandoc(snippet))


# ── Iterationsstufen 1–2 ────────────────────────────────────────────────────

def test_revier_rotation_is_machine_readable(prompt):
    """Stufe 1: Rotation nach Gefuehl ist keine Rotation.

    dashboard-experience plante drei Naechte hintereinander denselben Slice, weil
    der Planner den Ledger-Zustand frei interpretierte. Ein maschinenlesbares
    Feld laesst sich nicht wegargumentieren.
    """
    assert "REVIER=" in prompt
    assert "kleinste Nummer" in prompt
    assert "beginne bei 1" in prompt, "der leere Erstfall muss definiert sein"


def test_a_test_only_diff_is_not_a_fix(prompt):
    """Stufe 2: Anti-Reward-Hacking.

    Ein Commit, der nur eine Testdatei anfasst, dokumentiert die Luege, statt sie
    zu beheben — sieht im Ledger aber wie FIXED aus.
    """
    assert "nur eine Testdatei anfasst, ist kein Fix" in prompt
    assert "Reward-Hacking" in prompt
    assert "`BLOCKED`" in prompt
