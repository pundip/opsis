# Draft schemas for v2

Written in milestone V0 of `BUILD_BRIEF_V2.md`. These are proposals, not contracts yet: the running app does not read this folder (it loads `spec/*.schema.json` only, without subfolders). Each file moves up into `spec/`, replacing the v1 file of the same name, in the milestone that builds it, together with its code and migration. `workspace-model.schema.json` and `semantic-query.schema.json` moved up in V2: they are now `spec/workspace-model.schema.json` and `spec/semantic-query.schema.json`, and the app reads them. Their rows below say what they changed. Designs: `docs/V2_DESIGN.md`. Decisions: ADR-030 to ADR-035 in `docs/DECISIONS.md`.

`backend/tests/test_spec_drafts.py` checks that every draft (and the two promoted ones) is a valid JSON Schema (2020-12), that every example validates, that v1 documents from the demo data survive the planned migrations, and a few things the drafts must refuse.

## The files

| Draft | Replaces | Promoted in | What changes |
|---|---|---|---|
| `workspace-model.schema.json` | `semantic-model.schema.json` (kept, to import v1 files) | V2 (done, now in `spec/`) | New document: `tables` (each one v1 model without `joins`, plus `source`, `hidden`, `dateTable`, field `hidden`, `format`, `dataCategory`), `relationships` (from, to, cardinality, crossFilter, securityFilter, active, joinType), `layout`. Measures gain `relationship` (Power BI's USERELATIONSHIP) and may use `{table.measure}` and `table.column` of related tables. Transform tables' rules gain `replaces`. |
| `semantic-query.schema.json` | same name | V2 (done, now in `spec/`) | `dataset` becomes optional (the base table comes from the measures). A filter may be a group of alternatives, `{"or": [[...], [...]]}`. Measures may come from several tables. Additive: every v1 query is still valid. |
| `dashboard.schema.json` | same name | V4 | `specVersion: 2`. `pages`, and `page` on items. Slicer items (`visual: "slicer"` with `slicer` options and a `field` binding). `interactions.matrix` (Edit interactions: filter, highlight or none). `phone` on items (V6). `visualVersion` for imported visuals (V5). `dataset` optional on items. At most 200 items (was 100). |
| `visual-manifest.schema.json` | same name | V5 | `capabilities` is a list (`crossFilter`, `highlight`, `drill`, `resize`) instead of an object of two flags. `author`. The package fields: `sdkVersion`, `entry`, `style`, `icon`, `licence`, `sampleData`, `libraries`. |
| `visual-package.schema.json` | (new) | V5 | A manifest that must also have `sdkVersion`, `entry` and `author`. |
| `transform.schema.json` | (new) | V3 | A transform: `steps` (id, name, sql, enabled, the quick `action` that wrote it) or one `query`, and build limits. |

## The migrations planned

**Models (V2), one Alembic migration plus a data step, no data files touched.**

1. Each v1 model that someone edited (`semantic_models.is_starter = false`) becomes a table entry: its definition without `joins`, with `source: {dataset: <name>}`. Starter models stay implicit, as v1 already treats a dataset without a stored model: generated from the columns until someone edits them.
2. Each v1 join becomes a relationship. `many_to_one` and `one_to_one` joins: from the model's dataset to the joined one, active, `crossFilter: single`, `securityFilter: single`, `joinType` = the join's `type`. `one_to_many` joins, which v1 never used in queries: turned round into a many-to-one from the other dataset, inactive, so they still change nothing. Identical relationships declared on both sides are kept once (the active one).
3. Ids are `<from>__<to>` (`_2`, `_3` for a second one between the same tables).
4. The v1 version history stays readable; the workspace model starts at version 1, "Migrated from the v1 models".
5. Two behaviour changes, both towards showing less, are listed by the upgrade report (`python -m opsis results-digest`, ADR-038) before the migration: row-level security on a joined table now filters the tables that join to it (the brief's rule, ADR-032), and a loop of active relationships that a rule could travel round (a table with rules on the loop, or reachable from it) is refused. The migration does not choose which relationship of such a loop to make inactive: the report lists the loop under "To resolve before the migration", and until someone makes one of its relationships inactive, the tables on it or reaching it cannot be read. See ADR-031 for how "every dashboard renders unchanged" is checked on your stack.

`examples/workspace-model.demo.json` is the demo's three models after these steps.

**Semantic queries (V2):** none needed.

**Dashboards (V4), when a version is read, never rewriting stored versions.** `specVersion` becomes 2; nothing else moves (no pages means one page, items keep their `dataset`). Where the v2 rule for which items a filter or cross-filter reaches (through the model's relationships) would differ from the v1 rule (at most two many-to-one joins from the item's dataset), the migration writes it down: `targets` on the filter, and `none` in `interactions.matrix` for a cross-filter pair. So the move changes no result. Saving writes specVersion 2. `examples/dashboard.demo.json` is the demo dashboard after migration (only `specVersion` changes, because all its items read `demo_gifts`).

**Visual manifests (V5):** the nine built-in manifests are files, changed with the code: `capabilities: {crossFilter, drillDown}` becomes a list (`drillDown` is `drill`), and `author: "Opsis"`. Nothing stored refers to capabilities.

**Transforms (V3):** new, nothing to migrate.

## Examples

| File | Shows |
|---|---|
| `examples/workspace-model.demo.json` | The demo models migrated: three tables, two relationships, a layout. |
| `examples/semantic-query.across-tables.json` | No `dataset`, fields from three tables, and an `or` group as a hierarchy slicer makes it. |
| `examples/dashboard.demo.json` | The demo dashboard as specVersion 2. |
| `examples/dashboard.pages-and-slicers.json` | Pages, slicers sharing a selection across pages (`syncGroup`), a dashboard-wide slicer, a date hierarchy slicer, Edit interactions, phone settings, an imported visual with its version. |
| `examples/visual-package.candlestick.json` | The manifest of the reference candlestick package (V5). |
| `examples/transform.a-codes.json` | A transform in steps, some written by quick actions (made-up column names). |
