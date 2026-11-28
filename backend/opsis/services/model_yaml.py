"""YAML for models: the workspace model, its tables, and v1 model files.

Only `true` and `false` are booleans (YAML 1.2's rule): YAML 1.1 also reads on, off,
yes and no as booleans, which would turn the `on:` key of a v1 join into `True`.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from opsis.errors import ApiError

MAX_YAML_BYTES = 500_000
_BOOL_TAG = "tag:yaml.org,2002:bool"
_BOOL_RE = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")


def _strict_booleans(base: type[Any]) -> Any:
    cls: Any = type(f"Opsis{base.__name__}", (base,), {})
    cls.yaml_implicit_resolvers = {
        first: [(tag, rx) for tag, rx in resolvers if tag != _BOOL_TAG]
        for first, resolvers in base.yaml_implicit_resolvers.items()
    }
    cls.add_implicit_resolver(_BOOL_TAG, _BOOL_RE, list("tTfF"))
    return cls


_Loader = _strict_booleans(yaml.SafeLoader)
_Dumper = _strict_booleans(yaml.SafeDumper)


def dump(document: Any) -> str:
    """Key order kept, as the author wrote it."""
    text: str = yaml.dump(document, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)
    return text


def load(text: str, expected: str = "a mapping with keys such as dimensions") -> dict[str, Any]:
    if len(text.encode()) > MAX_YAML_BYTES:
        raise ApiError(413, "too_large", "The YAML document is too large.")
    try:
        document = yaml.load(text, Loader=_Loader)  # noqa: S506  a SafeLoader subclass
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        problem = getattr(exc, "problem", None) or "it could not be parsed"
        raise ApiError(422, "invalid_yaml", f"The YAML is not valid: {problem}{where}.") from None
    if not isinstance(document, dict):
        raise ApiError(422, "invalid_yaml", f"The YAML must be {expected}.")
    return document
