"""Operator-Autoland: pro Lauf schaltbar, mit Zweitschlüssel.

Bis 2026-07-28 konnte nur ein Pack aus ``AUTOLAND_PACK_ALLOWLIST`` landen —
gebunden an Safety- und Prompt-SHA. Das ist die richtige Autorität für
UNBEAUFSICHTIGTE Läufe (Timer/Cron) und bleibt unverändert. Neu daneben:
ein Operator kann Autoland für einen Lauf, den er selbst startet, für jedes
pipeline-Pack einschalten — mit ``AUTOLAND=1`` PLUS ``AUTOLAND_ACK=<Pack-Name>``.

Der Zweitschlüssel ist der Punkt: ``AUTOLAND=1`` allein könnte ein
versehentlicher Klick oder ein kopiertes Override-File sein.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from loops import runner as runner_module
from loops.runner import LoopRunner, load_pack


def _write_pack(root: Path, name: str, ptype: str = "pipeline") -> Path:
    d = root / name
    d.mkdir(parents=True)
    if ptype == "pipeline":
        prompts = {
            "plan": "PLANNER-PROMPT.md",
            "build": "BUILDER-PROMPT.md",
            "verify": "VERIFIER-PROMPT.md",
        }
    else:
        prompts = {"round": "ROUND-PROMPT.md"}
    for prompt in prompts.values():
        (d / prompt).write_text("x", encoding="utf-8")
    phases = "\n".join(
        textwrap.indent(
            textwrap.dedent(f"""
                {phase}:
                  engine: claude
                  model: claude-sonnet-5
                  timeout: 60
                  prompt: {prompt}
            """).strip("\n"),
            "  ",
        )
        for phase, prompt in prompts.items()
    )
    (d / "pack.yaml").write_text(
        f"name: {name}\ntype: {ptype}\nrepo: /home/piet/.hermes/hermes-agent\n"
        f"phases:\n{phases}\n",
        encoding="utf-8",
    )
    return d


def _runner(tmp_path: Path, pack_name: str, overrides: str, ptype: str = "pipeline"):
    packs = tmp_path / "packs"
    if not (packs / pack_name).exists():
        _write_pack(packs, pack_name, ptype)
    pack = load_pack(packs, pack_name)
    state_root = tmp_path / "state"
    state = state_root / pack.name
    state.mkdir(parents=True, exist_ok=True)
    (state / "overrides.env").write_text(overrides, encoding="utf-8")
    return LoopRunner(pack, state_root=state_root)


def test_without_the_switch_a_pack_does_not_land(tmp_path):
    assert _runner(tmp_path, "p", "").autoland_active is False


def test_switch_plus_second_key_activates_autoland(tmp_path):
    runner = _runner(tmp_path, "p", "AUTOLAND=1\nAUTOLAND_ACK=p\n")
    assert runner.autoland_active is True
    assert runner.autoland_runtime is True


def test_switch_without_second_key_is_a_hard_error(tmp_path):
    """Kein stilles Ignorieren: der Operator hat 'landen' gemeint."""
    with pytest.raises(RuntimeError, match="Zweitschlüssel"):
        _runner(tmp_path, "p", "AUTOLAND=1\n")


def test_second_key_must_match_the_pack_name(tmp_path):
    """Ein kopiertes overrides.env aus einem anderen Pack darf nicht landen."""
    with pytest.raises(RuntimeError, match="Zweitschlüssel"):
        _runner(tmp_path, "p", "AUTOLAND=1\nAUTOLAND_ACK=ein-anderes-pack\n")


def test_invalid_switch_value_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="ungültig"):
        _runner(tmp_path, "p", "AUTOLAND=vielleicht\nAUTOLAND_ACK=p\n")


def test_switch_off_variants_stay_off(tmp_path):
    for value in ("0", "false", "no", ""):
        runner = _runner(tmp_path, f"p{value or 'empty'}", f"AUTOLAND={value}\n")
        assert runner.autoland_active is False, value


def test_sweep_packs_cannot_be_switched_to_autoland(tmp_path):
    """Die Landungsleiter setzt die plan/build/verify-Queue voraus."""
    with pytest.raises(RuntimeError, match="pipeline"):
        _runner(tmp_path, "s", "AUTOLAND=1\nAUTOLAND_ACK=s\n", ptype="sweep")


# --- Der Vertrag, unter dem Operator-Autoland landet ----------------------


def test_operator_contract_allows_any_path_but_keeps_the_deny_list(tmp_path):
    runner = _runner(tmp_path, "p", "AUTOLAND=1\nAUTOLAND_ACK=p\n")
    contract = runner._autoland_contract()
    # Der Runner kann für ein beliebiges Pack nicht vorher wissen, welche Pfade
    # es anfasst — deshalb Deny- statt Allow-Liste.
    assert contract.allow_any_path is True
    assert contract.deny_prefixes == runner_module.OPERATOR_AUTOLAND_DENY_PREFIXES
    assert contract.require_visual == "if_web_touched"
    # Die SHA-Felder gehören zum Manifest-Vertrag, den dieser Pfad nicht führt.
    assert contract.safety_sha256 is None
    assert contract.prompt_sha256 is None


@pytest.mark.parametrize(
    "denied",
    [
        "hermes_cli/auth.py",
        "hermes_cli/dashboard_auth/session.py",
        "hermes_cli/kanban_db.py",
        "web/package-lock.json",
        "loops/runner.py",
        ".github/workflows/ci.yml",
        "scripts/deploy_dashboard.sh",
    ],
)
def test_deny_prefixes_cover_the_sensitive_paths(denied: str):
    """Auth, Board-Spine, Lockfiles, CI und der Runner selbst landen nie
    unbeaufsichtigt — auch nicht per Operator-Schalter."""
    assert any(
        denied.startswith(prefix)
        for prefix in runner_module.OPERATOR_AUTOLAND_DENY_PREFIXES
    ), denied


def test_empty_path_prefixes_still_mean_nothing_allowed(tmp_path):
    """Regressionswache: ``allow_any_path`` muss EXPLIZIT sein. Würde ein
    leeres ``path_prefixes`` als 'alles erlaubt' gelesen, hätte jeder
    kuratierte Vertrag mit leerer Liste still alles landen dürfen."""
    contract = runner_module.AutolandContract(
        path_prefixes=(),
        deny_prefixes=(),
        safety_sha256=None,
        prompt_sha256=None,
        require_visual="if_web_touched",
    )
    assert contract.allow_any_path is False


# --- Vertrags-Autoland bleibt unberührt ----------------------------------


def test_curated_autoland_pack_still_loads_and_lands_without_a_second_key():
    """Der Nachtlauf-Vertrag darf durch den neuen Schalter nicht aufweichen."""
    pack = load_pack(runner_module.PACKS_DIR, "dashboard-experience")
    assert pack.autoland is True
    contract = runner_module.AUTOLAND_CONTRACTS["dashboard-experience"]
    assert contract.allow_any_path is False
    assert contract.path_prefixes == ("web/src/control/",)
    assert contract.safety_sha256 is not None
    assert contract.prompt_sha256 is not None


def test_manifest_autoland_outside_the_allowlist_is_still_refused(tmp_path):
    """Ein Fremd-Pack kann sich Autoland weiterhin nicht selbst ins Manifest
    schreiben — der Operator-Weg ist der Schalter, nicht das YAML."""
    packs = tmp_path / "packs"
    _write_pack(packs, "fremd")
    manifest = packs / "fremd" / "pack.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "autoland: true\n", encoding="utf-8"
    )
    with pytest.raises(runner_module.ManifestError, match="nicht autorisiert"):
        load_pack(packs, "fremd")
