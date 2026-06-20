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


# ─── fo_set_vacation (0078-S2) ───────────────────────────────────────────────

def test_set_vacation_posts_payload():
    captured = {}

    def fake_request(method, url, **kw):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kw["json"]
        captured["headers"] = kw["headers"]
        return _resp(201, {"vacations": [{"id": "vac-1", "label": "Urlaub"}]})

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(
                fo.fo_set_vacation(
                    member_role="Papa", start_date="2026-07-01", end_date="2026-07-14"
                )
            )
    assert out["created"] is True
    assert out["vacations"][0]["id"] == "vac-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "http://fo.test/api/hermes/vacations"
    assert captured["json"] == {
        "memberRoles": ["papa"],
        "startDate": "2026-07-01",
        "endDate": "2026-07-14",
        "label": "Urlaub",
    }
    assert "X-Request-Id" in captured["headers"]


def test_set_vacation_rejects_bad_role():
    with patch.dict(os.environ, TOKEN_ENV):
        out = json.loads(
            fo.fo_set_vacation(member_role="opa", start_date="2026-07-01", end_date="2026-07-14")
        )
    assert "error" in out


def test_set_vacation_requires_dates():
    with patch.dict(os.environ, TOKEN_ENV):
        assert "error" in json.loads(
            fo.fo_set_vacation(member_role="papa", start_date="", end_date="2026-07-14")
        )
        assert "error" in json.loads(
            fo.fo_set_vacation(member_role="papa", start_date="2026-07-01", end_date="")
        )


# ─── fo_upsert_recipe (0078-S3) ──────────────────────────────────────────────

def test_upsert_recipe_posts_payload():
    captured = {}

    def fake_request(method, url, **kw):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kw["json"]
        captured["headers"] = kw["headers"]
        return _resp(201, {"recipe": {"id": "rec-1", "name": "Lasagne"}})

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(
                fo.fo_upsert_recipe(name="Lasagne", ingredients=["Hackfleisch", "Tomaten"])
            )
    assert out["created"] is True
    assert out["recipe"]["id"] == "rec-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "http://fo.test/api/hermes/recipes"
    assert captured["json"] == {
        "name": "Lasagne",
        "ingredients": ["Hackfleisch", "Tomaten"],
    }
    assert "X-Request-Id" in captured["headers"]


def test_upsert_recipe_requires_name():
    with patch.dict(os.environ, TOKEN_ENV):
        out = json.loads(fo.fo_upsert_recipe(name="  "))
    assert "error" in out


# ─── fo_import_recipe (NextGen-2 F3) ─────────────────────────────────────────

def test_import_recipe_posts_source_url_and_returns_discord_message():
    captured = {}
    recipe = {
        "id": "rec-2",
        "slug": "pikanter-dattel-frischkaese-dip",
        "name": "Pikanter Dattel-Frischkäse-Dip",
    }

    def fake_request(method, url, **kw):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kw["json"]
        captured["headers"] = kw["headers"]
        return _resp(201, {"recipe": recipe, "alreadyImported": False})

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_import_recipe(url="https://www.chefkoch.de/r/1?foo=bar"))
    assert out["imported"] is True
    assert out["alreadyImported"] is False
    assert out["recipe"]["slug"] == "pikanter-dattel-frischkaese-dip"
    assert out["url"] == "https://family-organizer-xi.vercel.app/recipes/pikanter-dattel-frischkaese-dip"
    assert "Pikanter Dattel-Frischkäse-Dip" in out["message"]
    assert out["url"] in out["message"]
    assert captured["method"] == "POST"
    assert captured["url"] == "http://fo.test/api/hermes/recipes"
    assert captured["json"] == {"sourceUrl": "https://www.chefkoch.de/r/1?foo=bar"}
    assert "X-Request-Id" in captured["headers"]


def test_import_recipe_already_imported_returns_existing_link_message():
    recipe = {"id": "rec-2", "slug": "dattel-dip", "name": "Dattel-Dip"}

    with patch.dict(os.environ, TOKEN_ENV):
        with patch(
            "tools.family_organizer_tool.httpx.request",
            return_value=_resp(200, {"recipe": recipe, "alreadyImported": True}),
        ):
            out = json.loads(fo.fo_import_recipe(url="https://www.chefkoch.de/r/1"))
    assert out["imported"] is False
    assert out["alreadyImported"] is True
    assert "Schon im Rezeptbuch" in out["message"]
    assert out["url"].endswith("/recipes/dattel-dip")


def test_import_recipe_requires_https_url():
    with patch.dict(os.environ, TOKEN_ENV):
        out = json.loads(fo.fo_import_recipe(url="http://localhost:3000/rezept"))
    assert "error" in out
    assert "HTTPS" in out["error"]


def test_import_recipe_api_error_is_calm_german():
    with patch.dict(os.environ, TOKEN_ENV):
        with patch(
            "tools.family_organizer_tool.httpx.request",
            return_value=_resp(400, {"error": {"code": "recipe_json_ld_missing"}}),
        ):
            out = json.loads(fo.fo_import_recipe(url="https://example.com/ohne-rezept"))
    assert "error" in out
    assert out["error"].startswith("Ich konnte das Rezept nicht importieren")


# ─── fo_delete_recipe (0078, Confirm-on-Delete) ──────────────────────────────

def test_delete_recipe_without_confirm_asks_first():
    recipes = {"recipes": [{"id": "rec-9", "name": "Lasagne"}]}
    calls = []

    def fake_request(method, url, **kw):
        calls.append(method)
        if method == "GET" and url.endswith("/api/hermes/recipes"):
            return _resp(200, recipes)
        raise AssertionError(f"unexpected {method} {url}")

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_delete_recipe(name="lasagne"))
    assert out["confirm_required"] is True
    assert out["recipe"]["id"] == "rec-9"
    assert "DELETE" not in calls


def test_delete_recipe_with_confirm_deletes():
    recipes = {"recipes": [{"id": "rec-9", "name": "Lasagne"}]}
    captured = {}

    def fake_request(method, url, **kw):
        if method == "GET":
            return _resp(200, recipes)
        if method == "DELETE" and url.endswith("/api/hermes/recipes/rec-9"):
            captured["json"] = kw["json"]
            captured["headers"] = kw["headers"]
            return _resp(200, {"ok": True, "id": "rec-9"})
        raise AssertionError(f"unexpected {method} {url}")

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_delete_recipe(name="Lasagne", confirm=True))
    assert out["deleted"] is True
    assert captured["json"] == {}
    assert "X-Request-Id" in captured["headers"]


def test_delete_recipe_unknown_returns_error():
    with patch.dict(os.environ, TOKEN_ENV):
        with patch(
            "tools.family_organizer_tool.httpx.request",
            return_value=_resp(200, {"recipes": [{"id": "r1", "name": "Pizza"}]}),
        ):
            out = json.loads(fo.fo_delete_recipe(name="Lasagne"))
    assert "error" in out


# ─── fo_delete_vacation (0078, Confirm-on-Delete) ────────────────────────────

def test_delete_vacation_by_date_confirm_flow():
    vacs = {"vacations": [
        {"id": "vac-9", "label": "Sommer", "startDate": "2031-03-01", "endDate": "2031-03-08"},
    ]}
    calls = []

    def fake_request(method, url, **kw):
        calls.append(method)
        if method == "GET" and "/api/hermes/vacations" in url:
            return _resp(200, vacs)
        raise AssertionError(f"unexpected {method} {url}")

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_delete_vacation(start_date="2031-03-01"))
    assert out["confirm_required"] is True
    assert out["vacation"]["id"] == "vac-9"
    assert "DELETE" not in calls


def test_delete_vacation_with_confirm_deletes_by_id():
    captured = {}

    def fake_request(method, url, **kw):
        if method == "DELETE" and url.endswith("/api/hermes/vacations/vac-9"):
            captured["json"] = kw["json"]
            captured["headers"] = kw["headers"]
            return _resp(200, {"ok": True, "id": "vac-9"})
        raise AssertionError(f"unexpected {method} {url}")

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_delete_vacation(vacation_id="vac-9", confirm=True))
    assert out["deleted"] is True
    assert captured["json"] == {}
    assert "X-Request-Id" in captured["headers"]


def test_delete_vacation_ambiguous_returns_candidates():
    vacs = {"vacations": [
        {"id": "v1", "label": "Trip", "startDate": "2031-03-01", "endDate": "2031-03-08"},
        {"id": "v2", "label": "Trip", "startDate": "2031-03-01", "endDate": "2031-03-08"},
    ]}

    def fake_request(method, url, **kw):
        if method == "GET":
            return _resp(200, vacs)
        raise AssertionError(f"unexpected {method} {url}")

    with patch.dict(os.environ, TOKEN_ENV):
        with patch("tools.family_organizer_tool.httpx.request", side_effect=fake_request):
            out = json.loads(fo.fo_delete_vacation(start_date="2031-03-01"))
    assert out["ambiguous"] is True
    assert len(out["vacations"]) == 2


def test_delete_vacation_requires_anchor():
    with patch.dict(os.environ, TOKEN_ENV):
        out = json.loads(fo.fo_delete_vacation())
    assert "error" in out


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
        "fo_set_vacation",
        "fo_upsert_recipe",
        "fo_import_recipe",
        "fo_delete_recipe",
        "fo_delete_vacation",
        "fo_list_lists",
        "fo_list_presence",
        "fo_log_wish",
    ):
        entry = registry._tools.get(name)
        assert entry is not None, f"{name} not registered"
        assert entry.toolset == "family-organizer"


# ─── fo_log_wish (Demand-Funnel T1) ─────────────────────────────────────────
# Schreibt in die lokale Kanban-DB (kein FO-API-Write) — isoliertes
# HERMES_HOME pro Test, gleiche Fixture-Idee wie tests/hermes_cli/test_funnel.py.

from pathlib import Path

from hermes_cli import funnel
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_log_wish_creates_triage_task_for_family(kanban_home):
    out = json.loads(fo.fo_log_wish("Dunkles Theme fürs Tablet", context="Oskar abends"))
    assert out.get("status") == "triage"
    conn = kb.connect()
    try:
        task = kb.get_task(conn, out["task_id"])
        assert task.status == "triage"
        assert task.created_by == "family"
        # Phase A: the coder-claude funnel default folds into the canonical premium lane.
        assert task.assignee == "premium"
        assert "NICHT bauen" in (task.body or "")
        assert "Oskar abends" in (task.body or "")
    finally:
        conn.close()


def test_log_wish_dedupes_same_wish(kanban_home):
    a = json.loads(fo.fo_log_wish("Mehr Statistik bitte"))
    b = json.loads(fo.fo_log_wish("mehr   STATISTIK bitte"))
    assert a["task_id"] == b["task_id"]


def test_log_wish_cap_guard(kanban_home):
    for i in range(funnel.FUNNEL_CAP):
        out = json.loads(fo.fo_log_wish(f"wunsch nummer {i}"))
        assert "task_id" in out, out
    out = json.loads(fo.fo_log_wish("einer zu viel"))
    assert "error" in out
    assert "voll" in out["error"]


def test_log_wish_requires_text(kanban_home):
    out = json.loads(fo.fo_log_wish("   "))
    assert "error" in out
