import pytest
from hermes_cli import design_board_cli as cli
from hermes_cli import design_board_store as store


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def test_build_brief_includes_pins_and_target():
    card = {
        "title": "Header overlaps", "kind": "bug",
        "target": {"view": "FleetView"},
        "entries": [{
            "author": "piet", "note": "top strip",
            "asset": "assets/e1.png",
            "pins": [{"id": "p1", "x": 0.42, "y": 0.61, "note": "overlaps puls chip"}],
        }],
    }
    brief = cli.build_brief(card)
    assert "FleetView" in brief
    assert "overlaps puls chip" in brief
    assert "0.42" in brief
    assert "assets/e1.png" in brief


def test_promote_creates_and_links(monkeypatch):
    cid = store.create_card(kind="bug", title="x")
    store.add_entry(cid, author="piet", kind="screenshot", note="n",
                    pins=[{"id": "p1", "x": 0.1, "y": 0.2, "note": "y"}])
    created = {}

    def fake_create_task(conn, *, title, body, assignee=None, **kw):
        created["title"] = title
        created["body"] = body
        return "t_new123"

    monkeypatch.setattr(cli.kanban_db, "create_task", fake_create_task)
    monkeypatch.setattr(cli.kanban_db, "connect_closing",
                        lambda *a, **k: _Ctx())
    tid = cli.promote(cid, assignee="coder")
    assert tid == "t_new123"
    assert store.get_card(cid)["linked_tasks"] == ["t_new123"]
    assert "0.1" in created["body"]


def test_render_html_to_png_invokes_chromium(monkeypatch, tmp_path):
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        out = [a.split("=", 1)[1] for a in cmd if a.startswith("--screenshot=")][0]
        open(out, "wb").write(b"PNG")
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    html = tmp_path / "m.html"; html.write_text("<h1>hi</h1>")
    png = tmp_path / "m.png"
    cli.render_html_to_png(str(html), str(png), width=800, height=600)
    assert png.read_bytes() == b"PNG"
    assert any("--window-size=800,600" in a for a in calls["cmd"])
    assert any(a.startswith("file://") for a in calls["cmd"])


class _Ctx:
    def __enter__(self): return object()
    def __exit__(self, *a): return False
