"""On-disk store for the /control Design Board. Pure — no FastAPI, no kanban."""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

from hermes_constants import get_hermes_home

VALID_KINDS = {"bug", "wish", "mockup", "reference"}
VALID_STATUSES = {"open", "in_progress", "addressed", "archived"}


def board_root() -> Path:
    root = get_hermes_home() / "design-board" / "cards"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _card_dir(card_id: str) -> Path:
    return board_root() / card_id


def _card_json(card_id: str) -> Path:
    return _card_dir(card_id) / "card.json"


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _now() -> int:
    return int(time.time())


def create_card(*, kind: str, title: str, target: dict | None = None,
                created_by: str = "piet") -> str:
    if kind not in VALID_KINDS:
        raise ValueError(f"bad kind: {kind}")
    card_id = "c_" + uuid.uuid4().hex[:8]
    now = _now()
    card = {
        "id": card_id, "kind": kind, "title": title, "target": target,
        "linked_tasks": [], "status": "open", "created_by": created_by,
        "created_at": now, "updated_at": now, "entries": [],
    }
    _write_json_atomic(_card_json(card_id), card)
    return card_id


def get_card(card_id: str) -> dict | None:
    path = _card_json(card_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_cards() -> list[dict]:
    out = []
    for d in board_root().iterdir():
        if d.is_dir():
            card = get_card(d.name)
            if card:
                out.append(card)
    out.sort(key=lambda c: c["updated_at"], reverse=True)
    return out


def _save(card: dict) -> None:
    card["updated_at"] = _now()
    _write_json_atomic(_card_json(card["id"]), card)


def add_entry(card_id: str, *, author: str, kind: str, note: str = "",
              pins: list[dict] | None = None, asset_name: str | None = None,
              html_name: str | None = None) -> str:
    card = get_card(card_id)
    if card is None:
        raise KeyError(card_id)
    entry_id = "e_" + uuid.uuid4().hex[:8]
    entry = {
        "id": entry_id, "author": author, "kind": kind, "note": note,
        "asset": f"assets/{asset_name}" if asset_name else None,
        "html": f"assets/{html_name}" if html_name else None,
        "pins": pins or [], "created_at": _now(),
    }
    card["entries"].append(entry)
    _save(card)
    return entry_id


def set_status(card_id: str, status: str) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(f"bad status: {status}")
    card = get_card(card_id)
    if card is None:
        raise KeyError(card_id)
    card["status"] = status
    _save(card)


def assets_dir(card_id: str) -> Path:
    d = _card_dir(card_id) / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sanitize_asset_name(name: str) -> str:
    base = os.path.basename(name.replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")
    if not base:
        raise ValueError("empty asset name")
    return base


def resolve_asset_path(card_id: str, name: str) -> Path:
    parts = name.replace("\\", "/").split("/")
    if ".." in parts:
        raise ValueError("asset path escapes card dir")
    root = assets_dir(card_id).resolve()
    candidate = (root / sanitize_asset_name(name)).resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError("asset path escapes card dir")
    return candidate


def write_asset(card_id: str, name: str, data: bytes) -> str:
    safe = sanitize_asset_name(name)
    dest = resolve_asset_path(card_id, safe)
    tmp = dest.with_suffix(dest.suffix + f".tmp-{uuid.uuid4().hex}")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return safe


_TERMINAL = {"done", "archived"}


def link_task(card_id: str, task_id: str) -> None:
    card = get_card(card_id)
    if card is None:
        raise KeyError(card_id)
    if task_id not in card["linked_tasks"]:
        card["linked_tasks"].append(task_id)
        _save(card)


def unlink_task(card_id: str, task_id: str) -> None:
    card = get_card(card_id)
    if card is None:
        raise KeyError(card_id)
    if task_id in card["linked_tasks"]:
        card["linked_tasks"].remove(task_id)
        _save(card)


def derive_card_status(task_statuses: list[str]) -> str | None:
    if not task_statuses:
        return None
    if all(s in _TERMINAL for s in task_statuses):
        return "addressed"
    return "in_progress"
