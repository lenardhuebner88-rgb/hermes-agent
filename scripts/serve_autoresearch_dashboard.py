#!/usr/bin/env python3
"""Serve the generated Autoresearch dashboard as loopback-only static HTML."""
from __future__ import annotations

import argparse
import contextlib
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPO = Path(__file__).resolve().parents[1]
AUDIT = REPO / ".hermes" / "skill-audit"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8776


def _safe_resolve(audit_dir: Path, request_path: str) -> Path | None:
    raw_path = unquote(urlsplit(request_path).path)
    if raw_path in {"", "/"}:
        raw_path = "/dashboard.html"
    if raw_path != "/dashboard.html":
        return None
    candidate = (audit_dir / "dashboard.html").resolve()
    root = audit_dir.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


class AutoresearchDashboardHandler(BaseHTTPRequestHandler):
    audit_dir = AUDIT
    server_version = "AutoresearchDashboard/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        target = _safe_resolve(self.audit_dir, self.path)
        if target is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = target.read_bytes()
        ctype = "text/html; charset=utf-8" if target.suffix == ".html" else "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        target = _safe_resolve(self.audit_dir, self.path)
        if target is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8" if target.suffix == ".html" else "application/octet-stream")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def log_message(self, format: str, *args: object) -> None:
        return


def make_handler(audit_dir: Path) -> type[AutoresearchDashboardHandler]:
    class Handler(AutoresearchDashboardHandler):
        pass

    Handler.audit_dir = audit_dir
    return Handler


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind host; defaults to loopback only")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port; defaults to 8776, not 8765")
    parser.add_argument("--audit-dir", type=Path, default=AUDIT, help="dashboard artifact directory")
    return parser


@contextlib.contextmanager
def run_test_server(audit_dir: Path, host: str = DEFAULT_HOST, port: int = 0):
    server = ThreadingHTTPServer((host, port), make_handler(audit_dir))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.host != DEFAULT_HOST:
        raise SystemExit("Refusing non-loopback host without a separate approval gate")
    if args.port == 8765:
        raise SystemExit("Refusing port 8765; use a dedicated Autoresearch port")
    audit_dir = args.audit_dir.resolve()
    dashboard = audit_dir / "dashboard.html"
    if not dashboard.is_file():
        raise SystemExit(f"dashboard.html not found under {audit_dir}")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(audit_dir))
    print(f"Serving Autoresearch dashboard at http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
