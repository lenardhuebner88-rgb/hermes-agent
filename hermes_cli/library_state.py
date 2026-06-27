"""Persistent library preferences: saved searches and topic follows.

This is the smallest stateful slice for the control Bibliothek. The store is a
profile-local JSON file under ``$HERMES_HOME/control/library_state.json`` so it
stays separate from the read-only library content adapters and from production
cron/kanban data. Demo topics are exposed as virtual seed rows and only become
persisted when the user changes their follow status.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

_STATE_VERSION = 1
_STORE_FILE = "library_state.json"
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")
_TOPIC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,79}$")
_MAX_NAME_LEN = 160
_MAX_QUERY_LEN = 1000
_MAX_TAG_LEN = 80

DEMO_TOPICS: tuple[dict[str, str], ...] = (
    {"id": "ki-modelle", "title": "KI-Modelle"},
    {"id": "wm-2026-deutschland", "title": "WM 2026 Deutschland"},
    {"id": "hermes-dashboard", "title": "Hermes Dashboard"},
    {"id": "langfuse-langsmith", "title": "Langfuse/LangSmith"},
)


def storage_path() -> Path:
    """Return the profile-local JSON store for library preferences."""
    return get_hermes_home() / "control" / _STORE_FILE


def _now() -> int:
    return int(time.time())


def _empty_state() -> dict[str, Any]:
    return {"version": _STATE_VERSION, "saved_searches": [], "topics": []}


def _read_state() -> dict[str, Any]:
    path = storage_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_state()
    except (OSError, ValueError, TypeError):
        # Fail soft: a corrupt preference file should not empty the whole
        # Bibliothek API with a 500. The next successful write replaces it.
        return _empty_state()
    if not isinstance(raw, dict):
        return _empty_state()
    state = _empty_state()
    if isinstance(raw.get("saved_searches"), list):
        state["saved_searches"] = [
            item for item in raw["saved_searches"] if isinstance(item, dict)
        ]
    if isinstance(raw.get("topics"), list):
        state["topics"] = [item for item in raw["topics"] if isinstance(item, dict)]
    return state


def _write_state(state: dict[str, Any]) -> None:
    path = storage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _clean_text(value: Any, field: str, *, max_len: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    if len(text) > max_len:
        raise ValueError(f"{field} is too long")
    return text


def _clean_optional_text(value: Any, *, max_len: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > max_len:
        raise ValueError("value is too long")
    return text


def _clean_tags(values: Optional[list[Any]]) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("tags must be a list")
    tags: list[str] = []
    seen: set[str] = set()
    for raw in values:
        tag = _clean_optional_text(raw, max_len=_MAX_TAG_LEN)
        if tag is None:
            continue
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            tags.append(tag)
    return tags


def _saved_search_response(item: dict[str, Any]) -> dict[str, Any]:
    name = str(item.get("name") or item.get("title") or "").strip()
    return {
        "id": str(item.get("id") or ""),
        "name": name,
        "title": name,
        "query": str(item.get("query") or ""),
        "topic_tags": list(item.get("topic_tags") or []),
        "person_tags": list(item.get("person_tags") or []),
        "created_at": int(item.get("created_at") or 0),
        "updated_at": int(item.get("updated_at") or item.get("created_at") or 0),
    }


def list_saved_searches() -> list[dict[str, Any]]:
    """List saved searches newest-updated first."""
    items = [_saved_search_response(item) for item in _read_state()["saved_searches"]]
    items = [item for item in items if item["id"] and item["name"] and item["query"]]
    items.sort(key=lambda item: (-item["updated_at"], item["name"].casefold()))
    return items


def create_saved_search(
    *,
    name: str,
    query: str,
    topic_tags: Optional[list[Any]] = None,
    person_tags: Optional[list[Any]] = None,
) -> dict[str, Any]:
    """Create and persist a saved library search."""
    ts = _now()
    item = {
        "id": f"ss_{uuid.uuid4().hex[:12]}",
        "name": _clean_text(name, "name", max_len=_MAX_NAME_LEN),
        "query": _clean_text(query, "query", max_len=_MAX_QUERY_LEN),
        "topic_tags": _clean_tags(topic_tags),
        "person_tags": _clean_tags(person_tags),
        "created_at": ts,
        "updated_at": ts,
    }
    state = _read_state()
    state["saved_searches"].append(item)
    _write_state(state)
    return _saved_search_response(item)


def update_saved_search(
    search_id: str,
    *,
    name: Optional[str] = None,
    query: Optional[str] = None,
    topic_tags: Optional[list[Any]] = None,
    person_tags: Optional[list[Any]] = None,
) -> Optional[dict[str, Any]]:
    """Update a saved search; return ``None`` when the id is unknown."""
    if not _ID_RE.match(search_id or ""):
        raise ValueError("invalid saved search id")
    state = _read_state()
    for item in state["saved_searches"]:
        if item.get("id") != search_id:
            continue
        if name is not None:
            item["name"] = _clean_text(name, "name", max_len=_MAX_NAME_LEN)
        if query is not None:
            item["query"] = _clean_text(query, "query", max_len=_MAX_QUERY_LEN)
        if topic_tags is not None:
            item["topic_tags"] = _clean_tags(topic_tags)
        if person_tags is not None:
            item["person_tags"] = _clean_tags(person_tags)
        item["updated_at"] = _now()
        _write_state(state)
        return _saved_search_response(item)
    return None


def delete_saved_search(search_id: str) -> bool:
    """Delete a saved search by id."""
    if not _ID_RE.match(search_id or ""):
        raise ValueError("invalid saved search id")
    state = _read_state()
    before = len(state["saved_searches"])
    state["saved_searches"] = [
        item for item in state["saved_searches"] if item.get("id") != search_id
    ]
    if len(state["saved_searches"]) == before:
        return False
    _write_state(state)
    return True


def _seed_topic(topic: dict[str, str]) -> dict[str, Any]:
    ts = 0
    return {
        "id": topic["id"],
        "title": topic["title"],
        "followed": False,
        "subscribed": False,
        "seeded": True,
        "created_at": ts,
        "updated_at": ts,
    }


def _topic_response(item: dict[str, Any], *, seeded: bool = False) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or ""),
        "followed": bool(item.get("followed", False)),
        "subscribed": bool(item.get("subscribed", item.get("followed", False))),
        "seeded": bool(item.get("seeded", seeded)),
        "created_at": int(item.get("created_at") or 0),
        "updated_at": int(item.get("updated_at") or item.get("created_at") or 0),
    }


def _topic_map(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    topics = {topic["id"]: _seed_topic(topic) for topic in DEMO_TOPICS}
    for raw in state["topics"]:
        if not isinstance(raw, dict):
            continue
        topic_id = str(raw.get("id") or "")
        if not _TOPIC_ID_RE.match(topic_id):
            continue
        base = topics.get(topic_id, {})
        merged = {**base, **raw}
        if "seeded" not in merged:
            merged["seeded"] = topic_id in topics
        topics[topic_id] = merged
    return topics


def list_topics() -> list[dict[str, Any]]:
    """List seed and persisted topics with follow/subscription state."""
    topics = [_topic_response(item) for item in _topic_map(_read_state()).values()]
    topics = [topic for topic in topics if topic["id"] and topic["title"]]
    topics.sort(key=lambda topic: (not topic["followed"], topic["title"].casefold()))
    return topics


def set_topic_follow(topic_id: str, followed: bool) -> Optional[dict[str, Any]]:
    """Set follow/subscription status for an existing topic."""
    if not _TOPIC_ID_RE.match(topic_id or ""):
        raise ValueError("invalid topic id")
    state = _read_state()
    topics = _topic_map(state)
    if topic_id not in topics:
        return None
    now = _now()
    topic = topics[topic_id]
    topic["followed"] = bool(followed)
    topic["subscribed"] = bool(followed)
    topic["updated_at"] = now
    if not topic.get("created_at"):
        topic["created_at"] = now

    was_persisted = any(
        isinstance(raw, dict) and str(raw.get("id") or "") == topic_id
        for raw in state["topics"]
    )
    if not followed and not was_persisted:
        # Demo topics are virtual seeds; unfollowing a never-followed seed
        # is a no-op and must not create an empty persisted row.
        return _topic_response(topic)

    persisted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in state["topics"]:
        raw_id = str(raw.get("id") or "") if isinstance(raw, dict) else ""
        if raw_id == topic_id:
            if topic_id not in seen:
                persisted.append(topic)
                seen.add(topic_id)
        elif raw_id and _TOPIC_ID_RE.match(raw_id) and raw_id not in seen:
            persisted.append(raw)
            seen.add(raw_id)
    if topic_id not in seen:
        persisted.append(topic)
    state["topics"] = persisted
    _write_state(state)
    return _topic_response(topic)
