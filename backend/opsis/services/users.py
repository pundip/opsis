"""User lookups, the built-in admin used by AUTH_MODE=none, and principals."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.auth.principal import Principal, Role
from opsis.db.models import GroupMember, User
from opsis.services import audit

BUILTIN_ADMIN_EMAIL = "admin@localhost"


def get_builtin_admin(db: Session) -> User | None:
    return db.scalars(select(User).where(User.is_builtin, User.is_active)).first()


def ensure_builtin_admin(db: Session) -> User:
    """Create (or bring back) the built-in admin. It has no password, so it can
    never sign in through the local or Google flows. In sign-in modes it is
    switched off at startup (see accounts.ensure_first_admin)."""
    user = db.scalars(select(User).where(User.is_builtin)).first()
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
    elif not user.is_active:
        user.is_active = True
        db.commit()
    return user


def to_principal(user: User, auth_method: str) -> Principal:
    """A principal without group memberships. Prefer `principal_for`."""
    return Principal(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=Role(user.role),
        auth_method=auth_method,
        attributes=dict(user.attributes),
        must_change_password=user.must_change_password,
    )


def principal_for(
    db: Session, user: User, auth_method: str, *, session_id: str | None = None
) -> Principal:
    """The principal for a user, with the groups they belong to (for sharing)."""
    groups = db.scalars(select(GroupMember.group_id).where(GroupMember.user_id == user.id)).all()
    return Principal(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=Role(user.role),
        auth_method=auth_method,
        groups=frozenset(str(group) for group in groups),
        attributes=dict(user.attributes),
        session_id=session_id,
        must_change_password=user.must_change_password,
    )
