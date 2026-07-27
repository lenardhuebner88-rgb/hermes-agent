"""Regression tests for off-loop file reads in dashboard endpoints."""

import asyncio
import base64
from pathlib import Path
import threading

import httpx
import pytest

from hermes_cli import web_server


@pytest.mark.parametrize(
    ("route", "filename", "response_key", "mime_type"),
    [
        ("/api/files/read", "sample.txt", "data_url", "text/plain"),
        ("/api/fs/read-data-url", "sample.png", "dataUrl", "image/png"),
    ],
)
def test_file_read_and_base64_encode_run_off_event_loop_thread(
    monkeypatch,
    tmp_path,
    route,
    filename,
    response_key,
    mime_type,
):
    root = tmp_path / "managed"
    root.mkdir()
    target = root / filename
    payload = b"off-loop"
    target.write_bytes(payload)
    monkeypatch.setenv("HERMES_DASHBOARD_FILES_ROOT", str(root))

    expected_data_url = (
        f"data:{mime_type};base64,"
        f"{base64.b64encode(payload).decode('ascii')}"
    )
    loop_thread = threading.current_thread()
    read_threads: list[threading.Thread] = []
    encode_threads: list[threading.Thread] = []
    original_read_bytes = Path.read_bytes
    original_b64encode = base64.b64encode

    def tracked_read_bytes(path: Path) -> bytes:
        if path == target:
            read_threads.append(threading.current_thread())
        return original_read_bytes(path)

    def tracked_b64encode(data: bytes, *args, **kwargs) -> bytes:
        if data == payload:
            encode_threads.append(threading.current_thread())
        return original_b64encode(data, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", tracked_read_bytes)
    monkeypatch.setattr(web_server.base64, "b64encode", tracked_b64encode)

    async def request_file() -> httpx.Response:
        transport = httpx.ASGITransport(app=web_server.app)
        headers = {
            web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN,
        }
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers=headers,
        ) as client:
            return await client.get(route, params={"path": str(target)})

    response = asyncio.run(request_file())

    assert response.status_code == 200, response.text
    assert response.json()[response_key] == expected_data_url
    assert read_threads and read_threads[0] is not loop_thread
    assert encode_threads and encode_threads[0] is not loop_thread
