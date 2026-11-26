"""Relationships from v1 joins, and row-level security through them (ADR-031, ADR-032).

v2 turns each dataset's joins into relationships between tables (ADR-031). A
`many_to_one` or `one_to_one` join becomes an active relationship from the table
that declares it (the many side) to the table it joins to. A `one_to_many` join is
turned round and made inactive, as v1 never used it in a query. A join declared on
both sides becomes one relationship, active if either side was. The planned
migration in `tests/test_spec_drafts.py` writes the same rules out, and a test checks
that the two agree.

Rule R2 (ADR-032): a table keeps only rows related to visible rows of each table that
filters it for security. For each active relationship from T (many) to S (one) where
S's rows are restricted for this person, T keeps the rows whose key is among S's
visible rows, so a row with an empty or unmatched key is hidden. It is transitive: S's
visible rows are those its own relationships leave. S is read only to filter T: there
is no access check on S, and none of its rows are returned. A loop of active
relationships that a rule could travel round (a table with rules on the loop, or
reachable from it) is refused, since the rows it would leave depend on themselves.

Until V2's compiler applies R2 itself, `relationship_scan` gives the compiler a scan
(`compiler.Scan`) that does. The upgrade check (`services.results_digest`) runs every
dashboard with it, to show who will see fewer rows.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from sqlglot import exp

from opsis.errors import ApiError
from opsis.semantic.compiler import Scan, Source, UserContext, secured_scan
from opsis.semantic.model import Model


@dataclass(frozen=True)
class Relationship:
    """From the many side (`from_table`) to the one side (`to_table`)."""

    id: str
    from_table: str
    from_columns: tuple[str, ...]
    to_table: str
    to_columns: tuple[str, ...]
    cardinality: str  # many_to_one | one_to_one
    active: bool
    join_type: str  # left | inner

    def describe(self) -> str:
        return (
            f"{self.from_table} ({', '.join(self.from_columns)}) to "
            f"{self.to_table} ({', '.join(self.to_columns)})"
        )


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
            base = rid = f"{many[0]}__{one[0]}"
            number = 2
            while rid in found:
                rid, number = f"{base}_{number}", number + 1
            found[rid] = Relationship(
                rid, many[0], many[1], one[0], one[1], cardinality, active, join.type
            )
    return list(found.values())


def reachable(start: str, edges: Mapping[str, Sequence[str]]) -> set[str]:
    """Every table reachable from `start` along active relationships (not itself, unless
    a loop leads back)."""
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
    round, which v2 refuses: each as the tables it passes, first and last the same."""
    edges: dict[str, list[str]] = {}
    for rel in relationships:
        if rel.active:
            edges.setdefault(rel.from_table, []).append(rel.to_table)
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
            "would travel round, which v2 refuses. One of them must be made inactive.",
        )
        self.loop = loop


def relationship_scan(sources: Mapping[str, Source], relationships: Sequence[Relationship]) -> Scan:
    """A scan that applies each table's own rules (R1, as v1 does) and R2 through the
    active relationships. `sources` must hold every table a relationship may reach."""
    outgoing: dict[str, list[Relationship]] = {}
    for rel in relationships:
        if rel.active:
            outgoing.setdefault(rel.from_table, []).append(rel)
    edges = {name: [rel.to_table for rel in rels] for name, rels in outgoing.items()}
    models = {name: source.model for name, source in sources.items()}
    loops = refused_loops(relationships, models)
    blocked: dict[str, tuple[str, ...]] = {}
    for loop in loops:
        for table in sorted(sources):
            if table in loop or set(loop) & reachable(table, edges):
                blocked.setdefault(table, loop)

    def visible(
        source: Source, user: UserContext, path: tuple[str, ...]
    ) -> tuple[exp.Select, bool, bool]:
        """The table's rows this person may see, whether a rule was passed over, and
        whether anything restricts them."""
        name = source.model.dataset
        scan, bypassed = secured_scan(source, user)
        restricted = scan.args.get("where") is not None
        conditions: list[exp.Expr] = []
        for rel in outgoing.get(name, []):
            target = sources.get(rel.to_table)
            if target is None or rel.to_table in path or rel.to_table == name:
                # No data to filter by; or a loop no rule travels round (a refused one
                # stops the scan before it gets here).
                continue
            inner, inner_bypassed, inner_restricted = visible(target, user, (*path, name))
            bypassed = bypassed or inner_bypassed
            if not inner_restricted:
                continue
            alias = f"_opsis_r2_{len(path)}"
            for column in rel.from_columns:
                if column not in source.columns:
                    raise ApiError(
                        409,
                        "relationship_column",
                        f"The relationship {rel.describe()} uses {column!r}, which {name!r} "
                        "does not have.",
                    )
            for column in rel.to_columns:
                if column not in target.columns:
                    raise ApiError(
                        409,
                        "relationship_column",
                        f"The relationship {rel.describe()} uses {column!r}, which "
                        f"{rel.to_table!r} does not have.",
                    )
            match = exp.and_(
                *[
                    exp.EQ(
                        this=exp.column(remote, table=alias, quoted=True),
                        expression=exp.column(local, table=name, quoted=True),
                    )
                    for local, remote in zip(rel.from_columns, rel.to_columns, strict=True)
                ]
            )
            found = (
                exp.select(exp.Literal.number(1))
                .from_(inner.subquery(exp.to_identifier(alias, quoted=True)))
                .where(match)
            )
            conditions.append(exp.Exists(this=found))
        if conditions:
            scan = scan.where(exp.and_(*conditions))
            restricted = True
        return scan, bypassed, restricted

    def scan(source: Source, user: UserContext) -> tuple[exp.Select, bool]:
        loop = blocked.get(source.model.dataset)
        if loop is not None:
            raise LoopError(loop)
        select, bypassed, _ = visible(source, user, ())
        return select, bypassed

    return scan
