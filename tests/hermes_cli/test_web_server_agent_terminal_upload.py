"""Tests for POST /api/agent-terminals/upload (phone → terminal file bridge)."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from hermes_cli import web_server


def _real_png_bytes(width: int = 4, height: int = 4) -> bytes:
    """A genuinely valid PNG (RGB, no filter), built with a real encoder path
    (zlib + PNG chunk framing) — not a fake `b"PNG..."` string."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # color type 2 = truecolor RGB
    raw = b"".join(b"\x00" + bytes((10, 200, 30)) * width for _ in range(height))
    idat = zlib.compress(raw)
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _client() -> TestClient:
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    return client


@pytest.fixture
def uploads_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_real_png_fixture_is_genuinely_decodable() -> None:
    """Sanity check on the fixture itself, not the endpoint: this must be a
    real PNG a real decoder accepts, not a hand-typed byte string."""
    png_bytes = _real_png_bytes()
    with Image.open(__import__("io").BytesIO(png_bytes)) as img:
        assert img.format == "PNG"
        assert img.size == (4, 4)


def test_upload_real_png_lands_byte_identical_inside_uploads_dir(uploads_home: Path) -> None:
    png_bytes = _real_png_bytes()
    client = _client()

    resp = client.post(
        "/api/agent-terminals/upload",
        files={"file": ("screenshot.png", png_bytes, "image/png")},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["size"] == len(png_bytes)

    saved_path = Path(body["path"])
    assert saved_path.is_absolute()
    uploads_dir = uploads_home / "uploads"
    assert saved_path.parent == uploads_dir
    assert saved_path.name == body["name"]
    assert saved_path.exists()
    assert saved_path.read_bytes() == png_bytes
    # Round-trips through a real PNG decoder too, not just a byte comparison.
    with Image.open(saved_path) as img:
        assert img.format == "PNG"
        assert img.size == (4, 4)


def test_upload_sanitizes_path_traversal_and_spaces_in_filename(uploads_home: Path) -> None:
    png_bytes = _real_png_bytes()
    client = _client()

    resp = client.post(
        "/api/agent-terminals/upload",
        files={"file": ("../../evil name.png", png_bytes, "image/png")},
    )

    assert resp.status_code == 200
    body = resp.json()
    uploads_dir = uploads_home / "uploads"
    saved_path = Path(body["path"])
    assert saved_path.parent == uploads_dir
    assert "/" not in body["name"]
    assert " " not in body["name"]
    assert saved_path.exists()
    assert saved_path.read_bytes() == png_bytes


def test_upload_over_cap_returns_413_and_leaves_no_residue(uploads_home: Path) -> None:
    oversized = b"\x00" * (25 * 1024 * 1024 + 1024)
    client = _client()

    resp = client.post(
        "/api/agent-terminals/upload",
        files={"file": ("big.bin", oversized, "application/octet-stream")},
    )

    assert resp.status_code == 413
    uploads_dir = uploads_home / "uploads"
    assert uploads_dir.exists()  # dir itself is created before the size check
    assert list(uploads_dir.iterdir()) == []
