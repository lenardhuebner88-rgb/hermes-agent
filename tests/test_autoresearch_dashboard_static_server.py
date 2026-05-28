from __future__ import annotations

import importlib.util
import threading
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "serve_autoresearch_dashboard.py"


def load_module():
    spec = importlib.util.spec_from_file_location("serve_autoresearch_dashboard", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def start_server(tmp_path):
    module = load_module()
    (tmp_path / "dashboard.html").write_text("<h1>Hermes Autoresearch Dashboard</h1>", encoding="utf-8")
    handler = module.make_handler(tmp_path)
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return module, server, server.server_address[1]


def request(port: int, method: str, path: str):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path)
    response = conn.getresponse()
    body = response.read().decode("utf-8", errors="replace")
    conn.close()
    return response.status, body


def test_get_root_returns_dashboard_html(tmp_path):
    _module, server, port = start_server(tmp_path)
    try:
        status, body = request(port, "GET", "/")
        assert status == 200
        assert "Hermes Autoresearch Dashboard" in body
    finally:
        server.shutdown()
        server.server_close()


def test_get_dashboard_returns_same_artifact(tmp_path):
    _module, server, port = start_server(tmp_path)
    try:
        root_status, root_body = request(port, "GET", "/")
        page_status, page_body = request(port, "GET", "/dashboard.html")
        assert root_status == page_status == 200
        assert root_body == page_body
    finally:
        server.shutdown()
        server.server_close()


def test_static_server_serves_only_dashboard_html(tmp_path):
    (tmp_path / "skill_improvements_report.md").write_text("private local audit detail", encoding="utf-8")
    _module, server, port = start_server(tmp_path)
    try:
        status, _body = request(port, "GET", "/skill_improvements_report.md")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()


def test_post_request_returns_405(tmp_path):
    _module, server, port = start_server(tmp_path)
    try:
        status, _body = request(port, "POST", "/request")
        assert status == 405
    finally:
        server.shutdown()
        server.server_close()


def test_path_traversal_is_rejected(tmp_path):
    _module, server, port = start_server(tmp_path)
    try:
        status, _body = request(port, "GET", "/../../.env")
        assert status in {403, 404}
    finally:
        server.shutdown()
        server.server_close()


def test_server_defaults_to_loopback_not_wildcard():
    module = load_module()
    assert module.DEFAULT_HOST == "127.0.0.1"
    assert module.DEFAULT_HOST != "0.0.0.0"
