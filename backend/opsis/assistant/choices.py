"""What a person may use in the assistant, what they chose, and the provider for one
request (v2 V1, ADR-036).

The options, in the order the picker shows them:

- their own keys ("key:<id>"), unless an admin has switched personal keys off;
- "workspace": the workspace's Anthropic key (ANTHROPIC_API_KEY), only for people
  an admin allows (off by default; in local mode the one built-in admin may);
- "ollama": the workspace's Ollama, when it is on (OLLAMA_ENABLED).

A request uses the person's remembered choice, or the workspace's default
(ASSISTANT_PROVIDER), or the first option. The choice is checked again before every
model call, so taking away a grant, deleting a key or switching personal keys off
stops a request at its next step. A key is decrypted only to build the provider.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx2
from sqlalchemy.orm import Session

from opsis.assistant.providers import (
    WORKSPACE_KEY,
    AnthropicProvider,
    KeySource,
    OllamaProvider,
    OpenAIProvider,
    Provider,
)
from opsis.auth.principal import Principal
from opsis.db.models import AssistantChoice, UserApiKey
from opsis.errors import ApiError
from opsis.services import api_keys, grants
from opsis.services.crypto import SecretsError
from opsis.settings import Settings

COMPANY = {"anthropic": "Anthropic", "openai": "OpenAI"}


@dataclass(frozen=True)
class Option:
    id: str  # "key:<uuid>" | "workspace" | "ollama"
    source: str  # own | workspace | ollama
    provider: str  # anthropic | openai | ollama
    label: str
    model: str | None  # the default model; None means the person must name one
    fixed: bool  # the model is the admin's choice (workspace key, Ollama)
    key_id: uuid.UUID | None = None
    key_label: str | None = None
    last4: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "provider": self.provider,
            "label": self.label,
            "model": self.model,
            "fixedModel": self.fixed,
            "keyId": str(self.key_id) if self.key_id else None,
            "last4": self.last4,
        }


@dataclass(frozen=True)
class Selected:
    option: Option
    model: str | None  # None: the person has to name a model for their key


def may_use_workspace_key(db: Session, settings: Settings, user_id: uuid.UUID) -> bool:
    if not settings.workspace_key_on:
        return False
    if not settings.sign_in:
        return True  # local mode: the built-in admin is the only person
    return grants.allowed(db, grants.WORKSPACE_KEY, user_id)


def _own(row: UserApiKey, settings: Settings) -> Option:
    company = COMPANY.get(row.provider, row.provider)
    default = row.default_model or (
        settings.anthropic_model.strip() if row.provider == "anthropic" else None
    )
    return Option(
        id=f"key:{row.id}",
        source="own",
        provider=row.provider,
        label=f"{row.label} (your {company} key ending {row.last4})",
        model=default,
        fixed=False,
        key_id=row.id,
        key_label=row.label,
        last4=row.last4,
    )


def options_for(db: Session, settings: Settings, user_id: uuid.UUID) -> list[Option]:
    found: list[Option] = []
    if api_keys.byo_on(db):
        found += [_own(row, settings) for row in api_keys.owned(db, user_id)]
    if may_use_workspace_key(db, settings, user_id):
        found.append(
            Option(
                "workspace",
                "workspace",
                "anthropic",
                "The workspace's Claude key",
                settings.anthropic_model.strip(),
                True,
            )
        )
    if settings.ollama_on:
        found.append(
            Option("ollama", "ollama", "ollama", "Ollama", settings.ollama_model.strip(), True)
        )
    return found


def remembered(db: Session, user_id: uuid.UUID) -> AssistantChoice | None:
    return db.get(AssistantChoice, user_id)


def current(db: Session, settings: Settings, user_id: uuid.UUID) -> Selected | None:
    """What the person's next request will use, or None when nothing is available or
    what they chose is gone."""
    return situation(db, settings, user_id)[0]


def situation(
    db: Session, settings: Settings, user_id: uuid.UUID
) -> tuple[Selected | None, str | None]:
    """(what the next request uses, why a remembered choice can no longer be used).

    A choice that is gone (a grant taken away, a key deleted) is never swapped for
    another option behind the person's back, which could be a key they pay for:
    they choose again."""
    options = options_for(db, settings, user_id)
    if not options:
        return None, None
    by_id = {option.id: option for option in options}
    stored = remembered(db, user_id)
    if stored is not None:
        if stored.option not in by_id:
            return None, _gone(db, stored.option) + " Choose what the assistant should use."
        option = by_id[stored.option]
        model = option.model if option.fixed else (stored.model or option.model)
        return Selected(option, model), None
    preferred = "workspace" if settings.assistant_provider == "anthropic" else "ollama"
    option = by_id.get(preferred) or options[0]
    return Selected(option, option.model), None


def _gone(db: Session, option_id: str) -> str:
    if option_id == "workspace":
        return "You may no longer use the workspace's key."
    if option_id == "ollama":
        return "Ollama is no longer available."
    if not api_keys.byo_on(db):
        return "An admin has switched off personal API keys."
    return "The key you chose was deleted."


def choose(
    db: Session, settings: Settings, actor: Principal, option_id: str, model: str | None
) -> Selected:
    """Remember the person's choice. A model is theirs to name only for their own key."""
    by_id = {option.id: option for option in options_for(db, settings, actor.user_id)}
    option = by_id.get(option_id)
    if option is None:
        raise ApiError(
            404, "not_found", "That is not one of the options you may use in the assistant."
        )
    chosen = None if option.fixed else api_keys.clean_model(model)
    row = remembered(db, actor.user_id)
    if row is None:
        db.add(AssistantChoice(user_id=actor.user_id, option=option.id, model=chosen))
    else:
        row.option, row.model = option.id, chosen
    db.commit()
    return Selected(option, option.model if option.fixed else (chosen or option.model))


def unavailable_reason(db: Session, settings: Settings, user_id: uuid.UUID) -> str:
    """Why someone has nothing to use, written for them."""
    if api_keys.byo_on(db):
        if settings.workspace_key_on and settings.sign_in:
            return (
                "Add your own Anthropic or OpenAI API key on your Account page, or ask an admin "
                "to let you use the workspace's key."
            )
        return "Add your own Anthropic or OpenAI API key on your Account page."
    if settings.workspace_key_on:
        return "Ask an admin to let you use the workspace's key."
    return "Ask an admin: no AI provider is available in this workspace."


def still_allowed(
    db: Session, settings: Settings, user_id: uuid.UUID, option_id: str
) -> str | None:
    """None while the option may still be used; otherwise why not, for the person."""
    if option_id in {option.id for option in options_for(db, settings, user_id)}:
        return None
    return _gone(db, option_id).removesuffix(".") + ", so the assistant stopped."


@dataclass(frozen=True)
class Resolved:
    """One request's provider settings: the option, its model and (for keys) whose."""

    option: Option
    model: str
    secret: str | None  # the decrypted key: only inside the backend, for this request


def resolve(db: Session, settings: Settings, actor: Principal) -> Resolved:
    selected, lost = situation(db, settings, actor.user_id)
    if lost is not None:
        raise ApiError(409, "choice_unavailable", lost)
    if selected is None:
        raise ApiError(
            409,
            "no_assistant_option",
            f"You have no AI provider to use. {unavailable_reason(db, settings, actor.user_id)}",
        )
    if not selected.model:
        raise ApiError(
            409,
            "no_model",
            "Choose a model for your key in the assistant, or give the key a default model on "
            "your Account page.",
        )
    secret: str | None = None
    option = selected.option
    if option.key_id is not None:
        row = db.get(UserApiKey, option.key_id)
        if row is None or row.user_id != actor.user_id:  # pragma: no cover - just listed
            raise ApiError(409, "no_assistant_option", "Your key was deleted.")
        try:
            secret = api_keys.secret(settings, row)
        except SecretsError:
            raise ApiError(
                409,
                "key_unreadable",
                "Your key cannot be read with the current ENCRYPTION_KEY. Add it again on your "
                "Account page.",
            ) from None
    return Resolved(option, selected.model, secret)


def build(
    settings: Settings, resolved: Resolved, transport: httpx2.AsyncBaseTransport | None = None
) -> Provider:
    option = resolved.option
    if option.source == "ollama":
        return OllamaProvider(settings, transport)
    if option.source == "workspace":
        return AnthropicProvider(settings, transport, model=resolved.model, source=WORKSPACE_KEY)
    source = KeySource(
        f'your {COMPANY.get(option.provider, option.provider)} key "{option.key_label}"',
        "Check it on your Account page, or choose another in the assistant.",
    )
    assert resolved.secret is not None
    if option.provider == "openai":
        return OpenAIProvider(
            settings, transport, api_key=resolved.secret, model=resolved.model, source=source
        )
    return AnthropicProvider(
        settings, transport, api_key=resolved.secret, model=resolved.model, source=source
    )
