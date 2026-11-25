"""The user guide inside the app: its pages and screenshots, for anyone signed in
(in local mode that is the built-in admin, so it is simply open)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from opsis.api.deps import get_principal, get_settings
from opsis.services import user_guide
from opsis.settings import Settings

# Not under /api/docs: that is the API reference, which is served without the CSP.
router = APIRouter(prefix="/api/guide", dependencies=[Depends(get_principal)])


@router.get("")
def contents(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return user_guide.render(settings, user_guide.CONTENTS)


@router.get("/images/{name}")
def image(name: str, settings: Settings = Depends(get_settings)) -> FileResponse:
    return FileResponse(
        user_guide.image_file(settings, name),
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/{page}")
def page(page: str, settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return user_guide.render(settings, page)
