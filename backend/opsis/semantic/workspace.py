"""The workspace model document (ADR-031): one model for the workspace, with tables,
the relationships between them, and where the Model view draws them.

- **Tables** are named after the dataset they read and never change name; `label` is
  what people see. A dataset with data and no entry is a table too, generated from
  its columns (a starter table, as in v1) until someone edits it: the stored document
  holds only the tables someone wrote, and `effective` adds the generated ones.
- **Relationships** are keyed by a stable id (`<from>__<to>`, `_2` for another).
- **Who sees what:** a person reads and changes only the tables they may use and the
  relationships between them (`visible_part`, `merge_visible`).
- **Changes** are whole documents or JSON Patch. A stale change is applied anyway
  when none of the paths it touches changed since (`changed_paths`, `overlaps`).
- **v1 models** become a document by the migration's rules (`from_v1_models`), and
  one table can still be read and written as a v1 model (`table_view`,
  `apply_table_view`), so v1 clients keep working for a release.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Collection, Container, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opsis.semantic import expressions
from opsis.semantic import model as semantic
from opsis.semantic.model import Model, Problem, TableContext, schema_problems, schema_validator
from opsis.semantic.relationships import (
    Relationship,
    describe_path,
    grouping_steps,
    new_id,
    parse_relationship,
    refused_loops,
    relationship_entry,
    shortest_paths,
)
from opsis.semantic.security import Security, Source

MODEL_VERSION = 2
SCHEMA = "workspace-model.schema.json"
# What the history keeps; the layout is stored beside it.
DEFINITION_KEYS = ("modelVersion", "label", "description", "tables", "relationships")
NUMBER_TYPES = {"integer", "float", "decimal"}


@dataclass(frozen=True)
class TableData:
    """A dataset with data, as the workspace model sees it."""

    name: str
    label: str
    columns: Mapping[str, str]  # column name -> logical type
    primary_key: tuple[str, ...] = ()
    restricted: bool = False


@dataclass(frozen=True)
class Workspace:
    """The workspace model ready for queries: every table with data, its
    relationships, and how each person's rows of each table are read."""

    sources: Mapping[str, Source]
    relationships: tuple[Relationship, ...] = ()
    timezone: str | None = None  # the workspace timezone (WORKSPACE_TIMEZONE)
    security: Security = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        security = Security(self.sources, self.relationships, self.timezone)
        object.__setattr__(self, "security", security)

    @classmethod
    def from_v1(cls, sources: Mapping[str, Source]) -> Workspace:
        """The workspace v1 models make: their joins become relationships by the
        migration's rules."""
        from opsis.semantic.relationships import from_v1_joins

        models = {name: source.model for name, source in sources.items()}
        return cls(sources, tuple(from_v1_joins(models)))

    @property
    def models(self) -> dict[str, Model]:
        return {name: source.model for name, source in self.sources.items()}


def empty_document() -> dict[str, Any]:
    return {"modelVersion": MODEL_VERSION, "tables": {}, "relationships": {}}


def definition_of(document: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a document that the history keeps (no layout)."""
    result = {key: copy.deepcopy(document[key]) for key in DEFINITION_KEYS if key in document}
    result.setdefault("modelVersion", MODEL_VERSION)
    result.setdefault("tables", {})
    result.setdefault("relationships", {})
    return result


def starter_table(data: TableData) -> dict[str, Any]:
    """The table generated for a dataset nobody has written a table for."""
    return {
        "source": {"dataset": data.name},
        **semantic.starter_definition(data.label, data.columns, data.primary_key),
    }


def effective(
    document: Mapping[str, Any], datasets: Mapping[str, TableData]
) -> tuple[dict[str, Any], set[str]]:
    """The document with a generated table for each dataset with data and no entry,
    and the names of those generated tables. Written tables keep their order; the
    generated ones follow, by name."""
    result = definition_of(document)
    generated = set()
    for name in sorted(datasets):
        if name not in result["tables"]:
            result["tables"][name] = starter_table(datasets[name])
            generated.add(name)
    return result, generated


def normalise(
    document: Mapping[str, Any],
    previous: Mapping[str, Any],
    datasets: Mapping[str, TableData],
) -> dict[str, Any]:
    """What to store of an effective document: a generated table that nobody changed
    stays generated, and a written table stays written."""
    result = definition_of(document)
    kept = previous.get("tables") or {}
    tables = {}
    for name, entry in result["tables"].items():
        if name not in kept and name in datasets and entry == starter_table(datasets[name]):
            continue
        tables[name] = entry
    result["tables"] = tables
    return result


# ---- reading it -------------------------------------------------------------------


def relationships_of(document: Mapping[str, Any]) -> tuple[Relationship, ...]:
    return tuple(
        parse_relationship(rid, entry)
        for rid, entry in (document.get("relationships") or {}).items()
    )


def tables_of(document: Mapping[str, Any]) -> dict[str, Model]:
    return {
        name: semantic.parse_model(name, entry)
        for name, entry in (document.get("tables") or {}).items()
    }


# ---- who sees what ----------------------------------------------------------------


def visible_part(document: Mapping[str, Any], allowed: Container[str]) -> dict[str, Any]:
    """The tables a person may use, and the relationships between them."""
    result = definition_of(document)
    result["tables"] = {n: t for n, t in result["tables"].items() if n in allowed}
    result["relationships"] = {
        rid: rel
        for rid, rel in result["relationships"].items()
        if rel["from"]["table"] in allowed and rel["to"]["table"] in allowed
    }
    return result


def merge_visible(
    full: Mapping[str, Any], submitted: Mapping[str, Any], allowed: Container[str]
) -> dict[str, Any]:
    """A person's change to the part they may see, with the rest put back as it was."""
    result = definition_of(submitted)
    current = definition_of(full)
    result["tables"] = {
        **{n: t for n, t in result["tables"].items() if n in allowed},
        **{n: t for n, t in current["tables"].items() if n not in allowed},
    }
    hidden = {
        rid: rel
        for rid, rel in current["relationships"].items()
        if rel["from"]["table"] not in allowed or rel["to"]["table"] not in allowed
    }
    result["relationships"] = {**result["relationships"], **hidden}
    return result


# ---- changes ----------------------------------------------------------------------


def _escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def diff_ops(old: Any, new: Any, path: str = "") -> list[dict[str, Any]]:
    """JSON Patch operations that turn `old` into `new`, objects compared key by key
    (lists are replaced whole)."""
    if isinstance(old, dict) and isinstance(new, dict):
        ops: list[dict[str, Any]] = []
        for key in old:
            if key not in new:
                ops.append({"op": "remove", "path": f"{path}/{_escape(key)}"})
        for key, value in new.items():
            child = f"{path}/{_escape(key)}"
            if key not in old:
                ops.append({"op": "add", "path": child, "value": copy.deepcopy(value)})
            elif old[key] != value or json.dumps(old[key]) != json.dumps(value):
                ops += diff_ops(old[key], value, child)
        return ops
    if old != new or json.dumps(old) != json.dumps(new):
        return [{"op": "replace", "path": path, "value": copy.deepcopy(new)}]
    return []


def changed_paths(old: Any, new: Any) -> set[str]:
    return {op["path"] for op in diff_ops(old, new)}


def touched_paths(ops: Iterable[Mapping[str, Any]]) -> set[str]:
    """The paths a JSON Patch reads or writes."""
    found = set()
    for op in ops:
        for key in ("path", "from"):
            if isinstance(op.get(key), str):
                found.add(_list_free(op[key]))
    return found


def _list_free(path: str) -> str:
    """A path cut at its first list index: a change to one item of a list counts as a
    change to the list (`rls`, the columns of a relationship)."""
    parts = path.split("/")
    for index, part in enumerate(parts[1:], start=1):
        if part.isdigit() or part == "-":
            return "/".join(parts[:index])
    return path


def overlaps(first: Collection[str], second: Collection[str]) -> bool:
    """True when a path in one set is, contains or is inside a path in the other."""
    for a in first:
        for b in second:
            if a == b or a.startswith(b + "/") or b.startswith(a + "/") or not a or not b:
                return True
    return False


# ---- checking it ------------------------------------------------------------------


def _table_problems_context(
    name: str,
    models: Mapping[str, Model],
    relationships: tuple[Relationship, ...],
    datasets: Mapping[str, TableData],
    hidden: Container[str] = (),
) -> TableContext:
    """What one table's checks may know of the others. A table the person can see
    knows nothing of the hidden ones: a formula or filter naming one reads as naming
    a table that does not exist, so a check never confirms what is hidden."""

    def known(table: str) -> bool:
        return name in hidden or table not in hidden

    restricted = {n for n, d in datasets.items() if d.restricted}
    reach = shortest_paths(name, grouping_steps(relationships), blocked=restricted)
    related = {t: datasets[t].columns for t in reach.paths if t in datasets and known(t)}
    return TableContext(
        related=related,
        other_measures={t: set(m.measures) for t, m in models.items() if t != name and known(t)},
        relationships={
            rel.id: (rel.from_table, rel.to_table)
            for rel in relationships
            if known(rel.from_table) and known(rel.to_table)
        },
        v2=True,
    )


def validate(
    spec_dir: Path,
    document: Any,
    datasets: Mapping[str, TableData],
    hidden: Container[str] = (),
) -> list[Problem]:
    """Every problem in a workspace model document (generated tables are added first).
    An empty list means it can be saved. `hidden` is the tables the person checking
    may not use (see `_table_problems_context`)."""
    if not isinstance(document, dict):
        return [{"path": "/", "message": "The workspace model must be a mapping."}]
    problems = schema_problems(schema_validator(spec_dir, SCHEMA), document)
    if problems:
        return problems  # the checks below assume the structure is right
    full, _ = effective(document, datasets)

    def add(path: str, message: str) -> None:
        problems.append({"path": path, "message": message})

    tables: dict[str, Model] = {}
    for name, entry in full["tables"].items():
        base = f"/tables/{name}"
        source = entry["source"]
        if "transform" in source:
            add(f"{base}/source", "Tables built from transforms come in a later version.")
            continue
        if source["dataset"] != name:
            add(
                f"{base}/source/dataset",
                f"A table is named after its dataset: this one must read {name!r}.",
            )
            continue
        if entry.get("table") not in (None, name):
            add(f"{base}/table", f"Inside `tables` this must be {name!r} or left out.")
        if name not in datasets:
            add(
                base,
                f"There is no dataset called {name!r} with data. Create it and refresh it "
                "before writing its table.",
            )
            continue
        tables[name] = semantic.parse_model(name, entry)

    relationships = relationships_of(full)
    seen: dict[tuple[Any, ...], str] = {}
    usable: list[Relationship] = []
    for rel in relationships:
        base = f"/relationships/{rel.id}"
        broken = False
        for end, table, columns in (
            ("from", rel.from_table, rel.from_columns),
            ("to", rel.to_table, rel.to_columns),
        ):
            if table not in tables:
                add(f"{base}/{end}/table", f"There is no table called {table!r}.")
                broken = True
                continue
            for index, column in enumerate(columns):
                if column not in datasets[table].columns:
                    add(
                        f"{base}/{end}/columns/{index}",
                        f"Table {table!r} has no column called {column!r}.",
                    )
                    broken = True
        if rel.from_table == rel.to_table:
            add(f"{base}/to/table", "A table cannot relate to itself.")
            broken = True
        if len(rel.from_columns) != len(rel.to_columns):
            add(
                f"{base}/to/columns",
                f"The two ends need as many columns each ({len(rel.from_columns)} and "
                f"{len(rel.to_columns)}).",
            )
            broken = True
        key = (rel.from_table, rel.from_columns, rel.to_table, rel.to_columns)
        if key in seen:
            add(base, f"This is the same relationship as {seen[key]!r}.")
            broken = True
        seen.setdefault(key, rel.id)
        if not broken:
            usable.append(rel)

    for loop in refused_loops(usable, tables):
        first = next(
            r for r in usable if r.active and (r.from_table, r.to_table) == (loop[0], loop[1])
        )
        add(
            f"/relationships/{first.id}/active",
            f"The active relationships {' → '.join(loop)} form a loop that row-level security "
            "would travel round, which is refused. Make one of them inactive.",
        )

    usable_tuple = tuple(usable)
    for name in tables:
        context = _table_problems_context(name, tables, usable_tuple, datasets, hidden)
        problems += semantic.table_problems(
            name,
            full["tables"][name],
            datasets[name].columns,
            prefix=f"/tables/{name}",
            context=context,
        )
    problems += _formula_loops(tables)
    return problems


def _formula_loops(tables: Mapping[str, Model]) -> list[Problem]:
    """Formulas that use themselves through another table's measures (a loop inside
    one table is reported by that table's own check)."""
    graph: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for table, model in tables.items():
        for name, measure in model.measures.items():
            if measure.expr is None:
                continue
            refs = []
            for ref in expressions.measure_references(measure.expr):
                owner, dot, other = ref.partition(".")
                refs.append((owner, other) if dot else (table, ref))
            graph[(table, name)] = refs
    problems: list[Problem] = []
    for start in graph:
        seen: set[tuple[str, str]] = set()
        todo = [ref for ref in graph[start] if ref[0] != start[0]]
        crosses = bool(todo)
        while todo:
            node = todo.pop()
            if node == start:
                if crosses:
                    problems.append(
                        {
                            "path": f"/tables/{start[0]}/measures/{start[1]}/expr",
                            "message": "This measure refers to itself through another table's "
                            "measures.",
                        }
                    )
                break
            if node in seen:
                continue
            seen.add(node)
            todo.extend(graph.get(node, ()))
    return problems


def structure_warnings(
    document: Mapping[str, Any], datasets: Mapping[str, TableData]
) -> list[dict[str, Any]]:
    """What a valid model saves with but may surprise, found without reading data:
    two paths of the same length, key columns of different types, and relationships
    into restricted datasets. (Repeated keys need the data: services.model_warnings.)"""
    full, _ = effective(document, datasets)
    relationships = relationships_of(full)
    found: list[dict[str, Any]] = []
    restricted = {n for n, d in datasets.items() if d.restricted}
    steps = grouping_steps(relationships)
    reported: set[tuple[str, str]] = set()
    for table in sorted(full["tables"]):
        reach = shortest_paths(table, steps, blocked=restricted)
        for target, (one, other) in sorted(reach.ambiguous.items()):
            if (table, target) in reported:
                continue
            reported.add((table, target))
            found.append(
                {
                    "code": "ambiguous_path",
                    "path": f"/relationships/{other[-1].rel.id}",
                    "table": table,
                    "target": target,
                    "message": f"Queries from {table!r} cannot group by {target!r}: two paths "
                    f"are as short as each other ({describe_path(one)}, and "
                    f"{describe_path(other)}). Make one of the relationships inactive, or name "
                    "one in a measure's `relationship`.",
                }
            )
    for rel in relationships:
        if rel.from_table not in datasets or rel.to_table not in datasets:
            continue
        for local, remote in zip(rel.from_columns, rel.to_columns, strict=False):
            a = datasets[rel.from_table].columns.get(local)
            b = datasets[rel.to_table].columns.get(remote)
            if a is None or b is None or types_match(a, b):
                continue
            found.append(
                {
                    "code": "key_types_differ",
                    "path": f"/relationships/{rel.id}",
                    "message": f"The relationship {rel.describe()} matches {local!r} ({a}) "
                    f"with {remote!r} ({b}). Values are compared after converting one of "
                    "them, which can fail or match less than you expect.",
                }
            )
        for end in (rel.to_table, rel.from_table):
            if end in restricted:
                found.append(
                    {
                        "code": "relationship_to_restricted",
                        "path": f"/relationships/{rel.id}",
                        "message": f"{end!r} is a restricted dataset. People who may use the "
                        f"other end but not {end!r} cannot use its fields through "
                        f"{rel.describe()}, though its row-level security still filters "
                        "what they see.",
                    }
                )
                break
    return found


def types_match(a: str, b: str) -> bool:
    if a == b or (a in NUMBER_TYPES and b in NUMBER_TYPES):
        return True
    return {a, b} == {"date", "timestamp"}  # a timestamp relates to a date table on its day


# ---- v1 models --------------------------------------------------------------------


def _v1_relationship(table: str, join: Mapping[str, Any]) -> dict[str, Any]:
    local, remote = list(join["on"]), list(join["on"].values())
    if join.get("relationship", "many_to_one") == "one_to_many":
        many, one, active, cardinality = (join["to"], remote), (table, local), False, "many_to_one"
    else:
        many, one, active = (table, local), (join["to"], remote), True
        cardinality = join.get("relationship", "many_to_one")
    return {
        "from": {"table": many[0], "columns": many[1]},
        "to": {"table": one[0], "columns": one[1]},
        "cardinality": cardinality,
        "crossFilter": "single",
        "securityFilter": "single",
        "active": active,
        "joinType": join.get("type", "left"),
    }


def _add_v1_joins(
    relationships: dict[str, dict[str, Any]], table: str, joins: Iterable[Mapping[str, Any]]
) -> list[str]:
    """Add each join as a relationship unless the same one is there (declared on both
    sides: one relationship, active if either was). The ids each join ended up as."""
    ids = []
    for join in joins:
        entry = _v1_relationship(table, join)
        same = next(
            (
                rid
                for rid, other in relationships.items()
                if (other["from"], other["to"]) == (entry["from"], entry["to"])
            ),
            None,
        )
        if same is not None:
            relationships[same]["active"] = (
                bool(relationships[same].get("active", True)) or entry["active"]
            )
            ids.append(same)
            continue
        rid = new_id(relationships, entry["from"]["table"], entry["to"]["table"])
        relationships[rid] = entry
        ids.append(rid)
    return ids


def v1_table_entry(table: str, definition: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": {"dataset": table},
        **{k: copy.deepcopy(v) for k, v in definition.items() if k not in ("joins", "dataset")},
    }


def from_v1_models(
    definitions: Mapping[str, Mapping[str, Any]], edited: Collection[str] | None = None
) -> dict[str, Any]:
    """The workspace model the migration makes from v1 models (ADR-031): each edited
    model becomes a table (all of them when `edited` is None), and every model's joins
    become relationships."""
    document = empty_document()
    for name, definition in definitions.items():
        if edited is None or name in edited:
            document["tables"][name] = v1_table_entry(name, definition)
    for name, definition in definitions.items():
        _add_v1_joins(document["relationships"], name, definition.get("joins") or [])
    return document


def _view_ids(document: Mapping[str, Any], table: str) -> list[str]:
    """The relationships one table's v1 view shows as its joins: its active ones, and
    the inactive ones into it (v1's `one_to_many` joins, turned round)."""
    return [
        rid
        for rid, rel in (document.get("relationships") or {}).items()
        if (rel["from"]["table"] == table and rel.get("active", True))
        or (rel["to"]["table"] == table and not rel.get("active", True))
    ]


def view_positions(document: Mapping[str, Any], table: str) -> dict[str, str]:
    """For each relationship one table's v1 view shows as a join, the join's index."""
    return {rid: str(index) for index, rid in enumerate(_view_ids(document, table))}


def table_view(document: Mapping[str, Any], table: str) -> dict[str, Any]:
    """One table as a v1 model: the table without its `source`, with `joins` for the
    relationships of `_view_ids`."""
    entry = (document.get("tables") or {}).get(table) or {}
    view = {k: copy.deepcopy(v) for k, v in entry.items() if k not in ("source", "table")}
    joins = []
    for rid in _view_ids(document, table):
        rel = document["relationships"][rid]
        if rel["from"]["table"] == table:
            joins.append(
                {
                    "to": rel["to"]["table"],
                    "on": dict(zip(rel["from"]["columns"], rel["to"]["columns"], strict=False)),
                    "type": rel.get("joinType", "left"),
                    "relationship": rel.get("cardinality", "many_to_one"),
                }
            )
        else:
            joins.append(
                {
                    "to": rel["from"]["table"],
                    "on": dict(zip(rel["to"]["columns"], rel["from"]["columns"], strict=False)),
                    "type": rel.get("joinType", "left"),
                    "relationship": "one_to_many",
                }
            )
    if joins:
        view["joins"] = joins
    return view


def apply_table_view(
    document: Mapping[str, Any], table: str, view: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """The document with one table replaced from a v1 model, and its joins as the
    table's relationships: a join that matches one keeps its id and other settings,
    a new one is added, and a relationship no join matches any more is removed. Also
    returns, for each join's position, the id of its relationship."""
    result = definition_of(document)
    previous = (result["tables"].get(table) or {}).get("source") or {"dataset": table}
    result["tables"][table] = {
        "source": previous,
        **{
            k: copy.deepcopy(v)
            for k, v in view.items()
            if k not in ("dataset", "table", "joins", "source")
        },
    }
    relationships = result["relationships"]
    old = _view_ids(result, table)
    kept = {rid: relationships.pop(rid) for rid in old}
    positions: dict[str, str] = {}
    for index, join in enumerate(view.get("joins") or []):
        entry = _v1_relationship(table, join)
        match = next(
            (
                rid
                for rid, rel in kept.items()
                if (rel["from"], rel["to"]) == (entry["from"], entry["to"])
                and bool(rel.get("active", True)) == entry["active"]
            ),
            None,
        )
        if match is not None:
            rel = kept.pop(match)
            rel["joinType"] = entry["joinType"]
            if entry["active"]:
                rel["cardinality"] = entry["cardinality"]
            relationships[match] = rel
            positions[str(index)] = match
            continue
        ids = _add_v1_joins(relationships, table, [join])
        positions[str(index)] = ids[0]
    result["relationships"] = relationships
    return result, positions


def table_entry_equal(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def relationship_document(rel: Relationship) -> dict[str, Any]:
    return relationship_entry(rel)
