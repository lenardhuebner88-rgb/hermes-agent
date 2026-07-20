import json

from tools import tts_tool


def test_edge_provider_falls_back_to_neutts_when_edge_is_unavailable(monkeypatch, tmp_path):
    output_path = tmp_path / "fallback.mp3"
    generated = []

    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "edge"})
    monkeypatch.setattr(tts_tool, "_import_edge_tts", lambda: (_ for _ in ()).throw(ImportError()))
    monkeypatch.setattr(tts_tool, "_check_neutts_available", lambda: True)

    def generate_neutts(text, file_path, _config):
        generated.append((text, file_path))
        output_path.write_bytes(b"RIFF-fallback")

    monkeypatch.setattr(tts_tool, "_generate_neutts", generate_neutts)

    result = json.loads(tts_tool.text_to_speech_tool("Hallo", str(output_path)))

    assert result["success"] is True
    assert result["provider"] == "neutts"
    assert generated == [("Hallo", str(output_path))]