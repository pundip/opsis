# Progress

Last updated: 2026-11-15 (end of Milestone 4)

## Milestone status

| Milestone | Status |
|---|---|
| M0 Plan | Done, reviewed |
| M1 Skeleton and modes | Done, reviewed, catch-up to the revised brief done |
| M2 Data ingestion | Done, reviewed |
| M3 Semantic layer and queries | Done, reviewed |
| M3.5 Timezones, conditional measures, model versions, benchmark | Done, reported with M4 |
| M4 Dashboards and visuals | Done, awaiting review |
| M5 MCP | Not started |
| M6 Auth and sharing | Not started |
| M7 In-app assistant | Not started |
| M8 More connectors and hardening | Not started |

## M4 Dashboards and visuals

Built (decisions in ADR-021, rjsf decision in ADR-018):

- **Backend:** `dashboards` and `dashboard_versions` tables (migration `0005`). `opsis/dashboards/spec.py` validates a spec in two phases with a JSON pointer path on every problem, and derives the semantic query for an item from its bindings plus the view state. `manifests.py` loads the visual manifests. `opsis/services/dashboards.py` and `opsis/api/dashboards.py`: list, create, read, save with `baseVersion` (409 when stale), versions, restore, publish, unpublish, delete, validate, item data, filter options. Also `GET /api/visuals` and `GET /api/schemas`.
- **Visuals:** table (with conditional formatting), KPI card (with comparison to the previous period or year), bar, line, area, combo, pie or donut, scatter, text (safe Markdown subset). Each is a folder with a manifest and a renderer in `frontend/src/visuals/`.
- **Editor** (`/dashboards/:id/edit`): grid with drag and resize, add and remove visuals, role-based field wells, options panel generated from each visual's `configSchema` (rjsf with a CSP-safe validator), filters panel, dashboard settings, undo and redo, version history with a line diff and restore, a Monaco JSON tab, Save and Publish.
- **Viewing** (`/dashboards/:id`): filter bar (dropdown, multi-select, date range, search), cross-filtering by clicking a mark or a table value, drill through time grains per tile, chart to table toggle, light and dark themes, an "RLS bypassed" badge on any tile where an exemption applied.
- Viewers read the published version only and never send a query; the backend derives it from the stored spec.

### How M4 was verified

- **Automated:** 210 backend tests pass (6 new in `tests/test_dashboards.py`, against a real Postgres source: validation paths, versions and restore, stale save refused, publish and what a viewer sees, item data equal to the semantic query, filters, cross-filter on a joined field, drill grain, KPI comparison, filter options). `ruff check`, `ruff format --check` and `mypy --strict` pass. Frontend: `tsc`, ESLint and 39 Vitest tests pass (chart options, palette rules, Markdown safety, schema validator, history, diff); the production build succeeds.
- **Acceptance, by hand in the Browser pane under http://localhost:8088/opsis/** on the sample `gifts` and `donors` data: created a dashboard and built six visuals (KPI, bar, line, pie, table, text) and two filters through the editor controls; saved; reloaded and saw the same six; changed the date filter ($12.5M for all dates, $3.7M for last financial year); changed the drill grain; switched a chart to a table; cross-filtered on donor region (KPI fell to $615.7K, chip shown and removable). Then: selected a tile by clicking it, removed it, Undo brought it back, Redo removed it again; dragged a tile to a new place and resized another by its corner, each undone with one Undo; saved (version 3); History showed the versions and a one-line diff against version 2; restored version 2 (became version 4, the resize was gone). JSON tab: Monaco loaded with its worker; broken JSON was refused with the parser's position; a wrong type was refused with `/interactions/crossFilter 'yes' is not of type 'boolean'`; an unknown field was refused with `/items/0/bindings/value/0 'gifts.no_such_measure' is not a field that 'gifts' can use`; nothing changed on the canvas in those three cases; a valid spec applied and one Undo reversed it. Publish worked; the view page then wrongly said "Draft", which was fixed and confirmed at the root. Dark mode rendered with the dark palette. Bars measured 24px wide on the canvas. No CSP violations and no horizontal overflow.
- **Repeated at the root, http://localhost:8080,** after a full image rebuild: the same dashboard rendered (six tiles, badges, published); remove, save, history diff, restore, Monaco and publish all worked; a new dashboard was created from the list page, a KPI bound and saved; area, combo and scatter visuals were added and rendered. Every API response in that run was 200.

Bugs found by this testing and fixed: a cross-filter on a joined dataset's field was ignored; the unsaved preview failed when a sort named a field that was no longer bound; clicking a tile did not select it (the grid library owns the tile's mouse handlers); Undo did not restore a removed tile because the grid's automatic re-packing was recorded as an edit; the view page said "Draft" when the draft was the published version.

Not verified:

- Area, combo and scatter were only checked to render with data at the root. Their options, and the table's conditional formatting, were not exercised by hand (the option builders are unit-tested).
- Charts were judged from measurements and the accessibility descriptions more than by eye, because the Browser pane is narrow. Worth a look on a full-size screen at review.
- A viewer account using a dashboard in the browser. There is no sign-in until M6; viewer behaviour is covered by API tests only.
- The select, multi-select and search filter controls by hand under both URLs (date range and multi-select were used under /opsis; search only in tests).
- Keyboard-only use of the editor, a screen reader, and touch devices.
- Dashboards on large data, and many tiles at once against the concurrency limit.
- No Playwright smoke test exists yet. Browser checks are manual.
- Everything already listed as unverified for earlier milestones (LXC, real proxy, https, `caddy`, `tunnel` and `ollama` profiles, worker mode for dashboards).

## Review outcomes so far

- **M0:** keep psycopg; `Australia/Melbourne`, Monday weeks, financial years in v1 (ADR-012); Apache 2.0; deviations ADR-005, 006 and 007 approved.
- **M1:** brief replaced with the current version (sub-path hosting, section 3.1); ADR-013 superseded by ADR-016; `FISCAL_YEAR_LABEL` added; one-shot config check; placeholder secrets refused in server mode; `httpx2` kept.
- **M2:** keep the built-in schema form for connections; at the start of M4 prototype rjsf with a CSP-safe validator and decide then (ADR-018). `BIND_ADDRESS` stays `0.0.0.0` in the server template and is not a required value. Editors may see connections and their non-secret settings.
- **M3:** approved with M3.5 (timezones, conditional measures, model versions, value checks, benchmark; all done). Admins stay exempt from RLS by default, with a visible badge (done in M4), share links never exempt and "View as" in M6. Bulk model export and two-hop joins go to M8. Every milestone is verified at the root and under /opsis.
- **For the LXC work (noted at the M2 review):** the server will use port forwarding to the LXC. Check then what source address the api sees for proxied requests, because that is the address `TRUSTED_PROXIES` must list (it may be the forwarder or the Docker gateway, not the reverse proxy itself).

## What exists

Run: `copy .env.local.example .env`, `docker compose up -d`, open http://localhost:8080. Add `COMPOSE_PROFILES=sample` for a sample MySQL source (see README).

**Platform (M1 and catch-up)**
- `compose.yml` profiles: `server` (worker), `caddy`, `tunnel`, `ollama`, `proxytest`, `sample`. A one-shot `config-check` service gates `api` and `worker`.
- `opsis/settings.py`: all flags, fail-fast rules (ADR-014), `base_path` derived from `PUBLIC_BASE_URL`, `TRUSTED_PROXIES`.
- `opsis/main.py`: everything mounted under the base path; `index.html` served with a runtime `<base href>` and JSON config block; security headers; CSRF check (`opsis/auth/cookies.py`).
- `opsis/auth/principal.py`: `Principal`, roles, permissions. `AUTH_MODE=none` uses a built-in admin. Other modes return 401 until M6.
- `opsis/api/sse.py`: SSE helper for use behind proxies (first real stream in M5).

**Data ingestion (M2)**
- Connectors: `opsis/connectors/file` (CSV, Excel), `postgres`, `mysql`, with `registry.py`, `sql_base.py` and `tabular.py`. Drop-in connectors go in `plugins/connectors/<id>/`.
- `opsis/services/`: `connections.py`, `datasets.py`, `refresh.py` (the refresh job), `crypto.py` (Fernet).
- `opsis/storage/snapshots.py`: Parquet snapshots for full, append and merge refresh; preview; cleanup.
- `opsis/jobs/`: `queue.py` and `worker.py` (`run_one`, the worker loop, and `InlineRunner`).
- `opsis/api/ingestion.py`: connectors, connections, uploads, datasets, jobs.
- Frontend: Connections, Datasets, dataset page, Jobs.

**Semantic layer (M3)**
- `spec/semantic-model.schema.json`: the model format (a fourth spec file).
- `opsis/semantic/`: `model.py` (parse, validate, starter models), `expressions.py` (the allow-list for author-written SQL), `compiler.py` (semantic query to DuckDB SQL through a sqlglot tree, with RLS), `calendar.py` (grains, ranges, fiscal years).
- `opsis/query/duckdb_engine.py`: `DuckDBEngine` (per-query connection, memory limit, timeout) and `ResultCache` (LRU).
- `opsis/services/models.py` (storage, YAML import and export, field lists) and `query.py` (validate, compile, run through the cache).
- `opsis/api/semantic.py`: `GET /api/models`, `GET|PUT /api/models/{dataset}`, `/validate`, `/starter`, `/export`, and `POST /api/query` (with `previewAs` for admins and `includeSql`).
- Migration `0003`: `semantic_models`.
- A starter model is generated at a dataset's first refresh and follows its columns until someone edits it.
- Frontend: Semantic models list; a model page with a query builder (measures, group by, date grain and range, one filter, segments, limit, "Run as" for RLS) and a YAML editor with Check, Save, Discard, Regenerate and Download.

**Dashboards (M4)**
- `opsis/dashboards/`: `spec.py` (validation, query derivation), `manifests.py`. `opsis/services/dashboards.py`, `opsis/api/dashboards.py`. Migration `0005`.
- `frontend/src/visuals/<id>/`: manifest and renderer for each of the nine visuals; `registry.ts`; shared `Cartesian.tsx`.
- `frontend/src/lib/`: `dashboard.ts` (types and API hooks), `chartOptions.ts`, `theme.ts` (palette), `history.ts`, `diff.ts`, `markdown.tsx`, `schemaValidator.ts`.
- `frontend/src/components/dashboard/`: `Canvas`, `ItemFrame`, `FiltersBar`, `EditorPanels`, `MonacoJson`; `components/ConfigForm.tsx` (rjsf), `components/EChart.tsx`.
- Pages: `Dashboards`, `DashboardPage`, `DashboardEditor`.

## M3.5 (from the M3 review)

Built (details in ADR-020):

- **Timezones:** timezone-aware timestamps are stored as UTC instants and converted to `WORKSPACE_TIMEZONE` before grouping, ranges and filters. Naive timestamps are workspace-local unless the dimension sets `timezone`.
- **Conditional measures:** `filter` on an aggregate measure, compiled to `agg(...) FILTER (WHERE ...)`, with segments usable by name.
- **Model version history:** immutable versions, author and message, restore, and `baseVersion` conflict detection. A "Version history" panel on the model page.
- **Values:** every query value is type-checked before rendering; new injection tests with quotes, backslashes, comment markers, unicode and null bytes (21 hostile strings, each matched exactly as data and as an RLS attribute).
- **Concurrency:** queries run in the thread pool, at most `QUERY_MAX_CONCURRENT` (default 4) at a time.
- **RLS exemptions:** reported as `rlsBypassed`, with a badge in the query builder; `may_be_exempt=False` for future share links.

Verified: 204 backend tests pass (12 new), lint and strict types clean; 17 frontend tests pass. Checked in the Browser pane at http://localhost:8080 and at http://localhost:8088/opsis/ (save with a note, version list, restore, stale save refused with 409, conditional measure equal to the filtered total, timezone conversion present in the SQL).

### Benchmark, 5 million rows

`node scripts/benchmark.mjs --rows 5000000`, on this laptop: Docker Desktop with 16 CPUs and 15.4 GB of memory available to containers, default settings (`QUERY_MAX_CONCURRENT=4`, `QUERY_THREADS=4`, `QUERY_MEMORY_LIMIT_MB=1024`), inline job runner. Source: a synthetic `gifts` table (8 columns, 3 years of dates, 200,000 donors) in Postgres, plus a 200,000 row `donors` table.

| Step | Result |
|---|---|
| Ingestion, 5.2 million rows from Postgres to Parquet | 18.5 s (about 300,000 rows/s); snapshot 74 MB |
| Peak api memory during ingestion | 264 MB |

| Query | Cold | Warm (cached) | Rows |
|---|---|---|---|
| KPI: total, count, distinct donors, last 12 months | 138 ms | 11 ms | 1 |
| Bar: total by fund, two regions | 77 ms | 12 ms | 8 |
| Line: total by month and channel, 3 years | 96 ms | 13 ms | 148 |
| Join: total and conditional total by donor tier and financial quarter | 250 ms | 13 ms | 39 |
| Table: top 100 of 200,000 donors by total | 197 ms | 14 ms | 100 |

Times are wall-clock for the HTTP request. All are well inside the brief's target of 1.5 s warm for a typical dashboard, at half the brief's 10 million rows.

| 10 different uncached queries at once | Round 1 | Round 2 |
|---|---|---|
| All ten finished in | 3.6 s | 3.4 s |
| Per request: fastest, median, slowest | 1.4, 2.7, 3.6 s | 1.3, 2.6, 3.4 s |
| Longest single engine time | 1.4 s | 1.4 s |
| `/healthz` during the burst: median, worst | 20 ms, 104 ms | 29 ms, 93 ms |

- Peak api memory during the query phases: 1,138 MB (four queries running at once).
- The health check stayed fast during the bursts, which confirms the queries are not blocking the event loop. Requests beyond the four slots waited their turn, which is why the slowest request took as long as the whole burst.
- Each concurrent query was slower than the same kind of query alone (about 1.4 s against 0.1 to 0.25 s), because four queries each using four threads compete for CPU, and these burst queries are heavier (week grain, distinct donors, a HAVING filter).

Not measured: 10 million rows, the Proxmox LXC (fewer cores and less memory than this laptop), slow disks, and worker mode.

## How M3 was verified

All on the Windows laptop with Docker Desktop.

- **Backend:** 192 pytest tests pass; `ruff check`, `ruff format --check` and `mypy --strict` pass. New in M3 (73 tests):
  - Every aggregation, expression measures and nested formulas, compared with results computed in plain Python over 400 generated rows.
  - All eight grains against the calendar rules, plus a sweep of 900 consecutive dates across both week starts and six fiscal start months, for date and timestamp columns.
  - Relative and absolute ranges, every filter operator, measure filters, segments, sort, limit, offset, truncation and the server row cap.
  - Joins, and refusal of fan-out joins, missing joins and measures from a joined dataset.
  - RLS: per attribute, lists and scalars, several rules, roles, `user.email` and `user.role`, rules on joined datasets, eight ways an attribute can be missing or unusable (all deny), and a broken rule blocking instead of failing open.
  - Injection: 23 rejected expressions (subqueries, `read_csv`, `getenv`, semicolons, window and aggregate functions, parameters, unsafe casts and more), SQL inside filter values and inside user attributes, hostile field references, unsafe YAML tags.
  - Model validation paths, starter models, engine timeout, cache keys and eviction.
- **Acceptance, automated (`tests/test_query_api.py`):** datasets loaded from a real Postgres source through the API; semantic query results equal hand-written SQL run on the source for (1) measures by dimension with a filter and a segment, (2) financial quarters joined to another dataset over a date range, (3) a relative financial-year range. RLS preview returns exactly the rows for each attribute set.
- **Acceptance, in the Browser pane:** on the sample `gifts` dataset I edited the model as YAML (the editor reported three deliberate mistakes with their paths, then saved the corrected model with a join and an RLS rule), ran total amount and gift count by fund, and got the same five totals as `SELECT fund_name, SUM(amount), COUNT(*) ... GROUP BY fund_name` on the MySQL source. A query grouped by donor region and financial quarter for "Last financial year" returned Q1 to Q4 FY2026. "Run as a viewer" with `{"funds": ["Library", "Sport"]}` returned those two funds only, and with `{}` returned no rows.
- **Frontend:** `tsc`, ESLint and 17 Vitest tests pass; the production build succeeds.

Three bugs were found by this testing and fixed: YAML reading `on:` as a boolean; the database column type re-sorting model keys; and a save that only reordered fields being skipped.

Not verified:

- Query speed or memory on large data. The largest query ran over 5,000 rows. The 10M-row target is an M8 check.
- The query builder at the `proxytest` sub-path. The pages use the same API client and router as the M2 screens that were tested there, but I did not repeat the browser run under `/opsis`.
- Queries in worker mode specifically (queries run in the api process in both modes, so nothing differs, but it was only exercised in inline mode).
- Concurrent queries under load.
- Everything already listed as unverified for M1 and M2: the Proxmox LXC and real proxy, https end to end, the `caddy`, `tunnel` and `ollama` profiles, dev mode after recent changes, MariaDB and other server versions, and the Mermaid diagrams in a renderer.

## Deviations from the brief

- ADR-005, 006, 007 (approved).
- ADR-016: CSRF protection brought forward from M6.
- ADR-017: the CSV and Excel connector id is `file`, not `csv`.
- ADR-018: connection forms use a small built-in schema form. Visual config panels use rjsf with our own CSP-safe validator instead of ajv (decided in M4). shadcn/ui is still not added.
- ADR-019: a fourth spec file (`semantic-model.schema.json`); query values are SQL literals rendered by sqlglot, not bound parameters (approved at the M3 review, with the value checks of ADR-020); the model editor is still a plain text area (Monaco is now available and could replace it).
- ADR-021 (new, M4): field wells are selectors with add and remove, not drag-and-drop of fields; the combo chart has one value axis, never two; cross-filter and drill state live in the view, not in the spec; the chart palette and mark rules are fixed in code.
- ADR-015: the task runner is npm scripts, not `tasks.py`; `httpx2` for the test client (approved).

## Open issues and notes

- Dashboards have no per-dashboard permissions yet: anyone with `dashboards:edit` can edit any dashboard, and viewers see every published one. Sharing and access arrive in M6.
- Cross-filter, drill and filter values are not kept in the URL, so a reload returns to the defaults.
- A new visual is placed beside the last one when it fits, otherwise below. The grid packs tiles upward, so empty rows cannot be kept.
- The JSON tab's message for broken JSON is the browser's own parser message (position, line and column), not a JSON pointer, because there is no document to point into yet.
- Two open editors on one dashboard: the second save gets a 409 and a message. Live updates arrive with the SSE channel in M5.
- Bundle size: the main chunk is 755 kB (236 kB gzipped), ECharts 562 kB and Monaco 3.4 MB, both lazy. Acceptable for a self-hosted tool; could be trimmed in M8.
- Joins are one hop from the query's dataset (two-hop joins: M8 backlog).
- The query builder has one filter row. The API accepts any number.
- A server install is not usable until M6 (sign-in).
- Merge refresh does not remove rows deleted in the source. A full rebuild does.
- One job runs at a time per install. No cancel or progress for a running job.
- Datasets cannot be edited after creation.
- `cloudflared` and `ollama` images are on `latest` until M6 and M7.
- `deploy/proxmox/README.md` and `deploy/windows/README.md` are not written yet. No `.mcp.json` yet (M5).
- Risks still open: R2 (DuckDB memory in the api process; per-query limit and concurrency cap set), R3 (snapshot rewrite cost), R5 (single api process), R6 (MCP mounting), R8 (hot reload on the Windows filesystem), R10 (share links on RLS datasets; `may_be_exempt=False` is ready for them).

## Backlog agreed at reviews

- **M6:** "View as" a specific user or role on a dashboard; share links and anonymous access never inherit an RLS exemption; per-dashboard access; named theme presets from branding.
- **M8:** bulk export and import of a `models/` folder; two-hop joins; the 10 million row check; a Playwright smoke test (could come earlier if wanted, see Q2).

## Questions for review

- **Q1. Field wells.** Fields are added to a visual's roles from a selector, not dragged from a field list. Is that acceptable for v1, or do you want drag-and-drop of fields before M5?
- **Q2. Playwright.** All browser checks so far are manual. Do you want a small Playwright smoke test (build a dashboard, save, reload, restore) added at the start of M5, run at the root and under /opsis, rather than waiting for M8?
- **Q3. Combo chart.** It has one value axis by design (ADR-021). Say if you need a second axis as an option despite the risk of misleading comparisons.
- **Q4. View state in the URL.** Should filter values, cross-filters and drill grains be kept in the URL so a filtered view can be bookmarked and shared? Small to add; natural to do with share links in M6.

## Exact next step

After review, start **M5 (MCP)**. Read the M5 section of BUILD_BRIEF.md first. In outline:

1. Mount the MCP server in the api under the base path (risk R6), with the streaming settings from section 3.1, and add `.mcp.json`.
2. Tools over the existing services: list datasets and models, read and validate a model, run a semantic query, list and read dashboards, validate a spec, write a dashboard as JSON Patch plus `base_version` (409 when stale), with every agent write recorded as an agent-authored version and in the audit log.
3. The dashboard SSE channel (`version_created`) so an open editor reloads the draft and offers Undo.
4. Verify with a real MCP client at the root and under /opsis through the `proxytest` profile.
