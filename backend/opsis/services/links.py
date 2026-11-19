"""Public share links (server mode, FEATURE_PUBLIC_SHARE_LINKS).

A link is a random 32-byte token that shows the published version of one
dashboard, read-only, to anyone who has it. It may expire, may need a
password, can be revoked, and counts its views.

Row-level security: everyone who opens a link is the same anonymous viewer
with the link's attribute set, chosen when the link is made. An empty set (the
default) makes every rule deny. A link never inherits an exemption. Only an
admin may give a link attributes of their choosing; anyone else can give it
none, or their own, so a link can never show more than its maker can see.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from opsis.auth import passwords
from opsis.auth.principal import Permission, Principal, Role
from opsis.db.models import Dashboard, ShareLink, User
from opsis.errors import ApiError
from opsis.services import access as acl
from opsis.services import audit, crypto, lockout
from opsis.services.accounts import _check_attributes
from opsis.settings import Settings

PREFIX_LENGTH = 8
UNLOCK_HOURS = 12
MAX_DAYS = 366


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def url_for(settings: Settings, token: str) -> str:
    return f"{settings.public_base_url}/s/{token}"


def _require_feature(settings: Settings) -> None:
    if not settings.feature_public_share_links:
        raise ApiError(404, "not_found", "Not found.")


def link_dict(link: ShareLink, creator: str | None = None) -> dict[str, Any]:
    now = datetime.now(UTC)
    status = (
        "revoked"
        if link.revoked_at
        else "expired"
        if link.expires_at and link.expires_at <= now
        else "active"
    )
    return {
        "id": str(link.id),
        "name": link.name,
        "prefix": link.prefix,
        "status": status,
        "hasPassword": link.password_hash is not None,
        "attributes": link.attributes,
        "expiresAt": link.expires_at.isoformat() if link.expires_at else None,
        "viewCount": link.view_count,
        "lastViewedAt": link.last_viewed_at.isoformat() if link.last_viewed_at else None,
        "createdBy": creator,
        "createdAt": link.created_at.isoformat(),
    }


def list_links(
    db: Session, settings: Settings, actor: Principal, dashboard: Dashboard
) -> list[dict[str, Any]]:
    _require_feature(settings)
    acl.require_dashboard(db, dashboard, actor, "share")
    rows = db.execute(
        select(ShareLink, User.display_name)
        .outerjoin(User, User.id == ShareLink.created_by)
        .where(ShareLink.dashboard_id == dashboard.id)
        .order_by(ShareLink.created_at.desc())
    ).all()
    return [link_dict(link, creator) for link, creator in rows]


def create(
    db: Session,
    settings: Settings,
    actor: Principal,
    dashboard: Dashboard,
    *,
    name: str | None,
    expires_in_days: int | None,
    password: str | None,
    attributes: Any,
) -> tuple[dict[str, Any], str]:
    """Make a link. Returns its description and URL; the URL is shown only now."""
    _require_feature(settings)
    acl.require_dashboard(db, dashboard, actor, "publish")
    if dashboard.published_version is None:
        raise ApiError(
            409,
            "not_published",
            "Publish the dashboard first: a share link shows the published version.",
        )
    if attributes == "mine":
        chosen = dict(actor.attributes)
    elif attributes in (None, {}):
        chosen = {}
    else:
        chosen = _check_attributes(attributes)
        if chosen != dict(actor.attributes) and not actor.is_admin:
            raise ApiError(
                403,
                "forbidden",
                "Only an admin can choose the attributes of a share link. Give the link no "
                'attributes (row-level security then shows nothing) or your own ("mine").',
            )
    if expires_in_days is not None and not 1 <= expires_in_days <= MAX_DAYS:
        raise ApiError(422, "invalid_request", f"A link can last from 1 to {MAX_DAYS} days.")
    if password:
        passwords.check_new(password, path="/password")
    token = secrets.token_urlsafe(32)
    link = ShareLink(
        dashboard_id=dashboard.id,
        token_hash=_hash(token),
        prefix=token[:PREFIX_LENGTH],
        name=(name or "").strip()[:100] or None,
        attributes=chosen,
        password_hash=passwords.hash_password(password) if password else None,
        expires_at=datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None,
        created_by=actor.user_id,
    )
    db.add(link)
    db.flush()
    audit.record(
        db,
        action="link.created",
        actor=actor,
        target_type="dashboard",
        target_id=str(dashboard.id),
        detail={
            "link": str(link.id),
            "prefix": link.prefix,
            "name": link.name,
            "password": bool(password),
            "expires_in_days": expires_in_days,
            "attributes": sorted(chosen),
        },
    )
    db.commit()
    return link_dict(link, actor.display_name), url_for(settings, token)


def revoke(db: Session, settings: Settings, actor: Principal, link_id: uuid.UUID) -> dict[str, Any]:
    _require_feature(settings)
    link = db.get(ShareLink, link_id)
    dashboard = db.get(Dashboard, link.dashboard_id) if link else None
    if link is None or dashboard is None:
        raise ApiError(404, "not_found", "Share link not found.")
    acl.require_dashboard(db, dashboard, actor, "share")
    if link.revoked_at is None:
        link.revoked_at = datetime.now(UTC)
        audit.record(
            db,
            action="link.revoked",
            actor=actor,
            target_type="dashboard",
            target_id=str(dashboard.id),
            detail={"link": str(link.id), "prefix": link.prefix},
        )
        db.commit()
    return link_dict(link)


# ---- opening a link ---------------------------------------------------------------------


GONE = "This link is not available. It may have expired or been revoked."


def resolve(db: Session, settings: Settings, token: str) -> tuple[ShareLink, Dashboard]:
    """The link and its dashboard, while the link works."""
    _require_feature(settings)
    link = (
        db.scalar(select(ShareLink).where(ShareLink.token_hash == _hash(token)))
        if len(token) < 100
        else None
    )
    dashboard = db.get(Dashboard, link.dashboard_id) if link else None
    now = datetime.now(UTC)
    if (
        link is None
        or dashboard is None
        or link.revoked_at is not None
        or (link.expires_at is not None and link.expires_at <= now)
        or dashboard.published_version is None
    ):
        raise ApiError(404, "link_not_available", GONE)
    return link, dashboard


def cookie_name(link: ShareLink) -> str:
    return f"opsis_link_{link.id.hex[:12]}"


def unlocked(settings: Settings, link: ShareLink, cookie_values: list[str]) -> bool:
    if link.password_hash is None:
        return True
    for value in cookie_values:
        sealed = crypto.unseal(settings, "share-link", value, UNLOCK_HOURS * 3600)
        if (
            sealed
            and sealed.get("link") == str(link.id)
            and sealed.get("pw") == link.password_hash[-16:]
        ):
            return True
    return False


def unlock(db: Session, settings: Settings, link: ShareLink, password: str, ip: str | None) -> str:
    """Check a link's password, with lockouts. Returns the cookie value to set."""
    keys = [f"link:{link.id}", f"ip:{ip or 'unknown'}"]
    lockout.check(db, keys)
    if not passwords.verify(link.password_hash, password):
        lockout.failed(db, keys)
        db.commit()
        raise ApiError(401, "invalid_password", "That password is not right.")
    lockout.succeeded(db, keys[:1])
    db.commit()
    assert link.password_hash is not None
    return crypto.seal(
        settings, "share-link", {"link": str(link.id), "pw": link.password_hash[-16:]}
    )


def count_view(db: Session, link: ShareLink) -> None:
    db.execute(
        update(ShareLink)
        .where(ShareLink.id == link.id)
        .values(view_count=ShareLink.view_count + 1, last_viewed_at=datetime.now(UTC))
    )
    db.commit()


def principal_for(link: ShareLink) -> Principal:
    """The anonymous viewer behind a link: a viewer who may only view, with the
    link's attributes, and never exempt from a row-level security rule."""
    return Principal(
        user_id=link.id,
        email="",
        display_name="Shared link",
        role=Role.VIEWER,
        auth_method="share_link",
        attributes=dict(link.attributes),
        allowed=frozenset({Permission.DASHBOARDS_VIEW}),
    )
