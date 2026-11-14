# Decisions

Short ADR entries. Newest at the bottom. "Brief" means `BUILD_BRIEF.md`. An entry marked **Deviation** changes something the brief specified.

## ADR-001: Parquet snapshots queried by in-process DuckDB
- **Status:** accepted (from the brief), M0
- **Decision:** each refresh writes an immutable snapshot directory; Postgres holds a pointer to the current one; DuckDB reads the Parquet files in-process behind a `QueryEngine` protocol.
- **Why:** no DuckDB single-writer lock, atomic swaps, refresh history for free, works on Windows volumes.
- **Cost:** every refresh rewrites the whole dataset, including incremental ones (see risk R3 in `PROGRESS.md`).

## ADR-002: Postgres table as the job queue
- **Status:** accepted (from the brief), M0
- **Decision:** jobs are rows claimed with `SELECT ... FOR UPDATE SKIP LOCKED`. The inline runner and the worker share one `run_job()` function.
- **Why:** no Redis, same behaviour in both modes.

## ADR-003: JSON Schema draft 2020-12, shared definitions by cross-file `$ref`
- **Status:** accepted, M0
- **Decision:** `semantic-query.schema.json` owns the shared definitions (`identifier`, `fieldRef`, `grain`, `scalar`, `range`, `filter`, `sort`). `dashboard.schema.json` references them with relative `$ref`. Each schema has an `$id` under `https://opsis.invalid/spec/`. The ids are identifiers only and are never fetched.
- **Why:** one definition of a field reference, a range and a filter, so the UI, the agent and the compiler cannot drift apart.
- **Cost:** both validators must preload all three schemas (a `referencing.Registry` in Python, `ajv.addSchema` in the frontend). Verified in Python in M0.

## ADR-004: Two-phase spec validation
- **Status:** accepted, M0
- **Decision:** JSON Schema checks structure. A second semantic pass in the backend checks what a schema cannot: unique ids, the visual exists, bindings satisfy manifest roles, `config` matches the manifest `configSchema`, fields exist in the model, items fit the grid, filter `targets` exist. Both phases report JSON pointer paths in one error format.
- **Why:** keeps the schema small and readable, and gives the agent precise paths to self-correct.

## ADR-005: A binding is a field reference or `{ field, grain }`
- **Status:** accepted, M0 (extends the brief)
- **Decision:** `bindings.<role>` is an array whose entries are either `"gifts.fund_name"` or `{ "field": "gifts.gift_date", "grain": "month" }`.
- **Why:** the brief's bindings carry no time grain, and a line chart needs one. A time dimension bound without a grain uses the coarsest grain that gives a sensible number of points (decided in M4). Drill-down changes the grain as view state, not in the spec.

## ADR-006: Date filtering uses `in_range`, and `timeDimension.grain` is optional
- **Status:** accepted, M0 (extends the brief)
- **Decision:** the filter operator `in_range` takes a `range` (relative or absolute) instead of `values`. `timeDimension` needs a `grain`, a `range`, or both.
- **Why:** a dashboard date filter must apply to items that do not group by date (a KPI), and to date fields other than the item's own time dimension.

## ADR-007: `Connector.read` takes an optional `options` argument and connectors have `close()`
- **Status:** accepted, M0. **Deviation.**
- **Decision:** `read(stream, mode, state, options: ReadOptions | None = None)`, where `ReadOptions` carries the selected columns, cursor field, primary key and batch size. `close()` releases connections.
- **Why:** the brief's signature has nowhere to pass the cursor column or column selection, which are per-dataset choices, not connection config and not cursor state.

## ADR-008: One logical column type enum
- **Status:** accepted, M0
- **Decision:** `ColumnType` (string, integer, float, decimal, boolean, date, timestamp, json) is used by connector discovery, snapshot metadata and starter-model generation. Arrow types carry the physical detail.
- **Why:** the UI, the semantic model and the agent need a small stable vocabulary, not source-specific types.

## ADR-009: The engine runs compiled SQL only
- **Status:** accepted, M0
- **Decision:** `QueryEngine.execute` takes a `CompiledQuery` (SQL, parameters, snapshot refs, row limit, timeout). The semantic compiler, RLS and the cache sit outside the engine. The engine exposes its sqlglot `dialect` so the compiler can target it.
- **Why:** swapping the engine later means implementing four methods, with no change to the API, the compiler rules or the cache.

## ADR-010: A single `api` process in v1
- **Status:** accepted, M0
- **Decision:** `api` runs one Uvicorn process. Request handlers use sync SQLAlchemy sessions in the threadpool; DuckDB queries run in threads; SSE uses an in-process publish/subscribe.
- **Why:** the result cache and the SSE channels are in-process, and agent writes (MCP, assistant) happen in `api`, so live updates need no cross-process bus. The worker only reports job status, which the UI polls.
- **Revisit when:** more than one api process is needed. The likely answer is Postgres `LISTEN/NOTIFY`.

## ADR-011: Verification tooling runs in containers, tasks are npm scripts
- **Status:** accepted, M0, revised M1
- **Decision:** the dev machine has Git, Docker and Node, but no Python or uv. Backend lint, type checks and tests run in the `dev` image target through the `tools` service in `compose.dev.yml`. The task runner is the root `package.json` (no dependencies), not `tasks.py`, because Node is on the host and Python is not. `uv.lock` is generated in a container (`npm run lock:backend`).
- **Why:** nothing extra to install on the laptop, and the same commands work on the server.

## ADR-012: Financial-year calendar
- **Status:** accepted, M1 (requested in the M0 review)
- **Decision:**
  - Three workspace settings: `WORKSPACE_TIMEZONE` (default `Australia/Melbourne`), `WEEK_START` (default `monday`) and `FISCAL_YEAR_START_MONTH` (default `7`). They are exposed in `/api/config` under `calendar`.
  - Three extra grains: `fiscal_year`, `fiscal_quarter`, `fiscal_month`. Every grain, fiscal or not, returns the **start date of its period**, so results sort correctly and the engine needs no label logic. `fiscal_month` has the same buckets as `month`; it differs only in its label (period 1 to 12).
  - A fiscal year is named after the calendar year it **ends** in: with a July start, FY2027 is 1 July 2026 to 30 June 2027. With a January start, fiscal and calendar grains are identical.
  - Relative ranges gain `this_fiscal_year`, `last_fiscal_year`, `this_fiscal_quarter`, `last_fiscal_quarter`, `fiscal_year_to_date`, `fiscal_quarter_to_date` and `last_N_fiscal_years` / `last_N_fiscal_quarters`.
  - Meaning of relative ranges, for all units: `this_<unit>` is the whole current period (including its future part); `last_<unit>` is the previous complete period; `last_N_<units>` is N periods ending with the current one, up to today; `<unit>_to_date` runs from the start of the current period to today.
  - The rules live in one pure module, `opsis/semantic/calendar.py`, with tests. The M3 compiler resolves relative ranges to literal dates with it and emits date truncation that matches `period_start`. Labels such as "FY2027" or "Q2 FY2027" are formatted in the frontend from the period start date plus the `calendar` config.
- **Why:** one definition of the calendar for the compiler, the UI and the agent. Returning dates keeps the engine contract simple.
- **Not included:** 4-4-5 or week-based fiscal calendars, and a fiscal year that does not start on the first of a month.

## ADR-013: The api container is published on loopback in server mode
- **Status:** accepted, M1. **Deviation.**
- **Decision:** `compose.yml` always publishes `api` on `${BIND_ADDRESS}:${HTTP_PORT}`. The server template sets `BIND_ADDRESS=127.0.0.1`, so the api is reachable only from the server itself and Caddy remains the only public entry. The brief says server mode publishes only Caddy's ports.
- **Why:** Compose cannot drop a `ports` entry based on `.env`, and the brief requires one `compose.yml` with all differences in `.env`. A loopback publish exposes nothing and is handy for diagnostics.
- **Guard:** `AUTH_MODE=none` and `MCP_AUTH=none` are rejected in server mode regardless of the bind address.

## ADR-014: What fails fast in M1, and what waits
- **Status:** accepted, M1
- **Decision:** startup rejects: `AUTH_MODE=none` or `MCP_AUTH=none` off loopback or in server mode; public share links without sign-in or outside server mode; a non-https `PUBLIC_BASE_URL` in server mode (localhost excepted); `JOB_RUNNER=worker` without the `server` Compose profile; malformed URL, timezone, week start or fiscal month. All problems are reported together.
- **Deferred on purpose:** checks that need credentials (`GOOGLE_CLIENT_ID` for `AUTH_MODE=full`, `ANTHROPIC_API_KEY` for the assistant, SMTP for email, `ENCRYPTION_KEY`) arrive with the milestone that uses them. Until then the server template boots as shipped, which the M1 acceptance test requires.
- **Open point for M6:** whether a missing Google client id should stop startup or only disable the Google button.

## ADR-015: Smaller M1 choices
- **Status:** accepted, M1
- **Migrations** run in the `api` entrypoint before Uvicorn starts. The worker waits for `api` to be healthy, so only one process migrates.
- **Entrypoint** is `python -m opsis <api|worker|migrate|check-config>`, in Python for cross-platform parity.
- **Image tags:** base images are pinned to a release line (`python:3.12-slim-bookworm`, `node:24-bookworm-slim`, `postgres:16-bookworm`, `caddy:2-alpine`, uv `0.13.8`); Python and npm packages are pinned exactly by lockfiles. `cloudflared` and `ollama` use `latest` until the milestones that exercise them (M6, M7) pin a tested version.
- **Test HTTP client:** the dev dependency is `httpx2`, which the installed Starlette test client now asks for in place of `httpx`. The brief's `httpx` remains the choice for runtime HTTP calls when they arrive.
- **Frontend:** Tailwind v4 through its Vite plugin. shadcn/ui components and the other listed libraries are added when the first screen needs them (M2 forms), not before.
- **Errors:** every API error is `{"error": {"code", "message", "details"}}`; validation details carry JSON pointer paths.
