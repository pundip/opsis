"""People: the first admin, invitations, password sign-in and resets, and the
changes an admin makes to users. Sign-in itself (sessions, Google) builds on this.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from opsis.auth import passwords
from opsis.auth.principal import Principal, Role
from opsis.db.models import AccessToken, Group, GroupMember, User, UserLink
from opsis.errors import ApiError
from opsis.services import audit, lockout, sessions
from opsis.settings import Settings

log = logging.getLogger("opsis.accounts")

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")
INVITE_DAYS = 7
RESET_HOURS = 24
MAX_ATTRIBUTES_BYTES = 4000


def normalise_email(email: str, path: str = "/email") -> str:
    value = email.strip().lower()
    if not EMAIL_RE.match(value):
        raise ApiError(
            422,
            "invalid_email",
            "Enter a valid email address.",
            [{"path": path, "message": "Not an email address."}],
        )
    return value


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _check_attributes(attributes: Any) -> dict[str, Any]:
    """User attributes feed row-level security rules: a flat object of text,
    numbers, booleans, or lists of those."""
    if not isinstance(attributes, dict):
        raise ApiError(
            422,
            "invalid_attributes",
            'Attributes must be an object, for example {"regions": ["VIC"]}.',
        )
    for key, value in attributes.items():
        values = value if isinstance(value, list) else [value]
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$", str(key)) or not all(
            isinstance(v, str | int | float | bool) for v in values
        ):
            raise ApiError(
                422,
                "invalid_attributes",
                f"Attribute {key!r} must be named like an identifier and hold text, a number, "
                "true or false, or a list of those.",
                [{"path": f"/attributes/{key}", "message": "Not a usable attribute."}],
            )
    if len(str(attributes)) > MAX_ATTRIBUTES_BYTES:
        raise ApiError(422, "invalid_attributes", "The attributes are too large.")
    return dict(attributes)


# ---- the first admin ----------------------------------------------------------------


def ensure_first_admin(db: Session, settings: Settings) -> None:
    """In sign-in modes, make sure an admin exists, from ADMIN_EMAIL and
    ADMIN_INITIAL_PASSWORD. Also switches off the built-in admin of AUTH_MODE=none,
    so nothing made without sign-in (such as its access tokens) keeps working."""
    if not settings.sign_in:
        return
    for builtin in db.scalars(select(User).where(User.is_builtin, User.is_active)).all():
        builtin.is_active = False
        audit.record(
            db, action="user.builtin_admin_disabled", target_type="user", target_id=str(builtin.id)
        )
    existing = db.scalar(
        select(User.id)
        .where(User.role == Role.ADMIN.value, User.is_active, User.is_builtin.is_(False))
        .limit(1)
    )
    if existing is not None:
        db.commit()
        return
    email = normalise_email(settings.admin_email)
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, display_name="Admin", role=Role.ADMIN.value)
        db.add(user)
    user.role = Role.ADMIN.value
    user.is_active = True
    password = settings.admin_initial_password
    if password is not None and password.get_secret_value():
        user.password_hash = passwords.hash_password(password.get_secret_value())
        user.must_change_password = True
    db.flush()
    audit.record(
        db,
        action="user.first_admin",
        target_type="user",
        target_id=str(user.id),
        detail={"email": email},
    )
    db.commit()
    if user.password_hash is None:
        log.warning(
            "No admin can sign in with a password yet. Set ADMIN_INITIAL_PASSWORD and restart, "
            "or run: docker compose exec api python -m opsis reset-link %s",
            email,
        )


# ---- one-time links -----------------------------------------------------------------


def make_link(
    db: Session, settings: Settings, user: User, purpose: str, actor: Principal | None
) -> str:
    """A link to set a password: an invitation (7 days) or a reset (24 hours).
    Any earlier unused link for the same user stops working. The caller commits."""
    db.execute(delete(UserLink).where(UserLink.user_id == user.id, UserLink.used_at.is_(None)))
    token = secrets.token_urlsafe(32)
    lifetime = timedelta(days=INVITE_DAYS) if purpose == "invite" else timedelta(hours=RESET_HOURS)
    db.add(
        UserLink(
            user_id=user.id,
            purpose=purpose,
            token_hash=_hash(token),
            expires_at=datetime.now(UTC) + lifetime,
            created_by=actor.user_id if actor else None,
        )
    )
    return f"{settings.public_base_url}/set-password/{token}"


def _link(db: Session, token: str) -> tuple[UserLink, User]:
    row = db.scalar(select(UserLink).where(UserLink.token_hash == _hash(token)))
    user = db.get(User, row.user_id) if row else None
    if row is None or user is None or row.used_at is not None:
        raise ApiError(
            404,
            "link_not_valid",
            "This link is not valid. It may have been used already, or replaced by a newer one. "
            "Ask an admin for a new link.",
        )
    if row.expires_at <= datetime.now(UTC):
        raise ApiError(410, "link_expired", "This link has expired. Ask an admin for a new link.")
    return row, user


def peek_link(db: Session, token: str) -> dict[str, Any]:
    row, user = _link(db, token)
    return {"email": user.email, "displayName": user.display_name, "purpose": row.purpose}


def use_link(db: Session, token: str, password: str, display_name: str | None) -> User:
    """Set a password through a link. Ends the user's other sessions. The caller commits."""
    row, user = _link(db, token)
    passwords.check_new(password, email=user.email)
    user.password_hash = passwords.hash_password(password)
    user.must_change_password = False
    if row.purpose == "invite":
        user.is_active = True
    if display_name and display_name.strip():
        user.display_name = display_name.strip()[:200]
    row.used_at = datetime.now(UTC)
    sessions.revoke_all(db, user.id)
    lockout.succeeded(db, [f"email:{user.email}"])
    audit.record(
        db,
        action=f"user.password_set_by_{row.purpose}",
        target_type="user",
        target_id=str(user.id),
        actor_type="user",
        detail={"email": user.email},
    )
    return user


# ---- password sign-in ---------------------------------------------------------------


def sign_in_with_password(db: Session, email: str, password: str, ip: str | None) -> User:
    """Check an email and password, with lockouts. The caller commits."""
    address = email.strip().lower()[:320]
    keys = [f"email:{address}", f"ip:{ip or 'unknown'}"]
    lockout.check(db, keys)
    user = db.scalar(select(User).where(User.email == address))
    usable = user is not None and user.is_active and not user.is_builtin
    if not passwords.verify(user.password_hash if usable and user else None, password):
        lockout.failed(db, keys)
        audit.record(
            db,
            action="auth.login_failed",
            actor=None,
            target_type="user",
            target_id=str(user.id) if user else None,
            detail={"email": address, "method": "password"},
            ip=ip,
        )
        db.commit()
        raise ApiError(401, "invalid_credentials", "The email or password is not right.")
    assert user is not None and user.password_hash is not None
    lockout.succeeded(db, [keys[0]])
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
    return user


def change_password(db: Session, principal: Principal, current: str, new: str) -> None:
    user = db.get(User, principal.user_id)
    if user is None:
        raise ApiError(404, "not_found", "User not found.")
    if not passwords.verify(user.password_hash, current):
        raise ApiError(
            422,
            "invalid_credentials",
            "The current password is not right.",
            [{"path": "/currentPassword", "message": "Not your current password."}],
        )
    if current == new:
        raise ApiError(
            422,
            "weak_password",
            "Choose a password different from the current one.",
            [{"path": "/newPassword", "message": "Same as the current password."}],
        )
    passwords.check_new(new, email=user.email, path="/newPassword")
    user.password_hash = passwords.hash_password(new)
    user.must_change_password = False
    # Every other session ends; this one continues.
    ended = sessions.revoke_all(db, user.id, keep=principal.session_id)
    audit.record(
        db,
        action="user.password_changed",
        actor=principal,
        target_type="user",
        target_id=str(user.id),
        detail={"other_sessions_ended": ended},
    )
    db.commit()


# ---- admin: users ---------------------------------------------------------------------


def _status(user: User) -> str:
    if not user.is_active:
        return (
            "deactivated"
            if user.password_hash or user.google_sub or user.last_login_at
            else "invited"
        )
    if user.password_hash is None and user.google_sub is None and user.last_login_at is None:
        return "invited"
    return "active"


def user_dict(user: User, groups: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "email": user.email,
        "displayName": user.display_name,
        "role": user.role,
        "status": _status(user),
        "isActive": user.is_active,
        "hasPassword": user.password_hash is not None,
        "google": user.google_sub is not None,
        "mustChangePassword": user.must_change_password,
        "attributes": user.attributes,
        "groups": groups or [],
        "lastLoginAt": user.last_login_at.isoformat() if user.last_login_at else None,
        "createdAt": user.created_at.isoformat(),
    }


def list_users(db: Session) -> list[dict[str, Any]]:
    users = db.scalars(
        select(User).where(User.is_builtin.is_(False)).order_by(func.lower(User.display_name))
    ).all()
    memberships: dict[uuid.UUID, list[dict[str, str]]] = {}
    for user_id, group_id, name in db.execute(
        select(GroupMember.user_id, Group.id, Group.name)
        .join(Group, Group.id == GroupMember.group_id)
        .order_by(Group.name)
    ).all():
        memberships.setdefault(user_id, []).append({"id": str(group_id), "name": name})
    return [user_dict(user, memberships.get(user.id)) for user in users]


def _get(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None or user.is_builtin:
        raise ApiError(404, "not_found", "User not found.")
    return user


def _set_groups(db: Session, user: User, group_ids: list[uuid.UUID]) -> None:
    known = set(db.scalars(select(Group.id).where(Group.id.in_(group_ids))).all())
    missing = [str(g) for g in group_ids if g not in known]
    if missing:
        raise ApiError(422, "invalid_request", f"Unknown group: {', '.join(missing)}.")
    db.execute(delete(GroupMember).where(GroupMember.user_id == user.id))
    for group_id in dict.fromkeys(group_ids):
        db.add(GroupMember(group_id=group_id, user_id=user.id))


def _other_active_admins(db: Session, user: User) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.role == Role.ADMIN.value,
                User.is_active,
                User.is_builtin.is_(False),
                User.id != user.id,
            )
        )
        or 0
    )


def invite(
    db: Session,
    settings: Settings,
    actor: Principal,
    *,
    email: str,
    display_name: str,
    role: str,
    attributes: Any,
    group_ids: list[uuid.UUID],
) -> tuple[dict[str, Any], str]:
    """Add a user and return their invitation link (shown once). With Google
    sign-in on, the person may also simply sign in with Google as that email."""
    address = normalise_email(email)
    if db.scalar(select(User.id).where(User.email == address)) is not None:
        raise ApiError(409, "user_exists", f"There is already a user with the email {address}.")
    user = User(
        email=address,
        display_name=display_name.strip()[:200] or address.split("@")[0],
        role=Role(role).value,
        attributes=_check_attributes(attributes or {}),
    )
    db.add(user)
    db.flush()
    _set_groups(db, user, group_ids)
    url = make_link(db, settings, user, "invite", actor)
    audit.record(
        db,
        action="user.invited",
        actor=actor,
        target_type="user",
        target_id=str(user.id),
        detail={"email": address, "role": user.role},
    )
    db.commit()
    return user_dict(user), url


def update_user(
    db: Session, actor: Principal, user_id: uuid.UUID, changes: dict[str, Any]
) -> dict[str, Any]:
    user = _get(db, user_id)
    before = {"role": user.role, "isActive": user.is_active}
    role = changes.get("role")
    active = changes.get("isActive")
    losing_admin = user.role == Role.ADMIN.value and (
        (role is not None and role != Role.ADMIN.value) or active is False
    )
    if losing_admin and _other_active_admins(db, user) == 0:
        raise ApiError(
            409,
            "last_admin",
            "Opsis needs at least one active admin. Make someone else an admin first.",
        )
    if active is False and user.id == actor.user_id:
        raise ApiError(409, "cannot_deactivate_self", "You cannot deactivate your own account.")
    if changes.get("displayName") is not None:
        user.display_name = str(changes["displayName"]).strip()[:200] or user.display_name
    if role is not None:
        user.role = Role(role).value
    if changes.get("attributes") is not None:
        user.attributes = _check_attributes(changes["attributes"])
    if changes.get("groupIds") is not None:
        _set_groups(db, user, list(changes["groupIds"]))
    if active is not None:
        user.is_active = bool(active)
        if not user.is_active:
            sessions.revoke_all(db, user.id)
            db.execute(
                delete(UserLink).where(UserLink.user_id == user.id, UserLink.used_at.is_(None))
            )
    after = {"role": user.role, "isActive": user.is_active}
    detail: dict[str, Any] = {
        "email": user.email,
        **{k: [before[k], after[k]] for k in after if before[k] != after[k]},
    }
    if changes.get("attributes") is not None:
        detail["attributes_changed"] = True
    if changes.get("groupIds") is not None:
        detail["groups_changed"] = True
    audit.record(
        db,
        action="user.updated",
        actor=actor,
        target_type="user",
        target_id=str(user.id),
        detail=detail,
    )
    db.commit()
    return next(u for u in list_users(db) if u["id"] == str(user.id))


def password_link(
    db: Session, settings: Settings, actor: Principal, user_id: uuid.UUID
) -> dict[str, str]:
    """A new invitation link for someone who has never signed in, otherwise a
    password reset link: {url, purpose, email, name}. Emailed to them with
    FEATURE_EMAIL, otherwise shown once to the admin, who passes it on."""
    user = _get(db, user_id)
    if not user.is_active and _status(user) != "invited":
        raise ApiError(409, "deactivated", "This user is deactivated. Reactivate them first.")
    purpose = "invite" if _status(user) == "invited" else "reset"
    url = make_link(db, settings, user, purpose, actor)
    audit.record(
        db,
        action=f"user.{purpose}_link_created",
        actor=actor,
        target_type="user",
        target_id=str(user.id),
        detail={"email": user.email},
    )
    db.commit()
    return {"url": url, "purpose": purpose, "email": user.email, "name": user.display_name}


def forgot_password(
    db: Session, settings: Settings, email: str, ip: str | None
) -> dict[str, str] | None:
    """A reset link for the sign-in page's "Forgot your password?", to be emailed.

    Every request counts against the address and the client, so this cannot be
    used to flood a mailbox. None when there is no such active person: the
    caller answers the same either way, so addresses cannot be probed."""
    try:
        address = normalise_email(email)
    except ApiError:
        return None
    keys = [f"forgot:{address}", f"forgotip:{ip or 'unknown'}"]
    try:
        lockout.check(db, keys)
    except ApiError as exc:
        raise ApiError(
            429, "too_many_requests", exc.message.replace("failed attempts", "requests")
        ) from None
    lockout.failed(db, keys)
    db.commit()
    user = db.scalar(select(User).where(User.email == address))
    if user is None or user.is_builtin or not user.is_active:
        return None
    purpose = "invite" if _status(user) == "invited" else "reset"
    url = make_link(db, settings, user, purpose, None)
    audit.record(
        db,
        action="user.reset_requested",
        target_type="user",
        target_id=str(user.id),
        detail={"email": user.email},
        ip=ip,
    )
    db.commit()
    return {"url": url, "purpose": purpose, "email": user.email, "name": user.display_name}


def reset_link_for_email(db: Session, settings: Settings, email: str) -> str:
    """For the command line (`python -m opsis reset-link <email>`): recovery when
    no admin can sign in. Whoever can run commands on the server already controls it."""
    user = db.scalar(select(User).where(User.email == normalise_email(email)))
    if user is None or user.is_builtin:
        raise ApiError(404, "not_found", f"There is no user with the email {email}.")
    user.is_active = True
    purpose = "invite" if _status(user) == "invited" else "reset"
    url = make_link(db, settings, user, purpose, None)
    audit.record(
        db,
        action="user.reset_link_from_cli",
        target_type="user",
        target_id=str(user.id),
        detail={"email": user.email},
    )
    db.commit()
    return url


def token_count(db: Session, user_id: uuid.UUID) -> int:
    return len(db.scalars(select(AccessToken.id).where(AccessToken.user_id == user_id)).all())


# ---- Sign in with Google --------------------------------------------------------------


def sign_in_with_google(
    db: Session, settings: Settings, claims: dict[str, Any], ip: str | None
) -> User:
    """Find or link the account for verified Google claims. The caller commits.

    Raises GoogleSignInError with a code the sign-in page explains."""
    from opsis.auth.google import GoogleSignInError, allowed  # avoid a cycle at import time

    email = str(claims.get("email") or "").strip().lower()
    sub = str(claims.get("sub") or "")

    def refuse(code: str, message: str, user: User | None = None) -> GoogleSignInError:
        audit.record(
            db,
            action="auth.login_failed",
            target_type="user",
            target_id=str(user.id) if user else None,
            detail={"email": email, "method": "google", "reason": code},
            ip=ip,
        )
        db.commit()
        return GoogleSignInError(code, message)

    if not email or claims.get("email_verified") not in (True, "true"):
        raise refuse(
            "google_unverified", "Google has not verified the email address of this account."
        )
    if not allowed(settings, email):
        raise refuse("google_not_allowed", f"{email} is not allowed to sign in to this workspace.")
    user = db.scalar(select(User).where(User.google_sub == sub)) or db.scalar(
        select(User).where(User.email == email, User.is_builtin.is_(False))
    )
    if user is None:
        if not settings.google_auto_provision:
            raise refuse(
                "google_not_invited",
                f"{email} has not been invited. Ask an admin to invite this address.",
            )
        user = User(
            email=email,
            display_name=str(claims.get("name") or email.split("@")[0])[:200],
            role=Role.VIEWER.value,
        )
        db.add(user)
        db.flush()
        audit.record(
            db,
            action="user.auto_provisioned",
            target_type="user",
            target_id=str(user.id),
            detail={"email": email},
        )
    elif not user.is_active and _status(user) != "invited":
        raise refuse("google_deactivated", "This account has been deactivated. Ask an admin.", user)
    if user.google_sub not in (None, sub):
        raise refuse("google_failed", "This account is linked to a different Google account.", user)
    # The email matched and Google verified it, so the account can be linked.
    user.google_sub = sub
    user.is_active = True
    db.execute(
        delete(UserLink).where(
            UserLink.user_id == user.id, UserLink.purpose == "invite", UserLink.used_at.is_(None)
        )
    )
    return user
