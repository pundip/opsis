"""Relationships between tables, and the paths queries and security take along them
(ADR-031, ADR-032).

A relationship goes from the many side (`from_table`) to the one side (`to_table`), as
in Power BI. Queries use the active ones:

- **Grouping:** a query may group by a field of any table its base table reaches by
  following active relationships from the many side to the one side, any number of
  steps. The shortest path wins; two shortest paths are ambiguous, and the query is
  refused with both named. A path never goes through a restricted table (it may end
  at one).
- **Filtering:** a filter on a table that is not on such a path still applies when a
  filter path leads from it to the base table: each step goes from the one side to
  the many side, or the other way along a relationship with `crossFilter: both`.
- **Security (R2):** see `opsis.semantic.security`.

v1's joins become relationships by the migration's rules (`from_v1_joins`): a
`many_to_one` or `one_to_one` join is an active relationship from the table that
declares it, a `one_to_many` join is turned round and made inactive (v1 never used it
in a query), and a join declared on both sides is one relationship, active if either
was. The migration (0014) and `tests/test_spec_drafts.py` write the same rules out.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from opsis.errors import ApiError
from opsis.semantic.model import Model

CARDINALITIES = ("many_to_one", "one_to_one")


@dataclass(frozen=True)
class Relationship:
    """From the many side (`from_table`) to the one side (`to_table`)."""

    id: str
    from_table: str
    from_columns: tuple[str, ...]
    to_table: str
    to_columns: tuple[str, ...]
    cardinality: str = "many_to_one"  # many_to_one | one_to_one
    active: bool = True
    join_type: str = "left"  # left | inner
    cross_filter: str = "single"  # single | both
    security_filter: str = "single"  # single | both

    def describe(self) -> str:
        return (
            f"{self.from_table} ({', '.join(self.from_columns)}) to "
            f"{self.to_table} ({', '.join(self.to_columns)})"
        )

    def ends(self) -> frozenset[str]:
        return frozenset((self.from_table, self.to_table))


def parse_relationship(rid: str, entry: Mapping[str, Any]) -> Relationship:
    """A relationship from its document form (workspace-model.schema.json)."""
    return Relationship(
        id=rid,
        from_table=str(entry["from"]["table"]),
        from_columns=tuple(entry["from"]["columns"]),
        to_table=str(entry["to"]["table"]),
        to_columns=tuple(entry["to"]["columns"]),
        cardinality=str(entry.get("cardinality", "many_to_one")),
        active=bool(entry.get("active", True)),
        join_type=str(entry.get("joinType", "left")),
        cross_filter=str(entry.get("crossFilter", "single")),
        security_filter=str(entry.get("securityFilter", "single")),
    )


def relationship_entry(rel: Relationship) -> dict[str, Any]:
    """The document form of a relationship, every property written out."""
    return {
        "from": {"table": rel.from_table, "columns": list(rel.from_columns)},
        "to": {"table": rel.to_table, "columns": list(rel.to_columns)},
        "cardinality": rel.cardinality,
        "crossFilter": rel.cross_filter,
        "securityFilter": rel.security_filter,
        "active": rel.active,
        "joinType": rel.join_type,
    }


def new_id(taken: Collection[str], from_table: str, to_table: str) -> str:
    """`<from>__<to>`, then `_2`, `_3` for another between the same tables."""
    base = rid = f"{from_table}__{to_table}"
    number = 2
    while rid in taken:
        rid, number = f"{base}_{number}", number + 1
    return rid


def from_v1_joins(models: Mapping[str, Model]) -> list[Relationship]:
    """The relationships the migration makes from the models' joins (ADR-031)."""
    found: dict[str, Relationship] = {}
    for name, model in models.items():
        for join in model.joins:
            local = tuple(pair[0] for pair in join.on)
            remote = tuple(pair[1] for pair in join.on)
            if join.relationship == "one_to_many":
                many, one, active, cardinality = (
                    (join.to, remote),
                    (name, local),
                    False,
                    "many_to_one",
                )
            else:
                many, one, active, cardinality = (
                    (name, local),
                    (join.to, remote),
                    True,
                    join.relationship,
                )
            same = next(
                (
                    key
                    for key, other in found.items()
                    if (other.from_table, other.from_columns, other.to_table, other.to_columns)
                    == (many[0], many[1], one[0], one[1])
                ),
                None,
            )
            if same is not None:  # declared on both sides: one relationship, the active one
                kept = found[same]
                found[same] = replace(kept, active=kept.active or active)
                continue
            rid = new_id(found, many[0], one[0])
            found[rid] = Relationship(
                rid, many[0], many[1], one[0], one[1], cardinality, active, join.type
            )
    return list(found.values())


# ---- the graph ----------------------------------------------------------------------


def active_edges(relationships: Iterable[Relationship]) -> dict[str, list[str]]:
    """For each table, the tables its active relationships lead to (many to one)."""
    edges: dict[str, list[str]] = {}
    for rel in relationships:
        if rel.active:
            edges.setdefault(rel.from_table, []).append(rel.to_table)
    return edges


def reachable(start: str, edges: Mapping[str, Sequence[str]]) -> set[str]:
    """Every table reachable from `start` along the edges (not itself, unless a loop
    leads back)."""
    seen: set[str] = set()
    todo = list(edges.get(start, ()))
    while todo:
        table = todo.pop()
        if table not in seen:
            seen.add(table)
            todo.extend(edges.get(table, ()))
    return seen


def _cycle(start: str, edges: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    """A shortest loop from `start` back to itself, as the tables it passes."""
    paths: list[tuple[str, ...]] = [(start,)]
    seen = {start}
    while paths:
        path = paths.pop(0)
        for table in sorted(edges.get(path[-1], ())):
            if table == start:
                return (*path, start)
            if table not in seen:
                seen.add(table)
                paths.append((*path, table))
    return ()


def refused_loops(
    relationships: Iterable[Relationship], models: Mapping[str, Model]
) -> list[tuple[str, ...]]:
    """The loops of active relationships that a row-level security rule could travel
    round, which v2 refuses (ADR-032, made precise in ADR-038): a table with rules on
    the loop, or reachable from it. Each as the tables it passes, first and last the
    same."""
    edges = active_edges(relationships)
    loops: list[tuple[str, ...]] = []
    covered: set[str] = set()
    for table in sorted(edges):
        reach = reachable(table, edges)
        if table not in reach or table in covered:
            continue
        group = {other for other in reach if table in reachable(other, edges)} | {table}
        covered |= group
        ruled = [name for name in sorted(group | reach) if name in models and models[name].rls]
        if ruled:
            loops.append(_cycle(table, edges))
    return loops


class LoopError(ApiError):
    def __init__(self, loop: tuple[str, ...]) -> None:
        super().__init__(
            409,
            "relationship_loop",
            f"The active relationships {' → '.join(loop)} form a loop that row-level security "
            "would travel round, which is refused. Make one of them inactive.",
        )
        self.loop = loop


@dataclass(frozen=True)
class Step:
    """One relationship on a path, and which way it is walked."""

    rel: Relationship
    forward: bool  # from the many side to the one side

    @property
    def start(self) -> str:
        return self.rel.from_table if self.forward else self.rel.to_table

    @property
    def end(self) -> str:
        return self.rel.to_table if self.forward else self.rel.from_table

    @property
    def start_columns(self) -> tuple[str, ...]:
        return self.rel.from_columns if self.forward else self.rel.to_columns

    @property
    def end_columns(self) -> tuple[str, ...]:
        return self.rel.to_columns if self.forward else self.rel.from_columns


Path = tuple[Step, ...]


@dataclass(frozen=True)
class Reach:
    """The shortest paths from one table: to each table reached, its path, or the two
    shortest paths when there is more than one."""

    paths: Mapping[str, Path]
    ambiguous: Mapping[str, tuple[Path, Path]]


def describe_path(path: Path) -> str:
    if not path:
        return "(the table itself)"
    tables = [path[0].start, *(step.end for step in path)]
    return " → ".join(tables)


def shortest_paths(
    start: str,
    steps: Mapping[str, Sequence[Step]],
    *,
    through: Collection[str] | None = None,
    blocked: Collection[str] = (),
) -> Reach:
    """Breadth first from `start` over `steps` (the steps leaving each table). A table
    in `blocked` may end a path but is never walked through; `through`, when given,
    is the only tables a path may pass (and end at)."""
    paths: dict[str, Path] = {start: ()}
    count: dict[str, int] = {start: 1}
    second: dict[str, Path] = {}
    frontier = [start]
    while frontier:
        level: dict[str, list[Path]] = {}
        for table in frontier:
            if table in blocked and table != start:
                continue
            for step in steps.get(table, ()):
                end = step.end
                if end in paths and end not in level:
                    continue  # reached at a shorter distance already
                if through is not None and end not in through:
                    continue
                level.setdefault(end, []).append((*paths[table], step))
        frontier = []
        for table, found in sorted(level.items()):
            total = sum(count.get(path[-1].start, 1) for path in found)
            paths[table] = found[0]
            count[table] = total
            if total > 1:
                if len(found) > 1:
                    second[table] = found[1]
                else:  # one way in, from a table reached two ways
                    previous = found[0][-1].start
                    other = second.get(previous)
                    if other is not None:
                        second[table] = (*other, found[0][-1])
            frontier.append(table)
    del paths[start]
    ambiguous = {t: (paths[t], second[t]) for t in second if t in paths}
    return Reach(
        {t: p for t, p in paths.items() if t not in ambiguous},
        ambiguous,
    )


def grouping_steps(relationships: Iterable[Relationship]) -> dict[str, list[Step]]:
    """The steps a query may take to group by another table's fields: along active
    relationships from the many side to the one side."""
    steps: dict[str, list[Step]] = {}
    for rel in relationships:
        if rel.active and rel.from_table != rel.to_table:
            steps.setdefault(rel.from_table, []).append(Step(rel, True))
    return steps


def filter_steps(relationships: Iterable[Relationship]) -> dict[str, list[Step]]:
    """The steps a filter takes: from the one side to the many side of each active
    relationship, and from the many side to the one side where `crossFilter: both`."""
    steps: dict[str, list[Step]] = {}
    for rel in relationships:
        if not rel.active or rel.from_table == rel.to_table:
            continue
        steps.setdefault(rel.to_table, []).append(Step(rel, False))
        if rel.cross_filter == "both":
            steps.setdefault(rel.from_table, []).append(Step(rel, True))
    return steps


def with_override(
    relationships: Sequence[Relationship], override: Relationship | None
) -> tuple[Relationship, ...]:
    """The relationships a measure that names `override` uses (Power BI's
    USERELATIONSHIP): it is active, and the others between the same two tables are
    not."""
    if override is None:
        return tuple(relationships)
    kept = [rel for rel in relationships if rel.id != override.id and rel.ends() != override.ends()]
    return (*kept, replace(override, active=True))


def within_two_steps(
    relationships: Sequence[Relationship], base: str, restricted: Collection[str] = ()
) -> dict[str, str | None]:
    """v1's reach, which dashboards keep for their filters until V4 (ADR-031): the
    tables `base` reaches in at most two many-to-one steps, each with the table it is
    reached through (None when it is one step away). A table two steps away by two
    ways is left out, and no path goes through a restricted table."""
    steps = grouping_steps(relationships)
    direct: dict[str, str | None] = {s.end: None for s in steps.get(base, ()) if s.end != base}
    farther: dict[str, list[str]] = {}
    for middle in direct:
        if middle in restricted:
            continue
        for step in steps.get(middle, ()):
            if step.end != base and step.end not in direct:
                farther.setdefault(step.end, []).append(middle)
    return {**direct, **{name: via[0] for name, via in farther.items() if len(set(via)) == 1}}
