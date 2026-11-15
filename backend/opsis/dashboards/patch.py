"""JSON Patch (RFC 6902) for agent edits to dashboards and models.

Written here rather than imported so that every failure names the operation
it happened in and says what to do instead. An agent reads these messages and
corrects itself, so they matter as much as the result.

The input is never modified: `apply` works on a deep copy.
"""

from __future__ import annotations

import copy
from typing import Any

from opsis.errors import ApiError

MAX_OPS = 200
OPS = ("add", "remove", "replace", "move", "copy", "test")


class PatchError(Exception):
    def __init__(self, index: int, message: str) -> None:
        self.index = index
        self.message = message
        super().__init__(message)


def _tokens(index: int, pointer: Any, name: str = "path") -> list[str]:
    if not isinstance(pointer, str):
        raise PatchError(index, f'`{name}` must be a string such as "/items/0/title".')
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise PatchError(
            index,
            f'`{name}` must start with "/" (got {pointer!r}). '
            'Example: "/items/0/title", or "/items/-" to append to the items list.',
        )
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _describe(node: Any) -> str:
    if isinstance(node, dict):
        keys = ", ".join(list(node)[:12])
        return f"an object with the keys: {keys}" if keys else "an empty object"
    if isinstance(node, list):
        return f"a list of {len(node)} item(s)"
    return f"the value {node!r:.60}"


def _list_index(index: int, token: str, size: int, pointer: str, *, allow_end: bool) -> int:
    if token == "-":
        if allow_end:
            return size
        raise PatchError(
            index, f'"-" means "after the last item" and only works with `add` ({pointer}).'
        )
    if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
        raise PatchError(
            index,
            f"{pointer}: {token!r} is not a list position. Use a number from 0 to "
            f"{max(size - 1, 0)}{', or "-" to append' if allow_end else ''}.",
        )
    position = int(token)
    limit = size if allow_end else size - 1
    if position > limit:
        raise PatchError(
            index,
            f"{pointer}: position {position} does not exist; the list has {size} item(s)"
            + (f", so the last position is {size - 1}." if size else "."),
        )
    return position


def _walk(index: int, document: Any, tokens: list[str], pointer: str) -> Any:
    """The node at `tokens`. Raises with the deepest path that does exist."""
    node = document
    for depth, token in enumerate(tokens):
        here = "/" + "/".join(tokens[: depth + 1])
        if isinstance(node, dict):
            if token not in node:
                parent = "/" + "/".join(tokens[:depth]) if depth else "The document root"
                raise PatchError(
                    index,
                    f"{pointer}: there is nothing at {here}. {parent} is {_describe(node)}.",
                )
            node = node[token]
        elif isinstance(node, list):
            node = node[_list_index(index, token, len(node), here, allow_end=False)]
        else:
            raise PatchError(
                index,
                f"{pointer}: {here} cannot be reached because its parent is {_describe(node)}.",
            )
    return node


def _get(index: int, document: Any, pointer: str, name: str = "path") -> Any:
    return _walk(index, document, _tokens(index, pointer, name), pointer)


def _add(index: int, document: Any, pointer: str, value: Any) -> Any:
    tokens = _tokens(index, pointer)
    if not tokens:
        return value
    parent = _walk(index, document, tokens[:-1], pointer)
    last = tokens[-1]
    if isinstance(parent, dict):
        parent[last] = value
    elif isinstance(parent, list):
        parent.insert(_list_index(index, last, len(parent), pointer, allow_end=True), value)
    else:
        raise PatchError(index, f"{pointer}: cannot add inside {_describe(parent)}.")
    return document


def _remove(index: int, document: Any, pointer: str) -> tuple[Any, Any]:
    tokens = _tokens(index, pointer)
    if not tokens:
        raise PatchError(index, "The whole document cannot be removed.")
    parent = _walk(index, document, tokens[:-1], pointer)
    last = tokens[-1]
    if isinstance(parent, dict):
        if last not in parent:
            raise PatchError(
                index, f"{pointer}: there is nothing to remove. Its parent is {_describe(parent)}."
            )
        return document, parent.pop(last)
    if isinstance(parent, list):
        return document, parent.pop(_list_index(index, last, len(parent), pointer, allow_end=False))
    raise PatchError(index, f"{pointer}: cannot remove from {_describe(parent)}.")


def apply(document: Any, ops: Any) -> Any:
    """Return a patched copy of `document`.

    Raises ApiError 422 `invalid_patch` whose details point at /ops/<n>. Either
    every operation applies or none does.
    """
    if not isinstance(ops, list) or not ops:
        raise ApiError(
            422,
            "invalid_patch",
            "`ops` must be a non-empty list of JSON Patch operations, for example "
            '[{"op": "replace", "path": "/title", "value": "New title"}].',
            [{"path": "/ops", "message": "Expected a non-empty list."}],
        )
    if len(ops) > MAX_OPS:
        raise ApiError(
            422,
            "invalid_patch",
            f"Too many operations ({len(ops)}). Send at most {MAX_OPS} in one call.",
            [{"path": "/ops", "message": f"At most {MAX_OPS} operations."}],
        )
    result = copy.deepcopy(document)
    try:
        for index, op in enumerate(ops):
            result = _apply_one(index, result, op)
    except PatchError as exc:
        raise ApiError(
            422,
            "invalid_patch",
            f"Operation {exc.index} could not be applied, so nothing was changed. {exc.message}",
            [{"path": f"/ops/{exc.index}", "message": exc.message}],
        ) from None
    return result


def _apply_one(index: int, document: Any, op: Any) -> Any:
    if not isinstance(op, dict):
        raise PatchError(index, 'Each operation must be an object such as {"op": "add", ...}.')
    kind = op.get("op")
    if kind not in OPS:
        raise PatchError(index, f"`op` must be one of: {', '.join(OPS)} (got {kind!r}).")
    if "path" not in op:
        raise PatchError(index, f"A `{kind}` operation needs a `path`.")
    path = op["path"]
    if kind in ("add", "replace", "test") and "value" not in op:
        raise PatchError(index, f"A `{kind}` operation needs a `value`.")
    if kind in ("move", "copy") and "from" not in op:
        raise PatchError(index, f"A `{kind}` operation needs a `from` path.")

    if kind == "add":
        return _add(index, document, path, copy.deepcopy(op["value"]))
    if kind == "remove":
        return _remove(index, document, path)[0]
    if kind == "replace":
        tokens = _tokens(index, path)
        if not tokens:
            return copy.deepcopy(op["value"])
        parent = _walk(index, document, tokens[:-1], path)
        last = tokens[-1]
        if isinstance(parent, dict) and last not in parent:
            raise PatchError(
                index,
                f"{path}: there is nothing there to replace. Use the `add` operation to create "
                f"it. Its parent is {_describe(parent)}.",
            )
        _walk(index, document, tokens, path)
        if isinstance(parent, dict):
            parent[last] = copy.deepcopy(op["value"])
        else:
            position = _list_index(index, last, len(parent), path, allow_end=False)
            parent[position] = copy.deepcopy(op["value"])
        return document
    if kind == "test":
        actual = _get(index, document, path)
        if actual != op["value"]:
            raise PatchError(
                index, f"{path}: the `test` failed. The value there is {_describe(actual)}."
            )
        return document
    source = op["from"]
    if kind == "copy":
        return _add(index, document, path, copy.deepcopy(_get(index, document, source, "from")))
    # move
    if isinstance(path, str) and isinstance(source, str) and path.startswith(source + "/"):
        raise PatchError(index, f"Cannot move {source} into itself ({path}).")
    document, value = _remove(index, document, source)
    return _add(index, document, path, value)
