"""Invariant tests for the real Hermes Voice PWA icon assets."""

from __future__ import annotations

import json
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from urllib.parse import urlsplit


CLIENT_DIR = Path(__file__).parents[2] / "hermes_cli" / "voice_client"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _decode_png_rgb(path: Path) -> tuple[int, int, list[bytes]]:
    """Decode the RGB scanlines emitted by the deterministic icon renderer.

    Keeping this tiny decoder in the test avoids making Pillow or another
    image package a project dependency merely to assert launcher corner
    pixels.  It deliberately accepts only the non-interlaced RGB format used
    by these committed assets.
    """
    data = path.read_bytes()
    assert data.startswith(PNG_SIGNATURE)

    position = len(PNG_SIGNATURE)
    compressed = bytearray()
    width = height = color_type = interlace = None
    while position < len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        chunk_type = data[position + 4 : position + 8]
        payload = data[position + 8 : position + 8 + length]
        position += 12 + length
        if chunk_type == b"IHDR":
            width, height, depth, color_type, _, _, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            assert depth == 8
        elif chunk_type == b"IDAT":
            compressed.extend(payload)
        elif chunk_type == b"IEND":
            break

    assert width is not None and height is not None
    assert color_type == 2, "committed icons must be opaque 8-bit RGB PNGs"
    assert interlace == 0
    bytes_per_pixel = 3
    stride = width * bytes_per_pixel
    raw = zlib.decompress(compressed)
    assert len(raw) == height * (stride + 1)

    rows: list[bytes] = []
    previous = bytearray(stride)
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        scanline = raw[offset + 1 : offset + 1 + stride]
        offset += stride + 1
        reconstructed = bytearray(stride)
        for index, value in enumerate(scanline):
            left = reconstructed[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            above = previous[index]
            upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                estimate = left + above - upper_left
                distances = (
                    abs(estimate - left),
                    abs(estimate - above),
                    abs(estimate - upper_left),
                )
                predictor = (left, above, upper_left)[distances.index(min(distances))]
            else:
                raise AssertionError(f"unsupported PNG filter {filter_type}")
            reconstructed[index] = (value + predictor) & 0xFF
        rows.append(bytes(reconstructed))
        previous = reconstructed

    return width, height, rows


def _pixel(row: bytes, x: int) -> tuple[int, int, int]:
    offset = x * 3
    return tuple(row[offset : offset + 3])  # type: ignore[return-value]


def test_svg_has_full_bleed_graphite_background_and_safe_voice_mark() -> None:
    root = ET.parse(CLIENT_DIR / "icon.svg").getroot()
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    rectangles = root.findall("svg:rect", namespace)

    assert rectangles[0].attrib == {
        "width": "512",
        "height": "512",
        "fill": "#0d100f",
    }
    voice_mark = root.find("svg:g[@id='voice-mark']", namespace)
    assert voice_mark is not None
    assert voice_mark.attrib["data-maskable-safe-zone"] == "central-80-percent-circle"


def test_committed_png_icons_are_opaque_dark_and_full_bleed() -> None:
    expected_sizes = {
        "icon-192.png": 192,
        "icon-512.png": 512,
        "icon-maskable-512.png": 512,
    }
    for name, expected_size in expected_sizes.items():
        width, height, rows = _decode_png_rgb(CLIENT_DIR / name)
        assert (width, height) == (expected_size, expected_size)
        corners = (
            _pixel(rows[0], 0),
            _pixel(rows[0], width - 1),
            _pixel(rows[-1], 0),
            _pixel(rows[-1], width - 1),
        )
        assert all(max(corner) < 40 for corner in corners), corners
        assert len(set(corners)) == 1, corners


def test_manifest_and_service_worker_share_versioned_icon_urls() -> None:
    manifest = json.loads((CLIENT_DIR / "manifest.json").read_text(encoding="utf-8"))
    icon_urls = {icon["src"] for icon in manifest["icons"]}
    assert icon_urls
    assert all(urlsplit(url).query == "v=2" for url in icon_urls)
    assert all((CLIENT_DIR / urlsplit(url).path.removeprefix("/voice/")).is_file() for url in icon_urls)

    service_worker = (CLIENT_DIR / "sw.js").read_text(encoding="utf-8")
    assert 'const CACHE = "hermes-voice-v7";' in service_worker
    assert all(f'"{url}"' in service_worker for url in icon_urls)
    assert 'fetch(request).catch(() => caches.match("/voice/offline.html"))' in service_worker
