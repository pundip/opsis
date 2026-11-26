# Opsis v2: the designs (milestone V0)

The plan for `BUILD_BRIEF_V2.md`, written in milestone V0 and approved at the V0 review (2026-11-26; the answers are in section 10). The decisions are ADR-030 to ADR-035 in `DECISIONS.md`; this file has the detail behind them, and each milestone builds from it. The draft schemas and their migrations are in `spec/drafts/v2` (start with its README).

## 1. The workspace model (ADR-031, built in V2)

### 1.1 The document

The demo data after the migration (`spec/drafts/v2/examples/workspace-model.demo.json`), shortened, in YAML:

```yaml
modelVersion: 2
tables:
  demo_gifts:
    source: { dataset: demo_gifts }
    label: Demo gifts
    dimensions:
      gift_date: { column: gift_date, type: time, label: Gift date }
      channel:   { column: channel, type: string, label: Channel }
    measures:
      total_given: { agg: sum, column: amount, format: currency_aud, label: Total given }
      # new in v2: a filter on a related table's column
      major_donor_total: { agg: sum, column: amount, filter: "demo_donors.tier = 'Major'" }
  demo_donors:  { source: { dataset: demo_donors }, ... }
  demo_regions: { source: { dataset: demo_regions }, ... }
relationships:
  demo_gifts__demo_donors:
    from: { table: demo_gifts, columns: [donor_id] }    # the many side
    to:   { table: demo_donors, columns: [donor_id] }   # the one side
    cardinality: many_to_one
    crossFilter: single
    securityFilter: single
    active: true
    joinType: left
  demo_donors__demo_regions: { ... }
layout:
  tables: { demo_gifts: { x: 40, y: 60 }, demo_donors: { x: 380, y: 60 }, demo_regions: { x: 720, y: 60 } }
```

- Table names are dataset or transform names and never change (v1 datasets cannot be renamed either); `label` is what people see.
- Measures aggregate their own table's columns. Their `filter` may use columns of tables reached many-to-one (`demo_donors.tier`), a formula may use another table's measure (`{targets.target}`), and `relationship` picks an inactive relationship (Power BI's USERELATIONSHIP).
- Starter tables stay implicit: a dataset with data and no entry is a table generated from its columns, as in v1.

### 1.2 Storage and the API

- `workspace_model` (one row: definition as JSON, version, layout, updated by and at) and `workspace_model_versions` (one immutable row per save). JSON, not JSONB, to keep the author's key order (ADR-019). Postgres compresses large values, so a whole document per version is affordable for a workspace of tens of tables.
- REST: `GET /api/model` (the part the caller may use, with YAML and warnings), `PUT` (full document or YAML, `baseVersion`), `PATCH` (JSON Patch, `baseVersion`), `POST /validate`, versions, diff and restore, and `GET|POST /api/model-files` extended (section 1.4). The v1 routes `/api/models/{dataset}` keep working for one table for a release, as a view of the workspace model.
- A stale patch is rebased: when none of the paths it touches changed between `baseVersion` and now, it is applied to the current version; otherwise 409, as today.
- The warnings of ADR-028 become warnings per relationship: repeated keys on the one side (with how many), a relationship whose key columns' types do not match, an ambiguous pair of paths, and a relationship into a restricted table.

### 1.3 The Model view

- A canvas built with React Flow (`@xyflow/react`, MIT): tables as cards (name, fields with type icons, keys, row count), relationships as edges with `1` and `*` at the ends, an arrow per filter direction, dashed when inactive. dagre (MIT) for "auto layout". Both work under the strict CSP (no eval).
- Dragging a field onto a field opens the side panel with guessed defaults: cardinality from the keys' uniqueness (one query per side), direction single, active unless it would make two paths of the same length.
- Side panels edit tables, fields (display names, descriptions, hidden, formats, data categories) and measures (a form: name, aggregation, field, optional filter, format, description; or an expression, through the v1 allow-list).
- The YAML tab is Monaco with the schema; Check runs the server's validation. Either side saves the same document, and the canvas redraws from it.
- "Generate a date table": creates a dataset from the built-in date connector (from the earliest date in the model to the end of the next financial year, refreshed daily), marks it, and offers relationships from date columns.
- "Suggest relationships": compares names, types and value overlap (sampled, on the server, as the person: restricted tables excluded) and returns proposals with a confidence and the evidence, for example "98% of gifts.donor_id values exist in donors.id; donors.id is unique". Nothing is applied until confirmed.

### 1.4 Migration and the upgrade check

The migration steps are in `spec/drafts/v2/README.md` and written out in `tests/test_spec_drafts.py` (`_planned_model_migration`). Three points need care:

1. **Which relationships a v1 query used.** v1 used only the joins declared in the query's own model (first step) and in the joined model (second step), many-to-one or one-to-one, never through a restricted dataset. Exactly those become active relationships; `one_to_many` joins become inactive. So every path a v1 query took is still the shortest active path, and v2 picks it.
2. **Filter reach.** v2 lets filters reach further (any number of steps, both directions where set). Dashboards keep the v1 reach until V4 migrates them, and that migration writes down any difference (section 4.3).
3. **Row-level security through relationships** (ADR-032, R2) is the one intended change of results. Before the upgrade, a report lists every model with a rule on a joined table (the `joined_rules_not_applied` warning of ADR-028 finds them).

The check on your stack: V2's first commit adds `python -m opsis results-digest`, and nothing else. Deployed and run before the migration, it writes, for every dashboard (draft and published) and every item, a hash of the rows at the default filter state, as an admin and as each person who has attributes. It also writes the report of which dashboards and which people will see fewer rows once row-level security travels through relationships. **The migration waits until you have seen that report and approved it** (decided at the V0 review). After the upgrade, `results-digest --compare <file>` lists every item whose rows changed, with who saw the difference. Tests do the same on fixtures that cover every v1 join pattern: direct, two steps, inner, `one_to_many` declared, a restricted dataset in between, and two ways to one dataset.

Model files: export writes `models/<table>.yml` (each with a `table` key) and `models/workspace-model.yml` (relationships, layout); `workspace-model` cannot be a table name, because table names have no hyphen. Import accepts that layout, or v1 files (`dataset` plus `joins`), converted with the migration's rules; all files are checked first and nothing is saved unless all are valid, as today.

## 2. Queries across tables (ADR-031, built in V2)

### 2.1 Planning a query

1. **Base tables:** the tables of the measures. One table: v1's single SELECT. Several, or measures that name different relationships: one aggregate block per group, each grouped by the same fields, joined on them (section 2.2, case 4). No measures: `dataset` when given, else the table every dimension's table is reached from; else an error asking for a measure or a `dataset`.
2. **Grouped fields:** each must be on the base table or on a table reached through active many-to-one relationships (the shortest path; a tie is an error naming both paths). Grouping by a field of a table on the many side (donors grouped by gifts.channel) is refused, as in v1: it would count a donor once per channel, which needs Power BI's evaluation model. Backlog.
3. **Filters and slicers:** a field on the base table or on a joined table is a WHERE condition, as in v1. Otherwise, when a filter path exists from the field's table to the base table, where each step goes from the one side to the many side, or the other way on a relationship with `crossFilter: both`, the condition becomes nested semi-joins. Otherwise the filter does not reach this query: a dashboard simply does not apply it there, and a direct query says why.
4. **Security:** every table is read through its secured rows (section 3), whatever role it plays in the query.

### 2.2 The SQL shapes

Case 1, the v1 shape, unchanged (a field two steps away):

```sql
SELECT "demo_regions"."state" AS "demo_regions.state", SUM("demo_gifts"."amount") AS "demo_gifts.total_given"
FROM (<secured demo_gifts>) AS "demo_gifts"
LEFT JOIN (<secured demo_donors>) AS "demo_donors" ON "demo_gifts"."donor_id" = "demo_donors"."donor_id"
LEFT JOIN (<secured demo_regions>) AS "demo_regions" ON "demo_donors"."state_code" = "demo_regions"."code"
GROUP BY 1
```

Case 2, a slicer on the many side filtering the one side (gifts to donors set to `crossFilter: both`), for a table of donors:

```sql
SELECT "demo_donors"."tier" AS "demo_donors.tier", COUNT(*) AS "demo_donors.donor_count"
FROM (<secured demo_donors>) AS "demo_donors"
WHERE "demo_donors"."donor_id" IN (
  SELECT "demo_gifts"."donor_id" FROM (<secured demo_gifts>) AS "demo_gifts"
  WHERE "demo_gifts"."channel" IN ('Online'))
GROUP BY 1
```

Case 3, security through a relationship (R2): a rule on donors, `state_code IN {{ user.attributes.states }}`, makes the secured gifts:

```sql
SELECT * FROM read_parquet([<gift files>]) AS "demo_gifts"
WHERE "demo_gifts"."donor_id" IN (
  SELECT "demo_donors"."donor_id" FROM read_parquet([<donor files>]) AS "demo_donors"
  WHERE "demo_donors"."state_code" IN ('VIC'))
```

Case 4, measures of two tables by month (drill-across):

```sql
WITH g AS (SELECT <month> AS m, SUM(amount) AS total FROM <gifts, joined as needed> WHERE ... GROUP BY 1),
     t AS (SELECT <month> AS m, SUM(target) AS target FROM <targets, joined as needed> WHERE ... GROUP BY 1)
SELECT COALESCE(g.m, t.m) AS m, g.total, t.target, g.total / NULLIF(t.target, 0) AS share
FROM g FULL OUTER JOIN t ON g.m IS NOT DISTINCT FROM t.m
```

The result cache is unchanged: the SQL text holds every snapshot file, value and rule, so it is still the key. Semi-joins are hash semi-joins in DuckDB; V2 adds an RLS rule on donors to the 10 million row benchmark to measure them.

## 3. Security for relationships, transforms and SQL (ADR-032)

### 3.1 R2 in practice

- A rule on `demo_regions` (`state IN {{ user.attributes.states }}`) hides donors in other states (through donors to regions) and their gifts (through gifts to donors): two steps of R2.
- Gifts with no donor, or a donor id that matches nothing, are hidden from people the rule applies to, as Power BI hides them.
- A dimension table is not filtered by a rule on the fact table unless `securityFilter: both`: someone limited to Victorian gifts still sees every donor's name in a donor slicer. To hide those, put a rule on the dimension, or set both. The Model view says so next to the setting.
- Someone who may not use `demo_donors` (restricted) but may use `demo_gifts` still gets their gifts filtered by the donors rule, without an access error: the donors rows only remove gift rows, and none of them are returned.

### 3.2 R3 and R4 in practice

Input `gifts` has the rule `region IN {{ user.attributes.regions }}`:

| Transform | Inherited? | Why |
|---|---|---|
| `SELECT * FROM gifts WHERE amount > 0` | yes, on `region` | column passes through |
| `SELECT region AS gift_region, fund, amount FROM gifts` | yes, on `gift_region` | renamed |
| `SELECT region, fund, SUM(amount) FROM gifts GROUP BY region, fund` | yes | every group is one region |
| `SELECT fund, SUM(amount) FROM gifts GROUP BY fund` | no: denies | a total mixes regions |
| `SELECT *, SUM(amount) OVER () AS grand_total FROM gifts` | no: denies | every row carries all regions' total |
| `SELECT *, SUM(amount) OVER (PARTITION BY region) FROM gifts` | yes | the window stays in one region |
| `SELECT * FROM gifts ORDER BY amount DESC LIMIT 10` | no: denies | the top ten depend on other regions |
| `SELECT * FROM gifts WHERE amount > (SELECT AVG(amount) FROM gifts)` | no: denies | the average includes other regions |
| `SELECT g.*, d.tier FROM gifts g JOIN donors d USING (donor_id)` | yes, and donors' rules too if their columns are kept | one gift and one donor per row |

A filter that reaches `gifts` through a relationship (the donors rule of 3.1) is inherited the same way, on `donor_id`: the transform's rows are kept when their `donor_id` is among the visible donors, at query time.

When a rule is not inherited, the table denies those roles, the Model view says which input rule could not be carried over and why, and someone who may edit the input's rules can write a rule with `replaces: {table: gifts}`; when an admin set the input's rule or its restriction, only an admin can. Every replacement is audited, and the Model view warns when it gives wider access: for each person with attributes and each role, it compares the transform built from what they may see of the inputs with what the replacement shows them from the full build, and lists who would see more. For a transform of totals by fund, they might decide fund totals may be seen by everyone (`filter: "TRUE"`), or limit them by fund (`fund IN {{ user.attributes.funds }}`). Opsis stamps `rulesHash`; if the input's rules change later, the replacement stops counting and the table denies again until someone confirms it.

### 3.3 The four gates for SQL

(ADR-032 lists them.) Details that matter:

- The allow-list of functions is v1's (`semantic/expressions.py`) plus aggregates, window functions and the string, date and list functions transforms need, each listed by name. A function DuckDB adds in an upgrade is refused until listed. The sqlglot dialect is `duckdb`, and the DuckDB parse tree check runs on the exact text that runs.
- Steps become a chain of CTEs (`step_1 AS (...), step_2 AS (... FROM step_1 ...)`); `prev` is the step before, skipping disabled steps. A preview runs the chain to the chosen step with `LIMIT 100`, and its row count.
- When a build fails, each step is checked alone in order, so the error names the step, shows its SQL and DuckDB's message with the position in that step.
- The V0 check of the lock, against DuckDB 1.5.6 (the pinned version), in the test image with no network:

| With `allowed_paths` = the input files, external access off, configuration locked | Result |
|---|---|
| a view over the input files; `read_parquet` of them | allowed |
| `read_parquet` of another file, a `..` path, a glob in the snapshot folder, `read_csv`, `read_text('/etc/passwd')`, `glob('/etc/*')`, an `https://` URL, `ATTACH` | refused |
| `COPY (SELECT 1) TO` a file in the snapshot folder, over an input file, or anywhere else | refused (into the snapshot folder it was allowed with `allowed_directories`, hence exact paths) |
| `INSTALL httpfs`, `LOAD httpfs`, `getenv(...)` | refused |
| `SET enable_external_access = true`, changing `allowed_paths` or `memory_limit` | refused (locked) |
| `query('SELECT 42')`, `duckdb_settings()` | allowed: only gates 1 and 3 stop them |
| two statements in one string | DuckDB's parser returns both (`extract_statements`); gate 3 refuses |
| a sort of 20 million rows with a 64 MB memory limit and a temp directory set before the lock | finishes (DuckDB spills such sorts to its temp directory), so a build's memory limit does not cap its data size |

V3 moves every query onto the locked connection, not only SQL written by people: the compiler's `read_parquet('<dir>/part-*.parquet')` becomes an explicit file list, because the lock refuses globs.

### 3.4 Transforms: storage, builds, lineage

- A transform is a dataset of another kind (`datasets.kind = 'transform'`, no connection), so snapshots, retention, previews, profiles, the data quality report, restriction, grants, starter tables and exposure checks all work unchanged. `transform_versions` holds each saved definition (`transform.schema.json`), with diff and restore; a snapshot records the transform version and the input snapshots it read.
- A build is a job (`transform.build`) that pins its inputs' current snapshots, runs on the locked connection with `TRANSFORM_TIMEOUT_SECONDS` and `TRANSFORM_MEMORY_LIMIT_MB`, streams Arrow batches to Parquet in a `.tmp` folder and swaps the pointer in one transaction, as a refresh does.
- The dependency graph comes from the names in each transform's SQL; a save that makes a cycle is refused. When a refresh or build succeeds, every transform downstream is queued in dependency order with one chain id, which the Jobs page shows as a chain. The runner runs one job at a time, so the order holds.
- Lineage: connections, datasets, transforms, model tables, dashboards (and which items use which fields), drawn with the Model view's canvas.

### 3.5 Tests that must pass (written with the features)

- **V2, R2:** a rule on donors hides other donors' gifts, gifts with no donor and gifts of unknown donors; two steps (regions to donors to gifts); `securityFilter: both`; inactive relationships carry nothing, and a measure over one shows no hidden gift; admins are exempt with the badge and share links are not; previews, profiles, filter options and `describe_dataset` samples follow it; a person who may not use donors still gets filtered gifts, with no access error; a loop through a table with rules is refused.
- **V3, R3 to R6:** every row of the table in 3.2; a rename; a self-join with the rule on both sides; UNION ALL with an input that has no rule; a rule reaching an input through a relationship (on `donor_id`, and dropped); the source rule edited after the transform was built; a re-declaration by someone allowed and someone not, its audit row, and its hash going stale; restriction inheritance (two restricted inputs, someone on one list only), the admin waiver, and exposure checks through a transform.
- **V3, gates:** every refused row of the table in 3.3, plus `SET`, `PRAGMA`, `CALL`, `EXPORT DATABASE`, comments hiding a second statement, unicode look-alike names and a reference to a dataset the person may not use, each sent to each gate on its own, so every gate is shown to refuse it without the others.

## 4. Pages, slicers and interactions (ADR-033, built in V4)

### 4.1 Which items a slicer filters

For a slicer S and an item I, both in the spec:

1. I is in S's scope (S's page, the listed pages, or every page) and is not S.
2. `interactions.matrix[S][I]` is not `none`.
3. The model relates S's field to I's query (section 2.1, step 3). If not, S does not filter I, and the editor's Edit interactions view shows I as "not related".

Cross-filters from a visual follow steps 2 and 3, and use only fields the dashboard itself uses (ADR-024). Filter bar controls keep `targets`. `highlight` sends I's query twice, with and without S's condition, and the visual draws the selected part over the whole.

### 4.2 State, options and the agent

- The view state grows to `{filters, slicers, cross, grains, page}`; slicer values are keyed by sync group, else by slicer id. The URL keeps the `s` parameter; links made before v2 still open.
- Slicer options come from `POST /api/dashboards/{id}/slicer-options` (as filter options do today), narrowed by the other slicers that filter that slicer, with RLS as the viewer.
- Hierarchy selections become `or` groups (`semantic-query.schema.json`); a date hierarchy's selected periods become date ranges.
- `list_visuals` gains `slicer` with ready-made example items, and the agent's guide covers pages, slicers and interactions. JSON Patch paths stay `/items/<n>`.

### 4.3 Migrating specVersion 1

On read, never rewriting stored versions:

1. `specVersion: 2`. No `pages`: one page. Items keep `dataset`.
2. For each filter control without `targets`: the items it reaches under the v1 rule (the item's dataset reaches the field in at most two many-to-one steps) and under the v2 rule. If they differ, write the v1 set as `targets`.
3. For each pair (cross-filtering visual A, item B): if v2 would apply A's selection to B and v1 would not, write `matrix[A][B] = "none"`.
4. If the v2 rule reaches fewer items than v1 (possible only after someone changes the model), report it on the dashboard's validation; nothing is invented.

The demo dashboard needs only step 1 (`examples/dashboard.demo.json`).

### 4.4 Phone layout (V6)

Below 768 px the dashboard shows the current page as one column: tiles in `phone.order`, then the rest in reading order, `phone.hidden` left out, at `phone.h` or their own height. The editor's "Phone layout" mode edits those three values on a phone-shaped canvas. Slicers and filters open as bottom sheets. Tiles below the fold load when they come near the screen.

## 5. Visual packages (ADR-034, built in V5)

### 5.1 The package and the contract

```
my-chart.opsisviz
  manifest.json      the manifest (visual-package.schema.json)
  visual.js          the entry: one ES module, export function create(host)
  style.css          optional
  icon.svg           optional, at most 50 kB
  sample-data.json   optional, the SDK's data shape
  README.md          optional, shown as text in the playground
  LICENSE            optional
```

```ts
export interface Visual { update(data: VisualData, config: Record<string, unknown>, context: Context): void | Promise<void>; destroy(): void }
export interface VisualData {
  fields: { name: string; label: string; type: "string" | "integer" | "float" | "decimal" | "boolean" | "date" | "timestamp"; role: string; format?: string; grain?: string }[];
  columns: unknown[][];            // columns[i][row], same order as fields
  rowCount: number; truncated: boolean;
}
export interface Context { theme: Theme; mode: "light" | "dark"; size: { width: number; height: number }; locale: string; calendar: Calendar; selection: Selection | null }
export interface Host {
  select(selection: Selection | null): void;   // { field, values }: values from this data only
  drill(grain: string): void;
  tooltip(tip: { x: number; y: number; items: { label: string; value: string }[] } | null): void;
  requestResize(height: number): void;          // with the resize capability
  log(level: "info" | "warn" | "error", message: string): void;
  lib(name: "echarts" | "d3"): unknown;         // the libraries the manifest asked for
}
```

### 5.2 Messages

Every message is `{opsis: 1, type, id, payload}`, plain JSON, checked against a schema on arrival.

| From the page | Payload |
|---|---|
| `init` | SDK version, theme, locale, calendar, size |
| `update` | data, config, context |
| `resize` | width, height |
| `destroy` | none |

| From the frame | Payload | Limits |
|---|---|---|
| `ready`, `rendered` | the update id | the 5-second timeout waits for `rendered` |
| `select` | field and values, or null | values must be in the tile's data; at most 1,000 |
| `drill` | grain | only with the drill capability |
| `tooltip` | position and items | drawn by the page, outside the frame |
| `resize` | height | only with the resize capability |
| `log` | level and text | 2 kB each, 50 a second, shown in the playground |
| `error` | message and stack | shown on the error tile |

The page accepts a message only when `event.source` is that tile's frame and `event.origin` is `"null"`.

### 5.3 The frame page

`GET {BASE_PATH}/visual-frames/<id>/<version>` returns HTML built from the stored package: the SDK runtime (inline, by hash), the requested libraries and the bundle as text blocks that the runtime imports from `blob:` URLs, and the package CSS. The response is the same for every request, so its policy uses hashes and it is cached as immutable:

```
Content-Security-Policy: default-src 'none'; script-src 'sha256-<runtime>' blob:; style-src 'unsafe-inline';
  img-src data: blob:; font-src data:; connect-src 'none'; frame-src 'none'; worker-src 'none';
  form-action 'none'; base-uri 'none'; frame-ancestors 'self'; sandbox allow-scripts
X-Frame-Options: SAMEORIGIN    X-DNS-Prefetch-Control: off    X-Content-Type-Options: nosniff
```

The page needs no sign-in (it holds code, not data), so share links work. The runtime removes `RTCPeerConnection` and its relatives, installs error handlers, calls `create(host)` and answers the messages. In the export file the same HTML goes into `srcdoc`.

To verify in V5 in Chrome, Firefox and Safari: importing a module from a `blob:` URL inside an opaque-origin frame (fallback: the bundle calls `opsis.register(create)`), and that Opsis's `frame-src 'self'` stops a frame that navigates itself to another site. The V5 test package tries `fetch`, `document.cookie`, `parent.document`, `top.location`, `window.open`, an image from another site, a self-navigation and WebRTC, and every one must fail.

### 5.4 Install, versions and the playground

- The playground (admins): upload a package; it is checked and held as pending (not installed); preview it with its sample data or a dataset and field bindings, at any size, light or dark, with its log, errors and a simulated selection; then Install.
- Installing a new version keeps the old ones; items keep their `visualVersion` until someone chooses "Use version 1.3.0" in the editor. A version in use cannot be removed (the dashboards are listed). Disabling a package shows its tiles as disabled without changing any spec.
- `list_visuals` lists installed packages with their roles, aiHints and example items, so the agent can use them.
- Backups add `/data/visual-packages`.

## 6. Export (ADR-035, built in V7)

### 6.1 Each tile's data

For a tile T, the fields it is grouped by are: its own bound fields at their grain; the field of every filter bar control and slicer that can reach it (section 4.1); and the grouped fields of every visual whose cross-filter can reach it. Fixed filters and segments are applied on the server, at export. Date fields are grouped by day when a control can choose single days; if that makes T too big, by month, and the export's date controls then choose months.

| Measure | Kept in the file | Worked out offline |
|---|---|---|
| sum, count | the sum | sum of sums |
| min, max | min or max | min of mins, max of maxes |
| avg | sum and count | sum / count |
| count distinct | value lists while they fit the cap, else a HyperLogLog sketch | size of the union (exact), or the sketch's estimate (≈) |
| conditional (`filter`) | as its aggregation | as its aggregation |
| formula | its measures | the formula, from a small expression tree (no eval) |

Sorting and limits are applied offline after filtering, so the file holds every group of T. Identical tile data is stored once. Strings are stored once per field (dictionary), dates as day numbers.

The estimate counts each tile's groups with one `COUNT(*)` per tile, as the person, and multiplies by the bytes of its columns.

### 6.2 The file

```
<!doctype html>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'sha256-<viewer>' 'sha256-<frame runtime>' blob:;
  style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'">
<style> ... </style>
<script type="application/json" id="opsis-export">{ spec, pages, who, when, as-at times }</script>
<script type="application/octet-stream" id="opsis-data">(gzip, base64)</script>
<script type="module">(the viewer: React, ECharts, the app's visuals, the engine)</script>
```

The viewer reuses the app's visuals and chart options (a second Vite entry), so a chart looks the same offline. Imported visuals run in `srcdoc` frames with section 5.3's runtime; a `srcdoc` frame inherits the file's policy, which is why that policy also allows the frame runtime and `blob:`. To verify in V7: browsers load `srcdoc` frames under this policy, and no frame can navigate anywhere. The viewer is responsive, so the phone layout works offline too.

### 6.3 PDF and PNG

The `renderer` service (Compose profile `render`, flag `FEATURE_STATIC_EXPORT`): a small image with headless Chromium and one internal endpoint. The api builds the export file as the person, posts it with a width (desktop or phone) and the page, and gets back a PDF (one page per dashboard page) or a PNG. Chromium runs with networking off. The renderer is not published on any port.

## 7. The other milestones, briefly

- **V1, bring your own agent:** keys in a `user_api_keys` table (provider, label, last four characters, encrypted with `ENCRYPTION_KEY`, default model), deleted with their person; the OpenAI provider uses the official `openai` SDK (Apache-2.0) and its Responses API with tool calling and streaming, behind the existing tool loop, budgets and undo; model lists from each provider's models endpoint. `ASSISTANT_PROVIDER=anthropic` no longer needs `ANTHROPIC_API_KEY` at start, since people may bring their own. Usage per person, key and provider in the audit log and on the Account page. A test searches every API response, log line and audit row for the keys used in the tests.
- **V6, mobile:** react-grid-layout is not used below 768 px; a one-column layout (section 4.4) renders the same tiles. Playwright runs at 390 x 844 (phone) and 820 x 1180 (tablet), at the root and under `/opsis`. A web app manifest (home-screen icon, no offline data) costs a manifest and an icon, so it is in.

## 8. MCP tools, by milestone

| Milestone | Tools |
|---|---|
| V1 | none (MCP clients bring their own model) |
| V2 | `get_workspace_model` (compact: tables, field counts and relationships, or one table in full), `patch_workspace_model` (JSON Patch with `base_version`), `validate_workspace_model`, `suggest_relationships` (proposals with confidence and evidence, nothing applied). `get_semantic_model`, `patch_semantic_model` and `validate_model` keep working for one table for a release. |
| V3 | `list_transforms`, `get_transform`, `create_transform`, `patch_transform`, `preview_transform_step` (100 rows, RLS applied), `run_sql` (SQL Lab, 1,000 rows, RLS applied, behind `FEATURE_SQL_LAB`), `get_lineage` |
| V4 | none new: `list_visuals` includes `slicer`; `get_spec_schema` returns specVersion 2 |
| V5 | none new: `list_visuals` includes installed packages |
| V7 | none: exports are made by people (ADR-035) |

The assistant gets the same tools, with v1's limits (ADR-025): it changes only the open dashboard, and creates nothing that publishes or shares.

## 9. Risks

| | Risk | Mitigation |
|---|---|---|
| V-R1 | The migration changes a result someone relies on | Upgrade report before; `results-digest` before and after; fixtures for every v1 join pattern |
| V-R2 | The compiler grows (semi-joins, per-table blocks, security filters) | Results compared with hand-written SQL, as in M3; the v1 shape stays the common case |
| V-R3 | Security semi-joins are slow on large tables | Measured on the 10 million row benchmark in V2 with an RLS rule on donors |
| V-R4 | One model document: size and two editors at once | Compressed storage; patches rebased when their paths did not change |
| V-R5 | User SQL: a parser difference, or a new DuckDB function | Two parsers, an allow-list that refuses what it does not know, the locked connection; DuckDB pinned to a minor version and the security tests rerun on every upgrade |
| V-R6 | Sandbox gaps: WebRTC, a frame stuck in a loop, side channels | Runtime removes WebRTC; error tile and timeout; admins install after the playground; what a visual can see is its own tile's rows |
| V-R7 | Export size grows with every control | Estimate per tile, a cap, and fixing a control for the offline copy |
| V-R8 | Distinct counts offline | Exact while they fit, then marked as approximate |
| V-R9 | Chromium to keep patched in the renderer image | Optional profile; pinned image, updated deliberately |
| V-R10 | Transform builds in local mode run inside the api process (inline runner), next to queries | A memory limit per build and one job at a time; the worker in server mode |
| V-R11 | Scope: seven milestones, V2 and V3 the largest | Each milestone stands alone; the drafts let later ones start from a reviewed shape |

Still open from v1: R2 (DuckDB memory in the api process), R3 (snapshot rewrite cost, which transforms add to), R5 (a single api process), R8 (hot reload on the Windows filesystem).

## 10. Decided at the V0 review (2026-11-26)

1. **Row-level security through relationships applies to migrated models,** after a report of which dashboards and which people will see fewer rows has been shown and approved (section 1.4).
2. **Measures from several tables in one visual, and measures that name a relationship,** are both in V2.
3. **Many-to-many relationships:** backlog.
4. **Replacement rules on a transform:** anyone allowed to edit the input's rules, except where an admin set the input's rule or its restriction, when only an admin may. Every replacement is audited, and the UI warns clearly when one gives wider access than the input's rule (section 3.2).
5. **PDF and PNG** come from the optional renderer container.
6. **The tests of the security rules** (section 3.5) must exist and pass before V2 and V3 count as done.
7. **Pages in V4.** The export cap is 25 MB by default, and admins can change it.
