from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm.exc import StaleDataError

from app.editor import apply_command, get_editor, positive_id, view
from app.keitaro import KeitaroError


class EditorCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, strict=True)
    action: Literal[
        "add",
        "remove",
        "restore",
        "pin",
        "equalize",
        "discard",
        "refresh",
        "resolve",
        "publish",
        "enable",
        "disable",
    ]
    stream_id: int | None = Field(default=None, gt=0, le=2147483647, strict=True)
    offer_id: int | None = Field(default=None, gt=0, le=2147483647, strict=True)
    share: int | None = Field(default=None, ge=0, le=100, strict=True)
    locked: bool | None = Field(default=None, strict=True)
    conflict_id: str | None = Field(default=None, max_length=100)
    choice: Literal["local", "remote"] | None = None


def build_editor_router(get_session, get_tracker, templates):
    router = APIRouter()

    @router.get("/campaigns", include_in_schema=False)
    def campaigns_page(request: Request):
        return templates.TemplateResponse(
            request=request, name="campaigns.html", headers={"Cache-Control": "no-store"}
        )

    @router.get("/campaigns/{campaign_id}", include_in_schema=False)
    def editor_page(request: Request, campaign_id: int = PathParam(gt=0, le=2147483647)):
        return templates.TemplateResponse(
            request=request,
            name="editor.html",
            context={"campaign_id": campaign_id},
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/api/campaigns")
    def campaign_list(
        offset: int = Query(default=0, ge=0, le=100000),
        limit: int = Query(default=20, ge=1, le=100),
        q: str = Query(default="", max_length=255),
        tracker=Depends(get_tracker),
    ):
        def page(start, count):
            data = tracker.request("GET", "/campaigns", params={"offset": start, "limit": count})
            if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
                raise KeitaroError("Некорректный список кампаний Keitaro.")
            return [
                {
                    "id": positive_id(c.get("id")),
                    "name": str(c.get("name", "")),
                    "state": str(c.get("state", "active")),
                }
                for c in data
            ]

        query = q.strip().casefold()
        if not query:
            rows = page(offset, limit + 1)
            return {"items": rows[:limit], "has_more": len(rows) > limit}
        matches = []
        for start in range(0, 100000, 100):
            rows = page(start, 100)
            matches.extend(
                c for c in rows if query in c["name"].casefold() or query in str(c["id"])
            )
            if len(matches) > offset + limit or len(rows) < 100:
                break
        return {
            "items": matches[offset : offset + limit],
            "has_more": len(matches) > offset + limit,
        }

    @router.get("/api/editor/campaigns/{campaign_id}")
    def editor_state(
        campaign_id: int = PathParam(gt=0, le=2147483647),
        session=Depends(get_session),
        tracker=Depends(get_tracker),
    ):
        return view(get_editor(session, tracker, campaign_id))

    @router.post("/api/editor/campaigns/{campaign_id}/actions")
    def action(
        command: EditorCommand,
        campaign_id: int = PathParam(gt=0, le=2147483647),
        session=Depends(get_session),
        tracker=Depends(get_tracker),
    ):
        try:
            editor = get_editor(session, tracker, campaign_id, lock=True)
            return apply_command(session, tracker, editor, command)
        except StaleDataError as exc:
            session.rollback()
            raise HTTPException(
                409, "Черновик изменён в другой вкладке. Перезагрузите редактор."
            ) from exc

    return router
