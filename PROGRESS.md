# Progress

Last updated: 2026-11-14 (end of Milestone 3)

## Milestone status

| Milestone | Status |
|---|---|
| M0 Plan | Done, reviewed |
| M1 Skeleton and modes | Done, reviewed, catch-up to the revised brief done |
| M2 Data ingestion | Done, reviewed |
| M3 Semantic layer and queries | Done, awaiting review |
| M4 Dashboards and visuals | Not started |
| M5 MCP | Not started |
| M6 Auth and sharing | Not started |
| M7 In-app assistant | Not started |
| M8 More connectors and hardening | Not started |

## Review outcomes so far

- **M0:** keep psycopg; `Australia/Melbourne`, Monday weeks, financial years in v1 (ADR-012); Apache 2.0; deviations ADR-005, 006 and 007 approved.
- **M1:** brief replaced with the current version (sub-path hosting, section 3.1); ADR-013 superseded by ADR-016; `FISCAL_YEAR_LABEL` added; one-shot config check; placeholder secrets refused in server mode; `httpx2` kept.
- **M2:** keep the built-in schema form for connections; at the start of M4 prototype rjsf with a CSP-safe validator and decide then (ADR-018). `BIND_ADDRESS` stays `0.0.0.0` in the server template and is not a required value. Editors may see connections and their non-secret settings.
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
- ADR-018: connection forms use a small built-in schema form, not react-jsonschema-form; shadcn/ui not added yet. To be decided by a prototype at the start of M4.
- ADR-019: a fourth spec file (`semantic-model.schema.json`); query values are SQL literals rendered by sqlglot, not bound parameters; the model editor is a plain text area (Monaco arrives with the dashboard editor in M4).
- ADR-015: the task runner is npm scripts, not `tasks.py`; `httpx2` for the test client (approved).

## Open issues and notes

- **Timezones:** timestamps are grouped as stored. A Postgres `timestamptz` is stored in UTC, so "by day" on it means UTC days, not Melbourne days. Date columns are unaffected. See Q1.
- Joins are one hop from the query's dataset. A dimension two joins away cannot be used yet.
- No conditional measures (`sum(amount) where channel = 'online'`). A segment or a filter applies to the whole query.
- No model version history. Saving replaces the model; the audit log records who and when.
- No bulk import or export of a `models/` folder. See Q2.
- The query builder has one filter row. The API accepts any number.
- A server install is not usable until M6 (sign-in).
- Merge refresh does not remove rows deleted in the source. A full rebuild does.
- One job runs at a time per install. No cancel or progress for a running job.
- Datasets cannot be edited after creation.
- `cloudflared` and `ollama` images are on `latest` until M6 and M7.
- `deploy/proxmox/README.md` and `deploy/windows/README.md` are not written yet. No `.mcp.json` yet (M5).
- Risks still open: R2 (DuckDB memory in the api process; a per-query limit is now set), R3 (snapshot rewrite cost), R5 (single api process), R6 (MCP mounting), R8 (hot reload on the Windows filesystem), R10 (share links on RLS datasets). R4 (model expressions as an injection surface) is addressed by the allow-list and its tests.

## Questions for review

- **Q1. Timezones.** Should timestamp columns that came from a timezone-aware source be converted to `WORKSPACE_TIMEZONE` before grouping by day, week or month? I recommend yes, done in M4: record at ingestion which columns were timezone-aware, and convert those in the compiler. Timestamps without a zone (MySQL `DATETIME`, CSV) would stay as written.
- **Q2. Models in version control.** Do you want a command that exports every model to a `models/` folder and imports from it, so models can live in git? It is small; I left it out because per-model export and import cover the brief.
- **Q3. Admins and RLS.** Admins are exempt from row-level security unless a rule lists `admin`. Is that the default you want?

## Exact next step

After review, start **M4 (Dashboards and visuals)**:

1. Prototype rjsf with a CSP-safe validator (and check ajv for spec validation under the CSP), then decide ADR-018 and record it.
2. Backend: `dashboards` and `dashboard_versions` tables; spec validation in two phases (schema, then visual manifests, model fields and grid); save creates a version; draft and published pointers; restore; derive the semantic query for an item from its bindings plus dashboard filters; `POST /api/dashboards/{id}/items/{item}/data`; manifests served from `/app/visuals`.
3. Frontend: react-grid-layout editor with drag and resize, field wells from manifest roles, config panel from `configSchema`, Monaco JSON tab, undo and redo, version history with diff and restore; the v1 visuals (table, KPI, bar, line, area, combo, pie, scatter, text) on ECharts; filter controls; cross-filtering; theming with an accessible palette.
4. Acceptance: build a six-visual dashboard by hand, save, reload, restore an old version, and see invalid JSON in the spec tab rejected with a clear path.
