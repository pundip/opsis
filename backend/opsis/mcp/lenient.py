"""Forgiving agent input: a single value where a list is expected is read as a
list of one (decided at the M7 review).

Models often send `"x": {"field": ..., "grain": "month"}` for `"x": [{...}]`, or
`"values": "Library"` for `"values": ["Library"]`. The agent tools (MCP and the
in-app assistant) normalise such values before validation, so the call succeeds
and what is stored is the strict, canonical form. The REST API and the editor's
JSON tab stay strict: they never pass through here.

Where a list is expected comes from the JSON Schemas themselves, so this follows
them as they change. Each normalisation is logged at debug level, to see which
slips models make.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from opsis.semantic.model import schema_registry

log = logging.getLogger("opsis.agent.lenient")

_KINDS = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
}


def _json_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    for kind, cls in _KINDS.items():
        if isinstance(value, cls):
            return kind
    return "unknown"


def _types(schema: dict[str, Any]) -> set[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return set(declared)
    return set()


def _fits(schema: dict[str, Any], value: Any) -> bool:
    """Whether a value is of a type the schema allows (or the schema does not say)."""
    types = _types(schema)
    if not types:
        if "properties" in schema:
            return isinstance(value, dict)
        if "items" in schema:
            return isinstance(value, list)
        return True
    kind = _json_kind(value)
    return kind in types or (kind == "integer" and "number" in types)


class _Walk:
    def __init__(self, resolver: Any, base: Draft202012Validator) -> None:
        self.base = base
        self.resolver = resolver
        self.found: list[tuple[str, str]] = []

    def resolve(self, schema: Any, resolver: Any) -> tuple[dict[str, Any], Any]:
        while isinstance(schema, dict) and "$ref" in schema:
            resolved = resolver.lookup(schema["$ref"])
            schema, resolver = resolved.contents, resolved.resolver
        return (schema if isinstance(schema, dict) else {}), resolver

    def value(self, schema: Any, value: Any, resolver: Any, path: str) -> Any:
        schema, resolver = self.resolve(schema, resolver)
        if not schema:
            return value
        types = _types(schema)
        if (
            "array" in types
            and value is not None
            and not isinstance(value, list)
            and not _fits({**schema, "type": sorted(types - {"array"})}, value)
        ):
            self.found.append((path or "/", _json_kind(value)))
            value = [value]
        if isinstance(value, list) and isinstance(schema.get("items"), dict):
            value = [
                self.value(schema["items"], item, resolver, f"{path}/{index}")
                for index, item in enumerate(value)
            ]
        elif isinstance(value, dict):
            value = self.members(schema, value, resolver, path)
        for sub in schema.get("allOf") or []:
            value = self.value(sub, value, resolver, path)
        if "if" in schema:
            condition, _ = self.resolve(schema["if"], resolver)
            branch = (
                schema.get("then")
                if self.base.evolve(schema=condition).is_valid(value)
                else (schema.get("else"))
            )
            if branch is not None:
                value = self.value(branch, value, resolver, path)
        for keyword in ("oneOf", "anyOf"):
            for branch in schema.get(keyword) or []:
                contents, inner = self.resolve(branch, resolver)
                if contents and _fits(contents, value):
                    value = self.value(contents, value, inner, path)
                    break
        return value

    def members(
        self, schema: dict[str, Any], value: dict[str, Any], resolver: Any, path: str
    ) -> dict[str, Any]:
        properties = schema.get("properties") or {}
        patterns = schema.get("patternProperties") or {}
        extra = schema.get("additionalProperties")
        fixed: dict[str, Any] = {}
        for key, member in value.items():
            sub = properties.get(key)
            if sub is None:
                sub = next((s for p, s in patterns.items() if re.search(p, key)), None)
            if sub is None and isinstance(extra, dict):
                sub = extra
            fixed[key] = (
                self.value(sub, member, resolver, f"{path}/{key}") if sub is not None else member
            )
        return fixed


def lists_of_one(
    schema: dict[str, Any],
    value: Any,
    *,
    spec_dir: Path | None = None,
    what: str,
    client: str = "an agent",
) -> Any:
    """`value` with every single value that the schema wants as a list wrapped in
    a list of one. `schema` is a tool's own input schema, or (with `spec_dir`) one
    of the spec schemas, whose references are resolved."""
    if spec_dir is not None:
        registry, _ = schema_registry(spec_dir)
    else:
        from referencing import Registry

        registry = Registry()
    base_uri = str(schema.get("$id", ""))
    walk = _Walk(
        registry.resolver(base_uri=base_uri), Draft202012Validator(schema, registry=registry)
    )
    fixed = walk.value(schema, value, walk.resolver, "")
    for path, kind in walk.found:
        log.debug(
            "lenient input: %s sent %s %s at %s %s where a list is expected; read as a list of one",
            client,
            "an" if kind[0] in "aeiou" else "a",
            kind,
            what,
            path,
        )
    return fixed


def spec_lists(spec_dir: Path, name: str, value: Any, *, what: str, client: str) -> Any:
    """`lists_of_one` against one of the spec schemas, by file name."""
    _, schemas = schema_registry(spec_dir)
    return lists_of_one(schemas[name], value, spec_dir=spec_dir, what=what, client=client)
