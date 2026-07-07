"""FastAPI routes for the /control Design Board."""
from __future__ import annotations

import logging
import mimetypes
import os
import sqlite3
import subprocess
import tempfile

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import Response

from hermes_cli import design_board_cli
from hermes_cli import design_board_store as store
from hermes_cli.design_board_kanban import batch_task_facets

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
_MAX_BYTES = 100 * 1024 * 1024
# HTML mockups are text; keep them well below the image ceiling.
_MAX_HTML_BYTES = 5 * 1024 * 1024


def _safe_batch_facets(task_ids: list[str]) -> tuple[dict, bool]:
    """batch_task_facets, degrading to {} + kanban_ok=False on a DB hiccup."""
    try:
        return batch_task_facets(task_ids), True
    except (sqlite3.Error, OSError) as exc:
        logger.warning("design-board batch facet lookup failed: %s", exc)
        return {}, False


def register_design_board_routes(app: FastAPI) -> None:
    @app.get("/api/design-board/cards")
    async def _list():
        cards = store.list_cards()
        all_task_ids = [
            tid for c in cards for tid in c.get("linked_tasks", [])
        ]
        facet_map, kanban_ok = _safe_batch_facets(all_task_ids)
        out = []
        for c in cards:
            item = {k: c[k] for k in ("id", "kind", "title", "target", "status",
                                      "linked_tasks", "updated_at")}
            statuses = [
                facet_map[tid]["status"]
                for tid in c.get("linked_tasks", [])
                if tid in facet_map
            ]
            item["derived_status"] = store.derive_card_status(statuses)
            item["kanban_ok"] = kanban_ok
            out.append(item)
        return out

    @app.post("/api/design-board/cards")
    async def _create(request: Request):
        body = await request.json()
        cid = store.create_card(
            kind=body["kind"], title=body["title"],
            target=body.get("target"), created_by=body.get("created_by", "piet"),
        )
        return {"id": cid}

    @app.get("/api/design-board/cards/{card_id}")
    async def _get(card_id: str):
        card = store.get_card(card_id)
        if card is None:
            raise HTTPException(404, "card not found")
        facet_map, kanban_ok = _safe_batch_facets(card["linked_tasks"])
        facets = list(facet_map.values())
        card["task_facets"] = facets
        card["derived_status"] = store.derive_card_status([f["status"] for f in facets])
        card["kanban_ok"] = kanban_ok
        return card

    @app.patch("/api/design-board/cards/{card_id}")
    async def _patch(card_id: str, request: Request):
        body = await request.json()
        card = store.get_card(card_id)
        if card is None:
            raise HTTPException(404, "card not found")
        if "status" in body:
            try:
                store.set_status(card_id, body["status"])
            except ValueError:
                raise HTTPException(400, "bad status")
        card = store.get_card(card_id)
        facet_map, kanban_ok = _safe_batch_facets(card["linked_tasks"])
        facets = list(facet_map.values())
        card["task_facets"] = facets
        card["derived_status"] = store.derive_card_status([f["status"] for f in facets])
        card["kanban_ok"] = kanban_ok
        return card

    @app.post("/api/design-board/cards/{card_id}/promote")
    async def _promote(card_id: str):
        card = store.get_card(card_id)
        if card is None:
            raise HTTPException(404, "card not found")
        if card.get("linked_tasks"):
            raise HTTPException(409, "card already promoted")
        try:
            task_id = design_board_cli.promote(card_id)
        except (sqlite3.Error, OSError) as exc:
            logger.warning("design-board promote failed: %s", exc)
            raise HTTPException(
                503, {"error": "kanban_unavailable", "message": str(exc)}
            )
        except ValueError as exc:
            raise HTTPException(400, {"error": "invalid_card", "message": str(exc)})
        card = store.get_card(card_id)
        facet_map, kanban_ok = _safe_batch_facets(card["linked_tasks"])
        facets = list(facet_map.values())
        card["task_facets"] = facets
        card["derived_status"] = store.derive_card_status([f["status"] for f in facets])
        card["kanban_ok"] = kanban_ok
        return {"task_id": task_id, "card": card}

    @app.post("/api/design-board/cards/{card_id}/entries")
    async def _add_entry(card_id: str, request: Request):
        body = await request.json()
        try:
            eid = store.add_entry(
                card_id, author=body["author"], kind=body["kind"],
                note=body.get("note", ""), pins=body.get("pins"),
                asset_name=body.get("asset_name"), html_name=body.get("html_name"),
            )
        except KeyError:
            raise HTTPException(404, "card not found")
        return {"id": eid}

    @app.post("/api/design-board/cards/{card_id}/images")
    async def _upload(card_id: str, file: UploadFile = File(...)):
        if store.get_card(card_id) is None:
            raise HTTPException(404, "card not found")
        buf = bytearray()
        while True:
            chunk = await file.read(_CHUNK)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > _MAX_BYTES:
                raise HTTPException(413, "file too large")
        name = store.write_asset(card_id, file.filename or "upload.bin", bytes(buf))
        return {"name": name}

    @app.post("/api/design-board/cards/{card_id}/mockups")
    async def _upload_mockup(
        card_id: str,
        file: UploadFile = File(...),
        note: str = Form(""),
    ):
        """Upload an HTML mockup: render it to PNG and store a mockup_html entry.

        Reuses ``design_board_cli.add_mockup`` (the same code the CLI uses), so
        the tab drives the Claude-Design integration pipeline directly. Render
        failures degrade to structured JSON errors instead of a bare 500.
        """
        if store.get_card(card_id) is None:
            raise HTTPException(404, "card not found")
        buf = bytearray()
        while True:
            chunk = await file.read(_CHUNK)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > _MAX_HTML_BYTES:
                raise HTTPException(413, {
                    "error": "file_too_large",
                    "message": f"HTML mockup exceeds {_MAX_HTML_BYTES} bytes",
                })
        # Name the temp file after the (sanitised) upload and force an .html
        # extension so the served asset renders as HTML in the iframe.
        filename = store.sanitize_asset_name(file.filename or "mockup.html")
        if not filename.lower().endswith((".html", ".htm")):
            filename += ".html"
        with tempfile.TemporaryDirectory() as td:
            html_path = os.path.join(td, filename)
            with open(html_path, "wb") as fh:
                fh.write(bytes(buf))
            try:
                eid = design_board_cli.add_mockup(card_id, html_path, note=note)
            except FileNotFoundError as exc:
                logger.warning("design-board mockup renderer missing: %s", exc)
                raise HTTPException(502, {
                    "error": "render_unavailable",
                    "message": "HTML→PNG renderer (chromium-shot) not available",
                })
            except subprocess.TimeoutExpired as exc:
                logger.warning("design-board mockup render timed out: %s", exc)
                raise HTTPException(504, {
                    "error": "render_timeout",
                    "message": "HTML→PNG render timed out",
                })
            except RuntimeError as exc:
                logger.warning("design-board mockup render failed: %s", exc)
                raise HTTPException(502, {
                    "error": "render_failed", "message": str(exc),
                })
        return {"id": eid}

    @app.get("/api/design-board/cards/{card_id}/assets/{name}")
    async def _serve(card_id: str, name: str):
        try:
            path = store.resolve_asset_path(card_id, name)
        except ValueError:
            raise HTTPException(400, "bad asset name")
        if not path.is_file():
            raise HTTPException(404, "asset not found")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return Response(content=path.read_bytes(), media_type=ctype)
