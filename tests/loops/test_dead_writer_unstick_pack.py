from __future__ import annotations

from pathlib import Path

from loops.runner import PACKS_DIR, load_pack


PACK_DIR = PACKS_DIR / "dead-writer-unstick"
PROMPT = PACK_DIR / "ROUND-PROMPT.md"
PATCH_ARTIFACT = "dead-writer-unstick.patch"
REPRO_ARTIFACT = "test_dead_writer_unstick.py"


def test_dead_writer_unstick_pack_is_single_round_preparation_only():
    pack = load_pack(PACKS_DIR, "dead-writer-unstick")

    assert pack.type == "sweep"
    assert pack.autoland is False
    assert pack.stop["max_rounds"] == 1
    assert pack.params["required_artifacts"] == (
        f"{PATCH_ARTIFACT},{REPRO_ARTIFACT}"
    )


def test_dead_writer_unstick_prompt_fixes_artifact_and_measurement_contracts():
    text = PROMPT.read_text(encoding="utf-8")

    assert f"{{{{STATE_DIR}}}}/{PATCH_ARTIFACT}" in text
    assert f"{{{{STATE_DIR}}}}/{REPRO_ARTIFACT}" in text
    assert "Unit(s)" in text
    assert "--since" in text
    assert "--until" in text
    assert "exakter Suchtext" in text
    assert '"dead writer recovery failed"' in text
    assert "LEDGER.md" in text


def test_dead_writer_unstick_prompt_closes_diagnosis_before_new_search():
    text = PROMPT.read_text(encoding="utf-8")

    assert "DIAGNOSED" in text
    assert "PREPARED" in text
    assert "keine neue Ursache" in text
    assert "hermes_cli/kanban_db.py nicht verändern" in text
    assert "git diff --exit-code -- hermes_cli/kanban_db.py" in text
    assert "git apply --check" in text
    assert "last-status" in text
    assert "push" in text.lower()


def test_dead_writer_unstick_artifact_names_are_plain_state_files():
    for name in (PATCH_ARTIFACT, REPRO_ARTIFACT):
        path = Path(name)
        assert not path.is_absolute()
        assert path.name == name
