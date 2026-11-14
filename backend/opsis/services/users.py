"""User lookups and the built-in admin used by AUTH_MODE=none."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.auth.principal import Principal, Role
from opsis.db.models import User
from opsis.services import audit

BUILTIN_ADMIN_EMAIL = "admin@localhost"


def get_builtin_admin(db: Session) -> User | None:
    return db.scalars(select(User).where(User.is_builtin, User.is_active)).first()


def ensure_builtin_admin(db: Session) -> User:
    """Create the built-in admin on first start. Has no password, so it can
    never sign in through the local or Google flows."""
    user = get_builtin_admin(db)
    if user is None:
        user = User(
            email=BUILTIN_ADMIN_EMAIL,
            display_name="Local admin",
            role=Role.ADMIN.value,
            is_builtin=True,
        )
        db.add(user)
        db.flush()
        audit.record(
            db, action="user.builtin_admin_created", target_type="user", target_id=str(user.id)
        )
        db.commit()
    return user


def to_principal(user: User, auth_method: str) -> Principal:
    return Principal(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=Role(user.role),
        auth_method=auth_method,
        attributes=dict(user.attributes),
    )
