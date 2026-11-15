# Progress

Last updated: 2026-11-15 (end of Milestone 5)

## Milestone status

| Milestone | Status |
|---|---|
| M0 Plan | Done, reviewed |
| M1 Skeleton and modes | Done, reviewed, catch-up to the revised brief done |
| M2 Data ingestion | Done, reviewed |
| M3 Semantic layer and queries | Done, reviewed |
| M3.5 Timezones, conditional measures, model versions, benchmark | Done, reviewed |
| M4 Dashboards and visuals | Done, reviewed |
| M5 MCP | Done, awaiting review |
| M6 Auth and sharing | Not started |
| M7 In-app assistant | Not started |
| M8 More connectors and hardening | Not started |

## M5 MCP

Built (decisions in ADR-022):

- **From the M4 review, done first:**
  - **Playwright smoke tests** in `e2e/`. `npm run smoke` rebuilds the image, runs every test at http://localhost:8080, switches the stack to the sub-path with `compose.subpath.yml` and the `proxytest` profile, runs them again at http://localhost:8088/opsis/, and puts the stack back. No edit to `.env` is needed. CLAUDE.md now names this as the way to verify UI.
  - **Combo chart, optional right-hand axis:** a measure in Bars or Lines can be bound as `{"field": ..., "axis": "right"}`; the editor has a Left axis / Right axis selector per measure. Each axis is named after its measures, the legend marks right-axis series, and only the left axis draws grid lines.
- **MCP server** at `{BASE_PATH}/mcp` (Streamable HTTP, stateless, JSON responses) with 21 tools, 6 resources (a guide, the visual manifests, the four schemas) and the `build_dashboard` prompt. `.mcp.json` at the repository root points Claude Code at it.
- **Tools** (`opsis/mcp/tools.py`): `list_datasets`, `describe_dataset`, `list_visuals`, `get_spec_schema`, `list_connections`, `get_semantic_model`, `preview_dataset`, `run_query`, `profile_column`, `validate_model`, `patch_semantic_model`, `list_dashboards`, `get_dashboard`, `validate_spec`, `create_dashboard`, `patch_dashboard`, `list_versions`, `restore_version`, `publish_dashboard`, `trigger_refresh`, `get_job_status`.
- **Written for small models:** names instead of ids, one example input per tool, a ready-made example item per visual from the real fields of a dataset, lenient argument handling, and errors that carry a code, the problems by path, "Did you mean" suggestions and a "What to do" line. Details in ADR-022.
- **JSON Patch with `base_version`** for dashboards and models (`opsis/dashboards/patch.py`); a stale version is refused with 409 and instructions to re-read and retry.
- **Personal access tokens** (migration `0006`, `opsis/services/tokens.py`, the Access tokens page): shown once, stored hashed, scoped, expiring, revocable, last-used time. Required when `MCP_AUTH=token`.
- **Audit:** agent-made versions are marked `agent`; every write tool adds an `agent.<tool>` audit row with an argument summary and the version created.
- **Live updates:** `GET /api/dashboards/{id}/events` (server-sent events) and the editor notice "Updated by assistant. Undo". Unsaved work in the editor is never overwritten: the editor offers to load the new version instead.
- Validation messages for an unknown field now suggest the closest real names and list what is available (in the UI as well).

### How M5 was verified

- **Backend, automated:** 219 pytest tests pass (9 new in `tests/test_mcp.py`, all through the real HTTP endpoint in-process); `ruff check`, `ruff format --check` and `mypy --strict` pass. The new tests cover:
  - the catalogue: every tool has an example input that is valid for it; resources and the prompt; GET and DELETE answer 405;
  - an agent flow on a real Postgres source: discover, every `exampleItem` valid as it stands, create, patch, a stale `base_version` refused and recovered from, a wrong field name answered with the right one, a bad patch path, bad arguments, restore, publish, and the audit rows;
  - `run_query` equal to SQL on the source and capped at 1,000 rows; `profile_column` equal to SQL; a column name containing SQL treated as a name; model patch, conflict and invalid model; refresh and job status;
  - tokens: missing, made-up, revoked, expired and deactivated-owner tokens get 401; a token is stored only as a hash; scopes hide and block tools; a viewer with every scope still only gets viewer tools; two tokens act as their two owners; another origin is refused;
  - row-level security through the tools; JSON Patch operations and 16 error messages; the axis option; the event hub across threads; and that writes announce themselves.
- **Frontend, automated:** `tsc`, ESLint and 40 Vitest tests pass (one new, for the right-hand axis).
- **Browser and a real MCP client, automated** (`npm run smoke`, 3 Playwright tests, **passed at http://localhost:8080 and at http://localhost:8088/opsis/** after a full image rebuild):
  - `smoke.spec.ts`: build a dashboard with two visuals from a fresh CSV dataset, save, reload, remove a visual, undo, redo, save, see the diff, restore an old version, have the JSON tab refuse a wrong type with its path, publish, and open the published view. No script errors and no CSP violations.
  - `mcp.spec.ts`: the official MCP TypeScript client connects to `/mcp` (and to `/opsis/mcp` through nginx), lists tools and resources, gets the prompt, describes the dataset, runs a query with the right totals, and creates a dashboard whose `editUrl` matches the install, sub-path included. With the editor open on that dashboard, a patch with a stale version is refused, the agent re-reads and retries, and **three visuals appear in the editor without a reload** with the notice "Updated by assistant."; Undo returns to the earlier version; with unsaved work in the editor, an agent change does not overwrite it and "Load version N" brings it in. The event stream response is `text/event-stream` through the proxy.
  - The Access tokens page: a token is created, shown once, not shown after a reload, revoked and deleted.
- **By hand, as an agent:** I called the tools over HTTP myself with only their descriptions to go on: `describe_dataset` on the sample `gifts` data, then one `create_dashboard` call with three KPIs, a combo chart (amount as bars, gift count on the right axis), a line chart by month and channel, a table and two filters. It was accepted first time and renders correctly (checked from a screenshot). It is still in the dev stack as "Giving overview (built over MCP)".

Bugs found by this testing and fixed: the `title` argument of `create_dashboard` was ignored when the spec had its own title; a GET on `/mcp` opened a stream that never ended; `replace` on a missing key gave a message that did not say to use `add`.

Not verified:

- **A model-driven session through Claude Code's own MCP integration.** `.mcp.json` is new, so it was not loaded in the session that built this. The tools were driven by the official MCP client from a script, and by me over plain HTTP. Your test with Sonnet and Fable is the first real run of that kind.
- `MCP_AUTH=token` against the running container. Token handling is tested through the real endpoint in-process only, because the dev stack runs with `MCP_AUTH=none`.
- Claude Desktop through `mcp-remote`. The config in the README follows the brief but was not run.
- Long-lived event streams (hours), many editors at once, and what a real proxy does to an idle stream beyond the nginx test profile.
- The Proxmox LXC and everything else listed as unverified for earlier milestones.

### M3.5 benchmark, in one line

5 million rows on this laptop: typical queries 77 to 250 ms cold and 11 to 14 ms warm; 10 different uncached queries at once finished in 3.4 to 3.6 s with the health check staying under 105 ms; peak api memory 1.1 GB during queries and 264 MB during ingestion. Full tables are below.

## M4 Dashboards and visuals (reviewed)

Built in M4 (ADR-021): the dashboard tables and versions, two-phase spec validation, query derivation on the backend, nine visuals, the editor (grid, field wells, config panels, filters, undo and redo, history with diff and restore, JSON tab), viewing with filters, cross-filtering, drill, table toggle, themes and the "RLS bypassed" badge. Verified at the time by hand at both URLs; the same flow is now the Playwright smoke test.

## Review outcomes so far

- **M0:** keep psycopg; `Australia/Melbourne`, Monday weeks, financial years in v1 (ADR-012); Apache 2.0; deviations ADR-005, 006 and 007 approved.
- **M1:** brief replaced with the current version (sub-path hosting, section 3.1); ADR-013 superseded by ADR-016; `FISCAL_YEAR_LABEL` added; one-shot config check; placeholder secrets refused in server mode; `httpx2` kept.
- **M2:** keep the built-in schema form for connections; at the start of M4 prototype rjsf with a CSP-safe validator and decide then (ADR-018). `BIND_ADDRESS` stays `0.0.0.0` in the server template and is not a required value. Editors may see connections and their non-secret settings.
- **M3:** approved with M3.5 (timezones, conditional measures, model versions, value checks, benchmark; all done). Admins stay exempt from RLS by default, with a visible badge (done in M4), share links never exempt and "View as" in M6. Bulk model export and two-hop joins go to M8. Every milestone is verified at the root and under /opsis.
- **M4:** approved. Field selector is fine for v1 (drag-and-drop to the M8 backlog). Playwright smoke test at the start of M5, at both URLs, replacing manual browser checks from then on. Combo chart gets an optional right-hand axis per measure. Filter and drill state in the URL in M6 with share links. MCP tool descriptions and errors must work for a smaller model: anything Sonnet struggles with counts as a tool design issue.
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

**MCP and agent writes (M5)**
- `opsis/mcp/`: `tools.py` (the 21 tools and `call`), `server.py` (endpoint, authentication, resources, prompt), `guide.py` (instructions, guide and prompt text).
- `opsis/dashboards/patch.py` (JSON Patch), `opsis/services/tokens.py`, `opsis/services/events.py`, `opsis/api/tokens.py`. Migration `0006`.
- Frontend: `pages/Tokens.tsx`; `useDashboardEvents` in `lib/dashboard.ts`; the remote-change notice in `DashboardEditor`.
- `e2e/` (Playwright tests and helpers), `scripts/smoke.mjs`, `compose.subpath.yml`, `.mcp.json`.

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
- ADR-018: connection forms use a small built-in schema form. Visual config panels use rjsf with our own CSP-safe validator instead of ajv. shadcn/ui is still not added.
- ADR-019: a fourth spec file (`semantic-model.schema.json`); query values are SQL literals rendered by sqlglot, not bound parameters (approved); the model editor is still a plain text area.
- ADR-021: field wells are selectors, not drag-and-drop (accepted for v1); cross-filter and drill state live in the view, not in the spec; the chart palette and mark rules are fixed in code.
- ADR-022 (new, M5): the MCP endpoint is stateless with JSON responses, so it never streams and cannot push to a client; `share_dashboard` and `create_share_link` wait for M6; JSON Patch is our own code, not a library; personal access tokens work for MCP only; the smoke test covers build, save, reload, restore and publish, and does not sign in because there is no sign-in until M6.
- ADR-015: the task runner is npm scripts, not `tasks.py`; `httpx2` for the test client (approved).

## Open issues and notes

- **Row-level security and raw previews (see Q1).** The REST preview of a dataset (`GET /api/datasets/{id}/preview`, the Datasets page) shows raw rows to anyone with `datasets:edit`, including an editor to whom an RLS rule applies. That is M2 behaviour. The MCP tools refuse in that case; the REST endpoint does not yet.
- The MCP SDK resolved to version 2.3, a new major version. The server uses its low-level API. Expect to revisit on SDK upgrades.
- The live event hub is in the memory of the single api process. Two api processes would need Postgres LISTEN/NOTIFY (R5).
- The audit log has rows for every agent write but no screen to browse them yet (M6).
- The Access tokens link sits in the "Admin" group of the navigation, although every role can use it.
- A right-hand axis that shows counts can have decimal tick labels, because its ticks are aligned to the left axis.
- If a smoke run is killed half-way it can leave a `smoke_...` dataset and connection behind; a completed run removes what it made.
- `deploy/windows/README.md` and `deploy/proxmox/README.md` are still not written. The Claude Code and Claude Desktop set-up is in the main README for now.
- Dashboards have no per-dashboard permissions yet (M6). Cross-filter, drill and filter values are not in the URL (M6).
- Two people editing one dashboard: the second save is refused, and now the second editor is told as soon as the first one saves.
- Bundle size: main chunk about 760 kB (240 kB gzipped), ECharts 562 kB and Monaco 3.4 MB, both lazy.
- Joins are one hop (two-hop: M8). The query builder has one filter row.
- A server install is not usable until M6 (sign-in).
- Merge refresh does not remove rows deleted in the source. One job runs at a time. Datasets cannot be edited after creation.
- `cloudflared` and `ollama` images are on `latest` until M6 and M7.
- Risks still open: R2 (DuckDB memory in the api process), R3 (snapshot rewrite cost), R5 (single api process), R8 (hot reload on the Windows filesystem), R10 (share links on RLS datasets). R6 (mounting MCP in the api) is closed: it runs at the root and under the sub-path.

## Backlog agreed at reviews

- **M6:** "View as" a specific user or role; share links and anonymous access never inherit an RLS exemption; per-dashboard access; named theme presets from branding; filter, cross-filter and drill state in the URL; the `share_dashboard` and `create_share_link` tools; a screen for the audit log; the smoke test signs in.
- **M8:** bulk export and import of a `models/` folder; two-hop joins; the 10 million row check; drag-and-drop of fields into wells.
- **Later (brief):** OAuth for the MCP server so it can be a claude.ai connector.

## Questions for review

- **Q1. Raw previews and row-level security.** Should the REST dataset preview follow the same rule as the MCP tools, and refuse raw rows to a user an RLS rule applies to? I recommend yes. It changes M2 behaviour for editors on datasets with rules; admins are unaffected.
- **Q2. Tokens for the REST API.** Personal access tokens currently work for MCP only. Do you want them accepted by the REST API as well (for scripts), in M6 with the rest of sign-in?
- **Q3. Stateless MCP.** The endpoint answers each request with plain JSON and keeps no session, which is the most robust choice behind your proxy. The cost is no server-to-client notifications, which no tool uses. Say if you would rather have the streaming, stateful mode.
- **Q4. Sonnet results.** When you test with Sonnet, the most useful thing to send back is the tool call that went wrong and the error text it got. Those are quick to turn into a better description or message.

## Exact next step

After review, start **M6 (Auth and sharing)**. Read the M6 section of BUILD_BRIEF.md first. In outline:

1. Sign-in: `AUTH_MODE=local` (passwords with argon2, sessions in the `sessions` table, first-run admin) and `AUTH_MODE=full` (Google OIDC through Authlib), replacing the 401 in `get_principal`. Users page, roles, attributes for RLS.
2. Sharing: per-dashboard access, share links behind `FEATURE_PUBLIC_SHARE_LINKS` that never inherit an RLS exemption, "View as", view state in the URL.
3. The two sharing tools for MCP, tokens for REST if wanted (Q2), and a screen for the audit log.
4. Extend the Playwright tests to sign in, and run them at both URLs.
