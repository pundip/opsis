"""Safe handling of the SQL expressions people write in a semantic model.

Dimension expressions, segment filters, RLS filters and measure formulas are
author-written SQL, which makes them the main injection surface (risk R4).
Nothing here is ever pasted into a query as text. Each expression is parsed
with sqlglot and every node is checked against an allow-list: plain columns,
literals, operators, CASE, CAST and a fixed set of scalar functions. Anything
else (subqueries, table functions, aggregates, window functions, parameters,
unknown functions) is rejected with a message that names the problem.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

MAX_LENGTH = 2000
MAX_NODES = 400

ALLOWED_NODES: tuple[type[exp.Expr], ...] = (
    exp.Column, exp.Identifier, exp.Literal, exp.Null, exp.Boolean, exp.Paren, exp.Tuple,
    exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg, exp.DPipe,
    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
    exp.And, exp.Or, exp.Not, exp.Is, exp.In, exp.Between, exp.Like, exp.ILike,
    exp.Case, exp.If, exp.Cast, exp.TryCast, exp.DataType, exp.Interval, exp.Var,
)  # fmt: skip

# Scalar functions, by the name sqlglot reports for them.
ALLOWED_FUNCTIONS = {
    "LOWER", "UPPER", "TRIM", "LENGTH", "SUBSTRING", "LEFT", "RIGHT", "REPLACE", "CONCAT",
    "CONTAINS", "STARTS_WITH", "ENDS_WITH",
    "ABS", "ROUND", "FLOOR", "CEIL", "SIGN", "SQRT", "POWER", "LN", "LOG", "GREATEST", "LEAST",
    "COALESCE", "NULLIF",
    "YEAR", "QUARTER", "MONTH", "DAY", "DAY_OF_WEEK", "TIMESTAMP_TRUNC", "DATE_TRUNC",
    "DATEDIFF", "TIME_TO_STR",
}  # fmt: skip

ALLOWED_CAST_TYPES = {
    exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR, exp.DataType.Type.CHAR,
    exp.DataType.Type.TINYINT, exp.DataType.Type.SMALLINT, exp.DataType.Type.INT,
    exp.DataType.Type.BIGINT, exp.DataType.Type.FLOAT, exp.DataType.Type.DOUBLE,
    exp.DataType.Type.DECIMAL, exp.DataType.Type.BOOLEAN, exp.DataType.Type.DATE,
    exp.DataType.Type.TIMESTAMP, exp.DataType.Type.DATETIME,
}  # fmt: skip

MEASURE_REF = re.compile(r"\{([a-z_][a-z0-9_]{0,62})\}")
USER_REF = re.compile(r"\{\{\s*user\.(attributes\.[a-z_][a-z0-9_]{0,62}|email|role)\s*\}\}")
_MEASURE_PREFIX = "__opsis_m_"
_USER_PREFIX = "__opsis_u_"


class ExpressionError(Exception):
    """The expression is not allowed. The message is written for the model's author."""


def _nodes(tree: exp.Expr) -> list[exp.Expr]:
    return [item[0] if isinstance(item, tuple) else item for item in tree.walk()]


def _function_name(node: exp.Expr) -> str | None:
    if isinstance(node, exp.Anonymous):
        return str(node.name).upper()
    if isinstance(node, exp.Func):
        return str(node.sql_name()).upper()
    return None


def parse(text: str) -> exp.Expr:
    """Parse one expression and check every node against the allow-list."""
    if len(text) > MAX_LENGTH:
        raise ExpressionError(f"The expression is longer than {MAX_LENGTH} characters.")
    try:
        statements = sqlglot.parse(text, read="duckdb")
    except SqlglotError as exc:
        first_line = str(exc).strip().splitlines()[0][:200]
        raise ExpressionError(f"This is not a valid SQL expression: {first_line}") from None
    if len(statements) != 1 or statements[0] is None:
        raise ExpressionError("Write a single expression, with no semicolons.")
    tree = statements[0]
    nodes = _nodes(tree)
    if len(nodes) > MAX_NODES:
        raise ExpressionError("The expression is too complex.")
    for node in nodes:
        if isinstance(node, exp.Select | exp.Subquery | exp.Union | exp.Table):
            raise ExpressionError("Subqueries and table references are not allowed.")
        if isinstance(node, exp.Window):
            raise ExpressionError("Window functions are not allowed.")
        if isinstance(node, exp.AggFunc):
            raise ExpressionError(
                f"Aggregate functions such as {_function_name(node)} are not allowed here. "
                "Define a measure instead."
            )
        if isinstance(node, exp.Placeholder | exp.Parameter):
            raise ExpressionError("Query parameters are not allowed.")
        if isinstance(node, ALLOWED_NODES):
            if isinstance(node, exp.DataType) and node.this not in ALLOWED_CAST_TYPES:
                raise ExpressionError(f"Casting to {node.sql()} is not allowed.")
            if isinstance(node, exp.In) and (node.args.get("query") or node.args.get("unnest")):
                raise ExpressionError("IN must be followed by a list of values.")
            continue
        name = _function_name(node)
        if name is not None:
            if name not in ALLOWED_FUNCTIONS:
                raise ExpressionError(f"The function {name} is not allowed.")
            continue
        raise ExpressionError(f"This SQL is not allowed: {node.sql(dialect='duckdb')[:80]}")
    return tree


def _resolve_columns(
    tree: exp.Expr, alias: str, columns: Sequence[str], skip_prefix: str | None = None
) -> exp.Expr:
    """Point every column at `alias`, using the dataset's exact column names."""
    exact = {c.lower(): c for c in columns}
    for node in list(tree.find_all(exp.Column)):
        if skip_prefix and node.name.startswith(skip_prefix):
            continue
        if node.table:
            raise ExpressionError(
                f"Write column names without a table prefix (found {node.sql(dialect='duckdb')})."
            )
        actual = exact.get(node.name.lower())
        if actual is None:
            raise ExpressionError(f"There is no column called {node.name!r} in this dataset.")
        node.replace(exp.column(actual, table=alias, quoted=True))
    return tree


def column_expression(text: str, alias: str, columns: Sequence[str]) -> exp.Expr:
    """A dimension expression or segment filter, with columns bound to `alias`."""
    return _resolve_columns(parse(text), alias, columns)


def measure_references(text: str) -> list[str]:
    return MEASURE_REF.findall(text)


def measure_expression(text: str, resolve: Callable[[str], exp.Expr]) -> exp.Expr:
    """A measure formula. `{name}` is replaced by resolve(name), the compiled measure.

    Raw columns are not allowed: a formula may only combine other measures.
    """
    prepared = MEASURE_REF.sub(lambda m: f"{_MEASURE_PREFIX}{m.group(1)}", text)
    if "{" in prepared or "}" in prepared:
        raise ExpressionError("Reference measures as {measure_name}.")
    tree = parse(prepared)
    for node in list(tree.find_all(exp.Column)):
        if node.table or not node.name.startswith(_MEASURE_PREFIX):
            raise ExpressionError(
                f"A measure formula can only use other measures in braces, not the column "
                f"{node.name!r}. Define a base measure with agg and column first."
            )
        replacement = exp.Paren(this=resolve(node.name.removeprefix(_MEASURE_PREFIX)))
        if node is tree:
            return replacement
        node.replace(replacement)
    return tree


def user_references(text: str) -> list[str]:
    return [m.group(1) for m in USER_REF.finditer(text)]


def _literal(value: Any) -> exp.Expr | None:
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, int | float):
        return exp.Literal.number(value) if value == value else None
    if isinstance(value, str):
        return exp.Literal.string(value)
    return None


def rls_expression(
    text: str, alias: str, columns: Sequence[str], user: Mapping[str, Any] | None
) -> exp.Expr:
    """An RLS filter with user values substituted as literals.

    `user` maps "email", "role" and "attributes.<name>" to values. With
    user=None the expression is only checked (model validation). If a
    referenced value is missing, empty or of an unusable type, the rule
    denies every row.
    """
    references: list[str] = []

    def stash(match: re.Match[str]) -> str:
        references.append(match.group(1))
        return f"({_USER_PREFIX}{len(references) - 1})"

    prepared = USER_REF.sub(stash, text)
    if "{{" in prepared or "}}" in prepared:
        raise ExpressionError(
            "Use {{ user.attributes.<name> }}, {{ user.email }} or {{ user.role }}."
        )
    tree = _resolve_columns(parse(prepared), alias, columns, skip_prefix=_USER_PREFIX)
    if user is None:
        return tree

    for node in list(tree.find_all(exp.Column)):
        if not node.name.startswith(_USER_PREFIX):
            continue
        value = user.get(references[int(node.name.removeprefix(_USER_PREFIX))])
        target: exp.Expr = node
        while isinstance(target.parent, exp.Paren):  # the parentheses added above
            target = target.parent
        parent = target.parent
        if isinstance(value, list | tuple):
            literals = [_literal(v) for v in value]
            in_list = isinstance(parent, exp.In) and target in parent.expressions
            if not value or any(lit is None for lit in literals) or not in_list:
                return exp.false()  # empty list, bad values, or a list used as a scalar
            assert isinstance(parent, exp.In)
            position = parent.expressions.index(target)
            parent.set(
                "expressions",
                [*parent.expressions[:position], *literals, *parent.expressions[position + 1 :]],
            )
            continue
        literal = _literal(value)
        if literal is None or value == "":
            return exp.false()  # the user has no such attribute: deny
        if target is tree:
            return literal
        target.replace(literal)
    return tree
