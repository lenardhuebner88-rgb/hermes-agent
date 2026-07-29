"""Der Claude-Abo-Katalog hat genau eine Quelle — und die Ableitungen driften nicht.

Der Loader ist absichtlich fail-soft (kaputte YAML → leere Liste, damit
``/api/lanes`` nicht stirbt). Damit "leer" nicht still zur Normalität wird,
prüfen die Tests hier die ECHTE Katalogdatei: sie muss lesbar sein, die
Max-Abo-Modelle tragen, und die eingecheckte TS-Datei muss dazu passen.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from hermes_cli import claude_cli_model_catalog as catalog


class TestLabelAbleitung:
    """Labels kommen aus der ID, damit die YAML eine Zeile pro Modell bleibt."""

    @pytest.mark.parametrize(
        ("model_id", "expected"),
        [
            ("claude-opus-5", "Claude Opus 5"),
            ("claude-opus-4-8", "Claude Opus 4.8"),
            ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
            ("claude-haiku-4-5", "Claude Haiku 4.5"),
            ("claude-fable-5", "Claude Fable 5"),
            # Wort-Suffixe bleiben eigene Wörter, statt in die Version zu rutschen.
            ("claude-opus-5-fast", "Claude Opus 5 Fast"),
            # 2-Part-ID: genau an der Grenze — muss noch formatiert werden,
            # nicht als Roh-ID zurückkommen (killt < → <= und 2 → 3 auf L52).
            ("claude-opus", "Claude Opus"),
        ],
    )
    def test_leitet_anzeigename_aus_id_ab(self, model_id: str, expected: str) -> None:
        assert catalog._derive_label(model_id) == expected

    def test_fremdes_schema_bleibt_unveraendert(self) -> None:
        """Keine Verstümmelung, wenn mal eine Nicht-Claude-ID auftaucht."""
        assert catalog._derive_label("gpt-5.5") == "gpt-5.5"


class TestKatalogLesen:
    def test_echte_katalogdatei_traegt_die_max_abo_modelle(self) -> None:
        """Kontrollprobe gegen den fail-soft-Pfad: die echte Datei ist NICHT leer."""
        ids = catalog.load_catalog_model_ids()
        assert ids, f"{catalog.CATALOG_PATH} liefert keine Claude-Modelle"
        assert "claude-opus-5" in ids
        assert "claude-opus-4-8" in ids

    def test_reihenfolge_bleibt_die_der_yaml(self) -> None:
        """Die Katalogreihenfolge IST die Dropdown-Reihenfolge."""
        ids = catalog.load_catalog_model_ids()
        assert ids.index("claude-opus-5") < ids.index("claude-opus-4-8")

    def test_lane_optionen_haben_die_dropdown_felder(self) -> None:
        by_id = {m["id"]: m for m in catalog.load_claude_cli_lane_models()}
        opus5 = by_id["claude-opus-5"]
        assert opus5 == {
            "id": "claude-opus-5",
            "label": "Claude Opus 5",
            "runtime": "claude-cli",
            "group": "Claude (Max-Abo)",
            "provider": None,
            "locked": False,
        }

    def test_claude_cli_runtime_schaltet_den_reasoning_regler_frei(self) -> None:
        """Opus 5 muss die volle `claude --effort`-Leiter anbieten.

        Der Lanes-Tab hängt den Regler an ``runtime == "claude-cli"``, nicht an
        der Modell-ID — dieser Test pinnt, dass die Katalog-Einträge genau diese
        Runtime tragen, sonst rendert der Regler still als "kein Reasoning-Knopf".
        """
        from hermes_cli.kanban_worker_runtime import CLAUDE_CLI_EFFORT_LEVELS

        runtimes = {m["runtime"] for m in catalog.load_claude_cli_lane_models()}
        assert runtimes == {"claude-cli"}
        assert CLAUDE_CLI_EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")

    def test_fehlender_katalog_kippt_die_route_nicht(self, tmp_path) -> None:
        assert catalog.load_catalog_model_ids(tmp_path / "weg.yaml") == []

    def test_kaputte_yaml_kippt_die_route_nicht(self, tmp_path) -> None:
        broken = tmp_path / "models.yaml"
        broken.write_text("engines: [nicht: ein: mapping", encoding="utf-8")
        assert catalog.load_catalog_model_ids(broken) == []

    def test_duplikate_werden_entfernt(self, tmp_path) -> None:
        dupes = tmp_path / "models.yaml"
        dupes.write_text(
            "engines:\n  claude:\n    models: [claude-opus-5, claude-opus-5]\n",
            encoding="utf-8",
        )
        assert catalog.load_catalog_model_ids(dupes) == ["claude-opus-5"]


class TestKeineZweiteListe:
    def test_lane_routen_speisen_aus_dem_katalog(self) -> None:
        """Der Lanes-Tab (und damit Fleet→Heute) zeigt genau den Katalog."""
        source = (
            catalog.REPO_ROOT / "plugins" / "kanban" / "dashboard" / "lane_routes.py"
        ).read_text(encoding="utf-8")
        assert "load_claude_cli_lane_models()" in source, (
            "lane_routes.py führt wieder eine eigene Modell-Liste"
        )

    def test_generierte_ts_datei_ist_aktuell(self) -> None:
        """Drift zwischen YAML und claudeModels.ts ist rot, nicht still."""
        result = subprocess.run(
            [sys.executable, "scripts/sync_model_catalog.py", "--check"],
            cwd=catalog.REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, (
            f"claudeModels.ts driftet von loops/models.yaml ab:\n{result.stderr}"
        )
