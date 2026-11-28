"""The workspace model (v2 V2, ADR-031): one model for the workspace, with tables and the
relationships between them, and its version history.

The data step makes version 1 from the v1 models, by the rules of ADR-031
(spec/drafts/v2/README.md):

1. Each v1 model someone edited (`is_starter` false) becomes a table entry: its
   definition without `joins`, with `source: {dataset: <name>}`. Starter models stay
   implicit, generated from the columns when read, as v1 already treated a dataset
   without a stored model.
2. Each v1 join becomes a relationship. `many_to_one` and `one_to_one` joins go from
   the model's dataset to the joined one, active, with `crossFilter` and
   `securityFilter` single and `joinType` the join's `type`. `one_to_many` joins, which
   v1 never used in queries, are turned round into a many-to-one from the other
   dataset and made inactive. The same relationship declared on both sides is kept
   once, active if either was.
3. Ids are `<from>__<to>`, then `_2`, `_3` for another between the same tables.
4. The v1 tables are kept as they were, read only: version 1 of the workspace model is
   "Migrated from the v1 models".

The rules are written out here rather than imported, so the migration never changes
when the app's code does. `tests/test_workspace_model.py` checks they give the same
document as `opsis.semantic.workspace.from_v1_models`.

Revision ID: 0014
Revises: 0013
"""

import copy
import json
from collections.abc import Iterable, Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
NOW = sa.func.now()
MESSAGE = "Migrated from the v1 models"


def _relationship(table: str, join: Mapping[str, Any]) -> dict[str, Any]:
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


def migrated_document(
    models: Iterable[tuple[str, Mapping[str, Any], bool]],
) -> dict[str, Any] | None:
    """The workspace model made from v1 models, given as (dataset name, definition,
    is starter) in dataset name order. None when there are no models."""
    rows = list(models)
    if not rows:
        return None
    tables: dict[str, Any] = {}
    relationships: dict[str, dict[str, Any]] = {}
    for name, definition, is_starter in rows:
        if not is_starter:
            tables[name] = {
                "source": {"dataset": name},
                **{
                    k: copy.deepcopy(v)
                    for k, v in definition.items()
                    if k not in ("joins", "dataset")
                },
            }
    for name, definition, _ in rows:
        for join in definition.get("joins") or []:
            entry = _relationship(name, join)
            same = next(
                (
                    rid
                    for rid, other in relationships.items()
                    if (other["from"], other["to"]) == (entry["from"], entry["to"])
                ),
                None,
            )
            if same is not None:
                relationships[same]["active"] = relationships[same]["active"] or entry["active"]
                continue
            base = rid = f"{entry['from']['table']}__{entry['to']['table']}"
            number = 2
            while rid in relationships:
                rid, number = f"{base}_{number}", number + 1
            relationships[rid] = entry
    return {"modelVersion": 2, "tables": tables, "relationships": relationships}


def upgrade() -> None:
    op.create_table(
        "workspace_model",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("definition", postgresql.JSON(), nullable=False),
        sa.Column("layout", postgresql.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.CheckConstraint("id = 1", name="ck_workspace_model_one_row"),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name="fk_workspace_model_updated_by_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_model"),
    )
    op.create_table(
        "workspace_model_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", postgresql.JSON(), nullable=False),
        sa.Column("author_type", sa.String(20), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column("message", sa.String(500), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name="fk_workspace_model_versions_author_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_model_versions"),
        sa.UniqueConstraint("version", name="uq_workspace_model_versions_version"),
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT d.name, m.definition, m.is_starter FROM semantic_models m "
            "JOIN datasets d ON d.id = m.dataset_id ORDER BY d.name"
        )
    ).all()
    document = migrated_document((name, definition, bool(starter)) for name, definition, starter in rows)
    if document is None:
        return  # a new workspace: the model starts empty, at version 0
    text = json.dumps(document)
    bind.execute(
        sa.text(
            "INSERT INTO workspace_model (id, definition, layout, version) "
            "VALUES (1, CAST(:definition AS json), CAST('{}' AS json), 1)"
        ),
        {"definition": text},
    )
    bind.execute(
        sa.text(
            "INSERT INTO workspace_model_versions "
            "(id, version, definition, author_type, message) "
            "VALUES (gen_random_uuid(), 1, CAST(:definition AS json), 'system', :message)"
        ),
        {"definition": text, "message": MESSAGE},
    )


def downgrade() -> None:
    op.drop_table("workspace_model_versions")
    op.drop_table("workspace_model")
