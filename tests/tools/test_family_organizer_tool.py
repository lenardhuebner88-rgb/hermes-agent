"""Tests for the family_organizer tool (Hermes NL-Brain → FO Hermes-API).

Covers env/token handling, list-name resolution, write payloads + headers,
presence enum validation, and the availability check_fn. httpx is patched at
the module level (same idiom as tests/tools/test_web_tools_tavily.py).
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

import tools.family_organizer_tool as fo

TOKEN_ENV = {"HERMES_SERVICE_TOKEN": "svc-token", "FAMILY_ORGANIZER_BASE_URL": "http://fo.test"}


def _resp(status: int, payload=None, *, content=True):
    r = MagicMock()
    r.status_code = status
    r.content = b"x" if content else b""
    r.json.return_value = payload if payload is not None else {}
    r.text = json.dumps(payload) if payload is not None else ""
    return r


# ─── check_fn ────────────────────────────────────────────────────────────────

def test_check_false_without_token():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_SERVICE_TOKEN", None)
        assert fo.check_family_organizer_requirements() is False


def test_check_true_on_health_200():
    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.get", return_value=_resp(200, {"ok": True})) as g:
            assert fo.check_family_organizer_requirements() is True
            url = g.call_args.args[0]
            assert url == "http://fo.test/api/hermes/health"
            assert g.call_args.kwargs["headers"]["Authorization"] == "Bearer svc-token"


def test_check_false_on_health_500():
    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.get", return_value=_resp(500)):
            assert fo.check_family_organizer_requirements() is False


# ─── fo_create_task ──────────────────────────────────────────────────────────

def test_create_task_resolves_list_and_posts():
    lists = {"lists": [{"id": "list-uuid", "name": "Einkauf", "kind": "todo", "itemCount": 3}]}
    created = {"item": {"id": "item-1", "listId": "list-uuid", "title": "Milch", "done": False}}

    def fake_request(method, url, **kw):
        if method == "GET" and url.endswith("/api/hermes/lists"):
            return _resp(200, lists)
        if method == "POST" and url.endswith("/api/hermes/lists/list-uuid/items"):
            assert kw["json"] == {"title": "Milch"}
            assert kw["headers"]["Authorization"] == "Bearer svc-token"
            # write path must carry a canonical UUID idempotency key
            assert "X-Request-Id" in kw["headers"]
            return _resp(201, created)
        raise AssertionError(f"unexpected {method} {url}")

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_create_task(list_name="einkauf", title="Milch"))
    assert out["created"] is True
    assert out["item"]["id"] == "item-1"


def test_create_task_unknown_list_returns_error_with_options():
    lists = {"lists": [{"id": "l1", "name": "Einkauf"}]}
    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", return_value=_resp(200, lists)):
            out = json.loads(fo.fo_create_task(list_name="Garage", title="x"))
    assert "error" in out
    assert out["available_lists"] == ["Einkauf"]


def test_create_task_requires_title():
    with patch.dict(os.environ, TOKEN_ENV):
        out = json.loads(fo.fo_create_task(list_name="Einkauf", title="   "))
    assert "error" in out


def test_create_task_surfaces_api_error():
    def fake_request(method, url, **kw):
        if method == "GET":
            return _resp(200, {"lists": [{"id": "l1", "name": "Einkauf"}]})
        return _resp(409, {"error": {"code": "conflict"}})

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_create_task(list_name="Einkauf", title="Milch"))
    assert "error" in out
    assert "409" in out["error"]


# ─── fo_set_presence ─────────────────────────────────────────────────────────

def test_set_presence_posts_payload():
    captured = {}

    def fake_request(method, url, **kw):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kw["json"]
        captured["headers"] = kw["headers"]
        return _resp(201, {"presence": {"id": "p1", "status": "homeoffice"}})

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_set_presence(member_role="Papa", status="HOMEOFFICE", date="2026-06-05"))
    assert out["updated"] is True
    assert captured["url"] == "http://fo.test/api/hermes/presence"
    assert captured["json"] == {"date": "2026-06-05", "memberRole": "papa", "status": "homeoffice"}
    assert "X-Request-Id" in captured["headers"]


def test_set_presence_rejects_bad_role():
    with patch.dict(os.environ, TOKEN_ENV):
        out = json.loads(fo.fo_set_presence(member_role="opa", status="home", date="2026-06-05"))
    assert "error" in out


def test_set_presence_rejects_bad_status():
    with patch.dict(os.environ, TOKEN_ENV):
        out = json.loads(fo.fo_set_presence(member_role="papa", status="sleeping", date="2026-06-05"))
    assert "error" in out


def test_set_presence_requires_date():
    with patch.dict(os.environ, TOKEN_ENV):
        out = json.loads(fo.fo_set_presence(member_role="papa", status="home", date=""))
    assert "error" in out


# ─── fo_add_birthday ─────────────────────────────────────────────────────────

def test_add_birthday_posts_payload():
    captured = {}

    def fake_request(method, url, **kw):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kw["json"]
        captured["headers"] = kw["headers"]
        return _resp(201, {"birthday": {"id": "b1", "name": "Oma", "date": "2026-06-14"}})

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_add_birthday(name="Oma", date="2026-06-14", notes="60."))
    assert out["created"] is True
    assert out["birthday"]["id"] == "b1"
    assert captured["method"] == "POST"
    assert captured["url"] == "http://fo.test/api/hermes/birthdays"
    assert captured["json"] == {"name": "Oma", "date": "2026-06-14", "notes": "60."}
    assert "X-Request-Id" in captured["headers"]


def test_add_birthday_requires_name_and_date():
    with patch.dict(os.environ, TOKEN_ENV):
        assert "error" in json.loads(fo.fo_add_birthday(name="  ", date="2026-06-14"))
        assert "error" in json.loads(fo.fo_add_birthday(name="Oma", date=""))


# ─── fo_set_meal_plan ────────────────────────────────────────────────────────

def test_set_meal_plan_posts_payload():
    captured = {}

    def fake_request(method, url, **kw):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kw["json"]
        captured["headers"] = kw["headers"]
        return _resp(201, {"mealPlan": {"id": "m1", "date": "2026-06-08", "title": "Pizza"}})

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_set_meal_plan(date="2026-06-08", title="Pizza"))
    assert out["updated"] is True
    assert out["mealPlan"]["id"] == "m1"
    assert captured["url"] == "http://fo.test/api/hermes/meal-plans"
    assert captured["json"] == {"date": "2026-06-08", "title": "Pizza"}
    assert "X-Request-Id" in captured["headers"]


def test_set_meal_plan_requires_date_and_title():
    with patch.dict(os.environ, TOKEN_ENV):
        assert "error" in json.loads(fo.fo_set_meal_plan(date="", title="Pizza"))
        assert "error" in json.loads(fo.fo_set_meal_plan(date="2026-06-08", title="  "))


# ─── read helpers ────────────────────────────────────────────────────────────

def test_list_lists():
    lists = {"lists": [{"id": "l1", "name": "Einkauf", "kind": "todo", "itemCount": 2}]}
    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", return_value=_resp(200, lists)):
            out = json.loads(fo.fo_list_lists())
    assert out["lists"][0]["name"] == "Einkauf"


def test_request_without_token_errors():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_SERVICE_TOKEN", None)
        out = json.loads(fo.fo_list_lists())
    assert "error" in out
    assert "HERMES_SERVICE_TOKEN" in out["error"]


# ─── fo_update_task (0091) ───────────────────────────────────────────────────

def test_update_task_resolves_item_and_patches():
    lists = {"lists": [{"id": "list-uuid", "name": "Einkauf"}]}
    items = {"items": [{"id": "item-9", "listId": "list-uuid", "title": "Brot", "doneAt": None}]}
    captured = {}

    def fake_request(method, url, **kw):
        if method == "GET" and url.endswith("/api/hermes/lists"):
            return _resp(200, lists)
        if method == "GET" and url.endswith("/api/hermes/lists/list-uuid/items"):
            return _resp(200, items)
        if method == "PATCH" and url.endswith("/api/hermes/lists/list-uuid/items/item-9"):
            captured["json"] = kw["json"]
            captured["headers"] = kw["headers"]
            return _resp(200, {"item": {"id": "item-9", "done": True}})
        raise AssertionError(f"unexpected {method} {url}")

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_update_task(list_name="einkauf", item_title="brot", done=True))
    assert out["updated"] is True
    assert captured["json"] == {"done": True}
    assert "X-Request-Id" in captured["headers"]


def test_update_task_requires_a_change_field():
    # Feld-Check greift VOR jedem HTTP-Call.
    with patch.dict(os.environ, TOKEN_ENV):
        out = json.loads(fo.fo_update_task(list_name="Einkauf", item_title="Brot"))
    assert "error" in out


def test_update_task_unknown_item_returns_error_with_options():
    lists = {"lists": [{"id": "list-uuid", "name": "Einkauf"}]}
    items = {"items": [{"id": "i1", "title": "Milch"}]}

    def fake_request(method, url, **kw):
        if url.endswith("/api/hermes/lists"):
            return _resp(200, lists)
        if url.endswith("/items"):
            return _resp(200, items)
        raise AssertionError(f"unexpected {method} {url}")

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_update_task(list_name="Einkauf", item_title="Brot", done=True))
    assert "error" in out
    assert out["available_items"] == ["Milch"]


# ─── fo_delete_task (0091, Confirm-on-Delete) ────────────────────────────────

def test_delete_task_without_confirm_asks_first_and_does_not_delete():
    lists = {"lists": [{"id": "list-uuid", "name": "Einkauf"}]}
    items = {"items": [{"id": "item-9", "title": "Brot"}]}
    calls = []

    def fake_request(method, url, **kw):
        calls.append(method)
        if url.endswith("/api/hermes/lists"):
            return _resp(200, lists)
        if url.endswith("/items"):
            return _resp(200, items)
        raise AssertionError(f"unexpected {method} {url}")

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_delete_task(list_name="Einkauf", item_title="Brot"))
    assert out["confirm_required"] is True
    assert out["item"]["id"] == "item-9"
    assert "DELETE" not in calls  # ohne Bestätigung wird NICHT gelöscht


def test_delete_task_with_confirm_deletes_with_empty_body():
    lists = {"lists": [{"id": "list-uuid", "name": "Einkauf"}]}
    items = {"items": [{"id": "item-9", "title": "Brot"}]}
    captured = {}

    def fake_request(method, url, **kw):
        if url.endswith("/api/hermes/lists"):
            return _resp(200, lists)
        if method == "GET" and url.endswith("/items"):
            return _resp(200, items)
        if method == "DELETE" and url.endswith("/api/hermes/lists/list-uuid/items/item-9"):
            captured["json"] = kw["json"]
            captured["headers"] = kw["headers"]
            return _resp(200, {"ok": True, "id": "item-9"})
        raise AssertionError(f"unexpected {method} {url}")

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(
                fo.fo_delete_task(list_name="Einkauf", item_title="Brot", confirm=True)
            )
    assert out["deleted"] is True
    assert out["item"]["id"] == "item-9"
    # DELETE trägt den von der FO-Write-Infra erwarteten leeren JSON-Body + Idempotenz-Key.
    assert captured["json"] == {}
    assert "X-Request-Id" in captured["headers"]


# ─── fo_create_event Konflikt-Gate (0094) ───────────────────────────────────

def test_create_event_creates_when_no_clash():
    def fake_request(method, url, **kw):
        if method == "GET" and "/api/hermes/events/conflicts" in url:
            return _resp(200, {"exactTimeClash": False, "conflicts": [], "count": 0})
        if method == "POST" and url.endswith("/api/hermes/events"):
            assert kw["json"]["title"] == "Zahnarzt"
            assert "X-Request-Id" in kw["headers"]
            return _resp(201, {"event": {"id": "ev-1", "title": "Zahnarzt"}})
        raise AssertionError(f"unexpected {method} {url}")

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_create_event(title="Zahnarzt", date="2026-06-14", time="15:30"))
    assert out["created"] is True
    assert out["event"]["id"] == "ev-1"


def test_create_event_gates_on_exact_time_clash():
    calls = []

    def fake_request(method, url, **kw):
        calls.append(method)
        if method == "GET" and "/conflicts" in url:
            return _resp(
                200,
                {
                    "exactTimeClash": True,
                    "conflicts": [{"title": "Oma Besuch", "startsAt": "2026-06-14T15:30:00+02:00"}],
                    "count": 1,
                },
            )
        raise AssertionError(f"unexpected {method} {url}")

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_create_event(title="Zahnarzt", date="2026-06-14", time="15:30"))
    assert out["conflict_required"] is True
    assert out["conflicts"][0]["title"] == "Oma Besuch"
    assert "POST" not in calls  # bei exakter Kollision wird NICHT angelegt


def test_create_event_with_confirm_conflict_creates_despite_clash():
    def fake_request(method, url, **kw):
        if method == "GET" and "/conflicts" in url:
            return _resp(200, {"exactTimeClash": True, "conflicts": [], "count": 1})
        if method == "POST" and url.endswith("/api/hermes/events"):
            return _resp(201, {"event": {"id": "ev-2"}})
        raise AssertionError(f"unexpected {method} {url}")

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(
                fo.fo_create_event(
                    title="Zahnarzt", date="2026-06-14", time="15:30", confirm_conflict=True
                )
            )
    assert out["created"] is True
    assert out["event"]["id"] == "ev-2"


def test_create_event_reports_other_same_day_events_without_blocking():
    def fake_request(method, url, **kw):
        if method == "GET" and "/conflicts" in url:
            return _resp(
                200,
                {
                    "exactTimeClash": False,
                    "conflicts": [{"title": "Sport", "startsAt": "2026-06-14T18:00:00+02:00"}],
                    "count": 1,
                },
            )
        if method == "POST" and url.endswith("/api/hermes/events"):
            return _resp(201, {"event": {"id": "ev-3"}})
        raise AssertionError(f"unexpected {method} {url}")

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_create_event(title="Zahnarzt", date="2026-06-14", time="15:30"))
    assert out["created"] is True
    assert out["also_that_day"][0]["title"] == "Sport"


# ─── registry wiring ─────────────────────────────────────────────────────────

def test_tools_registered_under_family_organizer_toolset():
    from tools.registry import registry

    for name in (
        "fo_create_task",
        "fo_set_presence",
        "fo_create_event",
        "fo_add_birthday",
        "fo_set_meal_plan",
        "fo_update_task",
        "fo_delete_task",
        "fo_list_lists",
        "fo_list_presence",
    ):
        entry = registry._tools.get(name)
        assert entry is not None, f"{name} not registered"
        assert entry.toolset == "family-organizer"
