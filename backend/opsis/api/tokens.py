"""REST routes for a user's own personal access tokens (used by MCP clients)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from opsis.api.deps import get_db, get_settings, signed_in_person
from opsis.auth.principal import Principal
from opsis.services import tokens
from opsis.settings import Settings

router = APIRouter(prefix="/api")


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1, max_length=len(tokens.SCOPES))
    days: int = Field(default=90, ge=1, le=tokens.MAX_DAYS)


@router.get("/tokens")
def list_tokens(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(signed_in_person),
) -> dict[str, Any]:
    return {
        "tokens": tokens.list_tokens(db, principal),
        "scopes": list(tokens.SCOPES),
        "mcpAuth": settings.mcp_auth,
        "mcpUrl": f"{settings.public_base_url.rstrip('/')}/mcp",
    }


@router.post("/tokens", status_code=201)
def create_token(
    body: TokenIn, db: Session = Depends(get_db), principal: Principal = Depends(signed_in_person)
) -> dict[str, Any]:
    """The response is the only time the token itself is returned."""
    described, secret = tokens.create(
        db, principal, name=body.name, scopes=body.scopes, days=body.days
    )
    return {**described, "token": secret}


@router.post("/tokens/{token_id}/revoke")
def revoke_token(
    token_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(signed_in_person),
) -> dict[str, Any]:
    return tokens.revoke(db, principal, token_id)


@router.delete("/tokens/{token_id}", status_code=204)
def delete_token(
    token_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(signed_in_person),
) -> None:
    tokens.delete(db, principal, token_id)
