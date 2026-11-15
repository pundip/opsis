"""Who is making a request, and what they may do.

Every auth mode (none, local, full, tokens, share links) resolves to a
Principal, and every protected operation checks a Permission. AUTH_MODE=none
uses the same checks with a built-in admin, so the code paths are identical.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class Permission(StrEnum):
    USERS_MANAGE = "users:manage"
    SETTINGS_MANAGE = "settings:manage"
    AUDIT_VIEW = "audit:view"
    CONNECTIONS_MANAGE = "connections:manage"
    SQL_LAB_USE = "sql_lab:use"
    DATASETS_EDIT = "datasets:edit"
    MODELS_EDIT = "models:edit"
    DASHBOARDS_EDIT = "dashboards:edit"
    DASHBOARDS_PUBLISH = "dashboards:publish"
    DASHBOARDS_VIEW = "dashboards:view"


_VIEWER = frozenset({Permission.DASHBOARDS_VIEW})
_EDITOR = _VIEWER | {
    Permission.DATASETS_EDIT,
    Permission.MODELS_EDIT,
    Permission.DASHBOARDS_EDIT,
    Permission.DASHBOARDS_PUBLISH,
}
ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = {
    Role.VIEWER: _VIEWER,
    Role.EDITOR: _EDITOR,
    Role.ADMIN: frozenset(Permission),
}


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    email: str
    display_name: str
    role: Role
    auth_method: str  # "none" | "session" | "token" | "share_link"
    groups: frozenset[str] = frozenset()
    attributes: Mapping[str, Any] = field(default_factory=dict)
    # None means unrestricted. Personal access tokens narrow this (M5).
    allowed: frozenset[Permission] | None = None
    # None means not limited by token scopes (a signed-in person, or MCP_AUTH=none).
    # A personal access token sets this to its scopes: read, edit, publish, admin.
    scopes: frozenset[str] | None = None

    def has_scope(self, scope: str) -> bool:
        return self.scopes is None or scope in self.scopes

    @property
    def permissions(self) -> frozenset[Permission]:
        granted = ROLE_PERMISSIONS[self.role]
        return granted if self.allowed is None else granted & self.allowed

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions
