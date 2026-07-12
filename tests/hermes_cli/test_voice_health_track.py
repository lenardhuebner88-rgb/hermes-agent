from hermes_cli.voice_health_track import health_track_capture
import pytest
from tools.voice_live_tools import FUNCTION_DECLARATIONS, VoiceToolExecutor


def test_weight_requires_visible_confirmation_before_any_write_boundary():
    preview = health_track_capture(
        {
            "kind": "weight",
            "date": "2026-07-12",
            "target_account": "piet",
            "weight_kg": 82.4,
            "confirm": False,
        }
    )
    assert preview == {
        "status": "confirm_required",
        "preview": {
            "date": "2026-07-12",
            "type": "weight",
            "weight_kg": 82.4,
            "target_account": "piet",
        },
        "message": "Zeige diese Vorschau sichtbar und frage nach einer ausdrücklichen Bestätigung.",
    }


def test_confirmed_capture_fails_closed_while_coordinated_adapter_is_pending():
    result = health_track_capture(
        {
            "kind": "meal",
            "date": "2026-07-12",
            "target_account": "piet",
            "description": "Zwei Eier und Brot",
            "amounts": "2 Eier, 2 Scheiben Brot",
            "confirm": True,
        }
    )
    assert result["status"] == "blocked"
    assert result["code"] == "health_track_adapter_pending"
    assert result["preview"]["description"] == "Zwei Eier und Brot"


def test_invalid_or_cross_account_payloads_never_reach_confirmation():
    assert health_track_capture({"kind": "weight", "date": "tomorrow", "target_account": "piet"})[
        "status"
    ] == "failed"
    assert health_track_capture(
        {
            "kind": "weight",
            "date": "2026-07-12",
            "target_account": "someone-else",
            "weight_kg": 80,
            "confirm": True,
        }
    )["status"] == "failed"


@pytest.mark.asyncio
async def test_voice_tool_surface_routes_to_fail_closed_preview_contract():
    declaration = next(item for item in FUNCTION_DECLARATIONS if item["name"] == "health_track_capture")
    assert "confirm=false" in declaration["description"]
    result = await VoiceToolExecutor(delegate=None).execute(
        "health_track_capture",
        {
            "kind": "weight",
            "date": "2026-07-12",
            "target_account": "piet",
            "weight_kg": 82.4,
            "confirm": False,
        },
    )
    assert result["status"] == "confirm_required"
