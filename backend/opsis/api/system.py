"""Health, public config and the current user."""

from __future__ import annotations

import contextlib
import json
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from opsis import __version__
from opsis.api.deps import get_db, get_principal, get_settings
from opsis.auth.principal import Principal
from opsis.db.models import User
from opsis.settings import Settings

health_router = APIRouter()
router = APIRouter(prefix="/api")

DEFAULT_BRANDING: dict[str, Any] = {
    "tagline": "See your data clearly",
    "colors": {"primary": "#2f5fd0", "primaryDark": "#8fb0ff"},
}


@health_router.get("/healthz", include_in_schema=False)
def healthz(db: Session = Depends(get_db)) -> JSONResponse:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse({"status": "unavailable", "db": "down"}, status_code=503)
    return JSONResponse({"status": "ok", "db": "ok"})


def load_branding(settings: Settings) -> dict[str, Any]:
    branding = dict(DEFAULT_BRANDING)
    with contextlib.suppress(OSError, ValueError):
        branding.update(json.loads(settings.branding_file.read_text(encoding="utf-8")))
    branding["name"] = settings.app_name  # APP_NAME always wins
    return branding


def public_config(settings: Settings) -> dict[str, Any]:
    """Non-secret flags for the frontend. Never add credentials or internal hosts here."""
    return {
        "appName": settings.app_name,
        "version": __version__,
        "deployMode": settings.deploy_mode,
        "authMode": settings.auth_mode,
        "jobRunner": settings.job_runner,
        "mcpAuth": settings.mcp_auth,
        "assistantProvider": settings.assistant_provider if settings.feature_assistant else None,
        "publicBaseUrl": settings.public_base_url,
        "basePath": settings.base_path,
        "maxUploadMb": settings.max_upload_mb,
        "features": {
            "scheduledRefresh": settings.feature_scheduled_refresh,
            "publicShareLinks": settings.feature_public_share_links,
            "email": settings.feature_email,
            "assistant": settings.feature_assistant,
            "sqlLab": settings.feature_sql_lab,
            "backups": settings.feature_backups,
        },
        "calendar": {
            "timezone": settings.workspace_timezone,
            "weekStart": settings.week_start,
            "fiscalYearStartMonth": settings.fiscal_year_start_month,
            "fiscalYearLabel": settings.fiscal_year_label,
        },
        "branding": load_branding(settings),
    }


@router.get("/config")
def get_config(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return public_config(settings)


@router.get("/schemas")
def get_schemas(
    settings: Settings = Depends(get_settings), _: Principal = Depends(get_principal)
) -> dict[str, Any]:
    """The JSON Schemas in spec/, by file name. The editor's JSON tab uses them
    for inline checks, and the agent will read them through MCP."""
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(settings.spec_dir.glob("*.schema.json"))
    }


@router.get("/me")
def get_me(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> dict[str, Any]:
    user = db.get(User, principal.user_id)
    return {
        "id": str(principal.user_id),
        "email": principal.email,
        "displayName": principal.display_name,
        "role": principal.role.value,
        "authMethod": principal.auth_method,
        "permissions": sorted(p.value for p in principal.permissions),
        "groups": sorted(principal.groups),
        "mustChangePassword": principal.must_change_password,
        "hasPassword": bool(user and user.password_hash),
        "google": bool(user and user.google_sub),
    }
