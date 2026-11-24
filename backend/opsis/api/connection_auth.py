"""REST routes for a connection's saved sign-in: Google Sheets through OAuth.

    POST /api/connections/{id}/google/start         the address to send the person to
    POST /api/connections/google/finish             finish from the address Google sent back
    POST /api/connections/{id}/google/token         use an existing token.json instead
    GET  /api/connections/{id}/google/spreadsheets  spreadsheets to pick from (?q= to search)

Only those who manage connections may use them. The refresh token is never
returned; see opsis/services/google_oauth.py.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from opsis.api.deps import get_db, get_settings, require
from opsis.auth.principal import Permission, Principal
from opsis.services import connections, google_oauth
from opsis.settings import Settings

router = APIRouter(prefix="/api")
manage = require(Permission.CONNECTIONS_MANAGE)


class FinishIn(BaseModel):
    # The whole address from the browser's address bar after allowing access.
    url: str = Field(min_length=5, max_length=8000)


class TokenIn(BaseModel):
    # The contents of a token.json written by google-auth (Credentials.to_json()).
    token: str = Field(min_length=2, max_length=64_000)


@router.post("/connections/{connection_id}/google/start")
def start(
    connection_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(manage),
) -> dict[str, Any]:
    return google_oauth.start(db, settings, actor, connections.get(db, connection_id))


@router.post("/connections/google/finish")
def finish(
    body: FinishIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(manage),
) -> dict[str, Any]:
    return google_oauth.finish(db, settings, actor, body.url)


@router.post("/connections/{connection_id}/google/token")
def use_token(
    connection_id: uuid.UUID,
    body: TokenIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(manage),
) -> dict[str, Any]:
    connection = connections.get(db, connection_id)
    return google_oauth.use_token_json(db, settings, actor, connection, body.token)


@router.get("/connections/{connection_id}/google/spreadsheets")
def spreadsheets(
    connection_id: uuid.UUID,
    q: str | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(manage),
) -> dict[str, Any]:
    connection = connections.get(db, connection_id)
    return {"spreadsheets": google_oauth.list_spreadsheets(db, settings, connection, q)}
