"""FastAPI routes for the /control Design Board."""
from __future__ import annotations

import mimetypes

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import Response

from hermes_cli import design_board_store as store
from hermes_cli.design_board_kanban import task_facets

_CHUNK = 1024 * 1024
_MAX_BYTES = 100 * 1024 * 1024


def register_design_board_routes(app: FastAPI) -> None:
    @app.get("/api/design-board/cards")
    async def _list():
        return [
            {k: c[k] for k in ("id", "kind", "title", "target", "status",
                               "linked_tasks", "updated_at")}
            for c in store.list_cards()
        ]

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
        facets = task_facets(card["linked_tasks"])
        card["task_facets"] = facets
        card["derived_status"] = store.derive_card_status([f["status"] for f in facets])
        return card

    @app.patch("/api/design-board/cards/{card_id}")
    async def _patch(card_id: str, request: Request):
        body = await request.json()
        if store.get_card(card_id) is None:
            raise HTTPException(404, "card not found")
        if "status" in body:
            store.set_status(card_id, body["status"])
        return store.get_card(card_id)

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
