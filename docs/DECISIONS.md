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
- **Status:** superseded by ADR-016 (M1 review). The brief now says the api publishes on `BIND_ADDRESS` in both modes, and on the server that is the LXC's LAN IP. The text below is kept for history only.
- **Decision:** `compose.yml` always publishes `api` on `${BIND_ADDRESS}:${HTTP_PORT}`. The server template sets `BIND_ADDRESS=127.0.0.1`, so the api is reachable only from the server itself and Caddy remains the only public entry. The brief says server mode publishes only Caddy's ports.
- **Why:** Compose cannot drop a `ports` entry based on `.env`, and the brief requires one `compose.yml` with all differences in `.env`. A loopback publish exposes nothing and is handy for diagnostics.
- **Guard:** `AUTH_MODE=none` and `MCP_AUTH=none` are rejected in server mode regardless of the bind address.

## ADR-014: What fails fast
- **Status:** accepted, M1; revised at the M1 review
- **Decision:** startup rejects:
  - `AUTH_MODE=none` or `MCP_AUTH=none` off loopback or in server mode;
  - public share links without sign-in or outside server mode;
  - a non-https `PUBLIC_BASE_URL` in server mode (localhost excepted);
  - an https `PUBLIC_BASE_URL` with empty `TRUSTED_PROXIES`;
  - `JOB_RUNNER=worker` without the `server` Compose profile;
  - in server mode, a placeholder or short `POSTGRES_PASSWORD` (under 12 characters, or containing words such as "change" or "password") and a missing `ENCRYPTION_KEY`;
  - an `ENCRYPTION_KEY` that is not a valid Fernet key, in any mode;
  - malformed URL, base path, proxy list, timezone, week start, fiscal month or fiscal label.
- All problems are reported together. Secret values are never echoed in the message.
- **How it fails:** a one-shot `config-check` service runs first; `api` and `worker` depend on it with `condition: service_completed_successfully`. A bad `.env` therefore stops `docker compose up` once, and the reason is in `docker compose logs config-check`. There is no restart loop.
- **Consequence:** the server template no longer boots as shipped. That is intended: it has three values marked CHANGE.
- **Local mode:** an empty `ENCRYPTION_KEY` is allowed. A key is generated on first use and stored in the `opsisdata` volume (`/data/secrets/encryption.key`), so a laptop install needs no setup. Losing that volume loses the key along with the data it protects.
- **Still to come:** the same placeholder rule will apply to each new secret as it is introduced (`ADMIN_INITIAL_PASSWORD` and `GOOGLE_CLIENT_SECRET` in M6, `ANTHROPIC_API_KEY` in M7).

## ADR-015: Smaller M1 choices
- **Status:** accepted, M1
- **Migrations** run in the `api` entrypoint before Uvicorn starts. The worker waits for `api` to be healthy, so only one process migrates.
- **Entrypoint** is `python -m opsis <api|worker|migrate|check-config>`, in Python for cross-platform parity.
- **Image tags:** base images are pinned to a release line (`python:3.12-slim-bookworm`, `node:24-bookworm-slim`, `postgres:16-bookworm`, `caddy:2-alpine`, uv `0.13.8`); Python and npm packages are pinned exactly by lockfiles. `cloudflared` and `ollama` use `latest` until the milestones that exercise them (M6, M7) pin a tested version.
- **Test HTTP client:** the dev dependency is `httpx2`, which the installed Starlette test client now asks for in place of `httpx`. The brief's `httpx` remains the choice for runtime HTTP calls when they arrive.
- **Frontend:** Tailwind v4 through its Vite plugin. shadcn/ui components and the other listed libraries are added when the first screen needs them (M2 forms), not before.
- **Errors:** every API error is `{"error": {"code", "message", "details"}}`; validation details carry JSON pointer paths.

## ADR-016: Sub-path hosting behind an existing reverse proxy
- **Status:** accepted, M1 catch-up (brief section 3.1). Supersedes ADR-013.
- **One setting:** `PUBLIC_BASE_URL` may carry a path. `Settings.base_path` is that path (`""` locally, `/opsis` on the server). Nothing else stores a URL or prefix.
- **The app owns the prefix:** every router is included with `prefix=base_path`, static assets are mounted at `{base}/assets`, and the SPA fallback is registered at `{base}/{path}`. A request outside the prefix matches no route and gets a JSON 404. `GET {base}` redirects to `{base}/`. I chose explicit prefixes over an ASGI `root_path` or a sub-application mount because the proxy forwards the path unchanged and there is then exactly one place where the prefix is applied.
- **Runtime frontend base:** Vite builds with `base: "./"`. The backend serves `index.html` with an injected `<base href="{base}/">` and a `<script id="opsis-config" type="application/json">` block. `src/lib/runtime.ts` reads it; the API client, React Router `basename` and (later) SSE URLs all go through it. The block is data, so the CSP stays `script-src 'self'` with no inline script.
- **Cookies:** names are prefixed `opsis_` and `Path` is the base path (`/` locally). `Secure` follows the scheme of `PUBLIC_BASE_URL`, not the request, so it cannot be downgraded by a missing forwarded header.
- **CSRF, brought forward from M6:** an `opsis_csrf` cookie is issued on first contact, and every POST, PUT, PATCH or DELETE under `{base}/api/` must repeat it in `X-CSRF-Token`. This is needed now because M2 adds write endpoints, and with `AUTH_MODE=none` any web page could otherwise post a form to `localhost:8080`. Requests with a Bearer token are exempt (browsers never attach those on their own).
- **Forwarded headers:** Uvicorn runs with proxy headers only when `TRUSTED_PROXIES` is set, and honours them only from those addresses or CIDR ranges.
- **HSTS:** never sent by the app. It applies to the whole domain, so it belongs to the proxy.
- **CORS:** none. The UI is same-origin.
- **Streaming:** `opsis/api/sse.py` builds SSE responses with `X-Accel-Buffering: no` and a keep-alive comment every `SSE_KEEPALIVE_SECONDS` (default 15). The first real stream arrives in M5.
- **Publishing:** `api` publishes `${BIND_ADDRESS}:${HTTP_PORT}` in both modes. The server template defaults to `0.0.0.0` with a note to set the LXC's LAN IP and firewall the port to the proxy.
- **Profiles:** `server` adds only the worker; `caddy` is its own optional profile; `proxytest` runs nginx at `http://localhost:8088` with another app at `/` and Opsis at `/opsis/`. `deploy/proxytest/nginx.conf` is the reference for the real proxy.
- **Healthcheck:** `python -m opsis healthcheck` calls `{base}/healthz`.
- **Several cookies with one name (found in M2 testing):** browsers do not separate cookies by port, so a laptop that has run Opsis at `localhost:8080/` and at `localhost:8088/opsis/` sends two `opsis_csrf` cookies. The CSRF check therefore accepts a header that matches any of them (`cookies.cookie_values`). The session cookie lookup in M6 must do the same.

## ADR-017: Data ingestion choices
- **Status:** accepted, M2
- **Connector ids:** `file` (CSV and Excel upload), `postgres`, `mysql`. The brief's layout calls the first folder `csv/`; `file` is used because it also reads Excel.
- **Registry:** a built-in connector is a package under `opsis/connectors/` that exports `CONNECTOR`. Drop-in connectors live in `plugins/connectors/<id>/__init__.py` (copied to `/app/plugins`), and installed packages can use the `opsis.connectors` entry point. A broken plugin is logged and skipped.
- **CSV reading uses DuckDB**, with dialect and type detection over the whole file, so a column that turns into text on row 90,000 does not break the load. Excel uses openpyxl; a column with mixed value types is stored as text.
- **SQL connectors** share `connectors/sql_base.py`. Table and column names come only from the connector's own discovery, are quoted per database, and cursor values are bound parameters. Sessions are read-only. Rows are streamed with a server-side cursor.
- **Type mapping:** eight logical types (ADR-008). Decimals keep their scale (`decimal128(38, s)`); a numeric with no declared scale is stored as float. Timestamps with a zone are stored as UTC. JSON and unknown types are stored as text.
- **Row estimates** are shown only when trustworthy: exact for files, the planner estimate for Postgres when it has one, never for MySQL (its figure was 0 for a 5,000 row table in testing).
- **Snapshots:** see `docs/ARCHITECTURE.md` for full, append and merge. Append hard-links the previous files, so it costs no extra disk for old rows. A source whose columns changed fails an incremental refresh with a message asking for a full rebuild; the current data stays in place.
- **One transaction:** the refresh handler does not commit. `run_one` commits the snapshot row, the dataset pointer, the cursor state and the job status together. Pruning runs after that commit and cannot fail the job.
- **Runner recovery:** v1 has one job runner per install. At start it marks any job still "running" as failed ("Interrupted by a restart") and deletes half-built `.tmp` snapshot directories.
- **Retention:** `SNAPSHOT_RETENTION` (default 3, minimum 2) applies to every dataset. A per-dataset setting can come with the scheduling UI in M8.
- **Uploads** go to `/data/uploads/<id>/<name>`, limited by `MAX_UPLOAD_MB` and to `.csv .tsv .txt .xlsx .xlsm`. File names are flattened to a safe name, and the connector refuses any path outside the uploads folder. An upload is deleted with its connection. Uploads from an abandoned form are not cleaned up yet.
- **Permissions:** admins manage connections and uploads (`connections:manage`). Editors can list connections and tables and manage datasets (`datasets:edit`). Per-dataset access control arrives in M6.
- **Sample data:** the `sample` Compose profile starts a MySQL 8.4 container with `donors` and `gifts` tables. It is a demo and test source only, with a published throwaway password; MySQL itself is not a dependency of Opsis.

## ADR-018: Connection forms use a small built-in schema form, not react-jsonschema-form
- **Status:** accepted for M2. **Deviation**, to revisit in M4.
- **Decision:** `frontend/src/components/SchemaForm.tsx` renders a connector's `config_schema()` (text, number, boolean, enum, secret and upload fields). Validation is done by the backend with `jsonschema`, which returns one message per JSON path, and the form shows each under its field.
- **Why:** rjsf validates in the browser with ajv, which compiles validators using `new Function`. The brief requires a strict CSP with no inline or evaluated script on a domain shared with other apps, and that blocks ajv's compiler. The alternatives were to weaken the CSP with `unsafe-eval` or to precompile validators at build time, which does not work for schemas that arrive at runtime from plugins.
- **Cost:** only flat schemas are supported. That covers connectors. Visual config panels in M4 may need nested objects and arrays.
- **To decide in M4:** keep extending the built-in form, or use rjsf with a validator that does not need `eval` (for example a non-compiling JSON Schema validator behind rjsf's validator interface). The same question applies to validating dashboard specs in the browser with ajv.
- **Also deferred:** shadcn/ui. The M2 screens use a handful of Tailwind components in `components/ui.tsx`. shadcn/ui is worth adding when dialogs, menus and popovers are needed (the M4 editor).
- **M2 review:** keep the built-in form for connections. At the start of M4, prototype rjsf with a validator that works under the strict CSP, then decide.

## ADR-019: Semantic layer and query compiler
- **Status:** accepted, M3

**Models**
- One model per dataset, described by `spec/semantic-model.schema.json` (a fourth spec file). Stored as JSON, not JSONB, because JSONB re-sorts keys and a model must keep its author's order in field lists and YAML.
- A starter model is generated when a dataset is first refreshed: a dimension per column, a row count, and a sum for each numeric column that is not an identifier. While nobody has edited it, it is regenerated on each refresh so it follows the source's columns. The first save makes it "edited" and it is never touched again.
- Validation is two-phase like dashboards (ADR-004): the schema, then columns, expressions, measure references and cycles, joins and RLS rules. Every problem has a JSON pointer path.
- YAML uses the YAML 1.2 rule for booleans (only `true` and `false`). Under YAML 1.1 the `on:` key of a join is read as `True`.
- Not built: model version history, and a command to sync a whole `models/` folder. Export and import are per model, through the API and the UI.

**Expressions (risk R4)**
- Dimension expressions, segment filters, RLS filters and measure formulas are parsed with sqlglot and every node is checked against an allow-list in `opsis/semantic/expressions.py`: columns of the dataset, literals, operators, CASE, CAST to basic types, and a fixed list of scalar functions. Subqueries, table functions, aggregates, window functions, parameters and unknown functions are rejected by name.
- Non-deterministic functions (`now()`, `current_date`, `random()`) are not allowed, so a cached result is always the right result. Relative dates come from query ranges instead.
- A measure formula may only combine other measures (`{total} / NULLIF({count}, 0)`). It cannot contain a column or an aggregate, so every aggregate in a query comes from a declared base measure.

**Compiler**
- Output is one SELECT built as a sqlglot tree. Field references are looked up in the model; values become literals in the tree; sqlglot renders the text. Values are literals, not bound parameters, so the SQL text alone identifies the result (see the cache). This narrows the M0 note that said "values as parameters".
- Every grain returns the start date of its period (ADR-012). The SQL truncation is tested against `calendar.period_start` for every grain, both week starts and six fiscal start months.
- Joins: only joins declared in the base dataset's model, one hop, and only `many_to_one` or `one_to_one`. Dimensions may come from the joined dataset; measures must come from the base dataset. A `one_to_many` join is refused because it would double-count.
- Text matching (`contains`, `starts_with`, `ends_with`) is case-insensitive and literal: `%` and `_` in the value are ordinary characters.
- Filters on measures become HAVING. Decimal results are sent to the browser as JSON numbers; exact decimals stay in the snapshot.
- A query's row limit is capped by `QUERY_MAX_ROWS` (default 10,000); one extra row is fetched to report `truncated`.

**Row-level security**
- A dataset's rules are applied inside the subquery that reads its snapshot, so no filter, join or segment can widen them. On a joined dataset this hides that dataset's rows (the joined fields become null); it does not remove rows of the base dataset.
- Rules that apply to the user's role are ANDed. The default roles are viewer and editor; admins are exempt unless a rule lists them.
- A rule whose user attribute is missing, empty, or not a text, number or boolean (or list of those) denies every row. A list is only valid after `IN`.
- A rule that fails to compile blocks queries on that dataset for the roles it applies to. It never fails open.
- User values are substituted as literals in the tree. A value containing SQL is just a string that matches nothing.
- Admins can run a query as another role and attribute set (`previewAs`) to test rules. No other role can.

**Engine and cache**
- `DuckDBEngine` opens an in-memory connection per query, with a memory limit, a thread cap, a temp directory, extension auto-install and auto-load switched off, and a watchdog timer that interrupts the query at `QUERY_TIMEOUT_SECONDS`.
- `ResultCache` is an LRU per process (`QUERY_CACHE_ENTRIES`, default 200) keyed by a hash of the SQL. The SQL contains the snapshot path, the user's RLS values and relative dates resolved to real dates, so a refresh, a different user or a new day each produce a different key without any invalidation logic.

**Other**
- DuckDB treats column names case-insensitively, so columns that differ only by case are stored under distinct names at ingestion (`Fund`, `fund_2`).
- Reading models and running queries needs `dashboards:edit`; saving a model needs `models:edit`. Viewers will query through published dashboards (M4), not through `/api/query`.
- **Known limit, timezones:** timestamps are grouped as stored. A `timestamptz` from Postgres is stored in UTC, so "by day" means UTC days, not `WORKSPACE_TIMEZONE` days. Date columns are unaffected. See the question in `PROGRESS.md`.
