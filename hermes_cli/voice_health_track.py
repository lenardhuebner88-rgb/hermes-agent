"""Fail-closed Hermes Voice contract for confirmed Health Track capture.

The Health Track repository is intentionally not imported here. This module owns only validation,
preview and confirmation semantics; the currently coordinated adapter follow-up will replace the
terminal `adapter_pending` result with the existing authenticated write surface.
"""

from __future__ import annotations

from datetime import date
from typing import Any


def _iso_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def health_track_capture(args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        return {"status": "failed", "code": "invalid_arguments"}
    kind = args.get("kind")
    day = _iso_date(args.get("date"))
    target = args.get("target_account")
    confirmed = args.get("confirm") is True
    if kind not in {"weight", "meal"} or day is None or target != "piet":
        return {"status": "failed", "code": "invalid_arguments"}

    if kind == "weight":
        allowed = {"kind", "date", "target_account", "weight_kg", "confirm"}
        value = args.get("weight_kg")
        if set(args) - allowed or not isinstance(value, (int, float)) or not 20 <= float(value) <= 400:
            return {"status": "failed", "code": "invalid_weight"}
        preview = {
            "date": day,
            "type": "weight",
            "weight_kg": round(float(value), 2),
            "target_account": "piet",
        }
    else:
        allowed = {"kind", "date", "target_account", "description", "amounts", "confirm"}
        description = args.get("description")
        amounts = args.get("amounts", "")
        if (
            set(args) - allowed
            or not isinstance(description, str)
            or not description.strip()
            or len(description) > 500
            or not isinstance(amounts, str)
            or len(amounts) > 500
        ):
            return {"status": "failed", "code": "invalid_meal"}
        preview = {
            "date": day,
            "type": "meal",
            "description": description.strip(),
            "amounts": amounts.strip(),
            "target_account": "piet",
        }

    if not confirmed:
        return {
            "status": "confirm_required",
            "preview": preview,
            "message": "Zeige diese Vorschau sichtbar und frage nach einer ausdrücklichen Bestätigung.",
        }
    return {
        "status": "blocked",
        "code": "health_track_adapter_pending",
        "preview": preview,
        "message": "Bestätigter Entwurf bleibt erhalten; der koordinierte Health-Track-Adapter ist noch nicht freigegeben.",
    }
