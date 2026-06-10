"""Unit tests for the Orchestrator hand-off message builder."""

from bridges.discord_prefilter.forward import build_forward_message


def test_includes_mention_provenance_and_content():
    msg = build_forward_message(
        content="bau mir einen neuen tab",
        author_name="Piet",
        source_channel="reviewer",
        mention_id="1500199614706483210",
    )
    assert msg.startswith("<@1500199614706483210> ")
    assert "#reviewer" in msg
    assert "Piet" in msg
    assert msg.endswith("bau mir einen neuen tab")


def test_no_mention_when_id_absent():
    msg = build_forward_message("x", "Piet", "reviewer", None)
    assert not msg.startswith("<@")
    assert "#reviewer" in msg


def test_falls_back_when_fields_missing():
    msg = build_forward_message("  do the thing  ", "", None, "42")
    assert msg.startswith("<@42> ")
    assert "Vorfilter" in msg
    assert msg.endswith("do the thing")  # content stripped
