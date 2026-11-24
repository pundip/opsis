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
- **Decided in M4:** visual config panels use rjsf (`@rjsf/core` 6) with our own validator, `frontend/src/lib/schemaValidator.ts`, built on `@cfworker/json-schema`, which interprets a schema instead of compiling it. The prototype and the finished editor run under the strict CSP with no violations. ajv is not used anywhere in the browser: rjsf's own `@rjsf/validator-ajv8` import is aliased to our validator in `vite.config.ts` so it is never bundled. Connection forms keep the built-in `SchemaForm` (flat schemas, secret and upload fields, errors from the backend). Dashboard specs are validated by the backend only (`POST /api/dashboards/validate`); the JSON tab gets inline hints from Monaco's JSON worker using the same schema files.
- **Still deferred:** shadcn/ui. The editor is built from the existing Tailwind components; no dialog or popover library was needed.

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
- **Timezones:** resolved in ADR-020.

## ADR-020: M3.5 additions to the semantic layer
- **Status:** accepted, M3.5 (from the M3 review)

**Timezones**
- A source value that identifies an instant (Postgres `timestamptz`, a CSV timestamp written with an offset) is stored as a UTC instant: the Parquet column is `timestamp[us, tz=UTC]` and the snapshot's column list carries `"tz": "UTC"`.
- A timestamp without a zone is wall-clock time and is taken to be in `WORKSPACE_TIMEZONE`. A model can override that per dimension with `timezone: <IANA name>` (for example `timezone: UTC` for Salesforce datetimes that arrive without an offset).
- The compiler converts both kinds to workspace wall-clock time in `dimension_sql`, before anything else uses the value, so grains, date ranges, filters and plain selection all agree. Dates are never shifted.
- The conversion is explicit in the SQL (`TIMEZONE('Australia/Melbourne', col)`), the engine's session timezone is pinned to UTC, and the zone name is part of the SQL text, so it is part of the cache key.
- Datasets refreshed before this change stored `timestamptz` as naive UTC. They convert correctly after their next full refresh. An incremental refresh of such a dataset reports "the columns have changed, run a full refresh".
- MySQL `DATETIME` and `TIMESTAMP` are both read as wall-clock time. Set `timezone` on the dimension if the server keeps them in UTC.

**Conditional measures**
- A measure with `agg` may have a `filter`: a boolean expression over the dataset's columns, in which a segment can be used by name in braces (`{large} AND region = 'VIC'`). It compiles to `agg(...) FILTER (WHERE ...)` and goes through the same allow-list as every other model expression. Formula measures cannot have a filter; they combine measures that already do.

**Model version history**
- `semantic_model_versions` holds an immutable row per save: definition, version number, author type (user, agent or system), author, optional message. `semantic_models` keeps the current definition and its version number.
- Restore copies an old version into a new one after validating it against today's columns. History is never rewritten.
- `PUT /api/models/{dataset}` accepts `baseVersion`. A stale value returns 409 `version_conflict`. This is what the agent will use in M5; the UI sends it too.
- Starter models are versions as well: version 1 when generated, and a new version only when a refresh actually changes the generated model.

**Values in queries**
- sqlglot literals are kept (not bound parameters). Every value is checked in one place, `compiler._value`, before it becomes a literal: numbers must be finite `int` or `float` (not strings, booleans, NaN or infinity, and within 64 bits); dates must match `YYYY-MM-DD` exactly and be real dates; booleans must be booleans; text is capped at 1,000 characters and may not contain a null byte. The query schema also caps text at 1,000 characters. RLS attribute values get the same checks and deny when they fail.

**Concurrency**
- Query endpoints are plain `def` functions, so FastAPI runs them in its worker thread pool and DuckDB never runs on the event loop. `QueryRuntime` holds a semaphore of `QUERY_MAX_CONCURRENT` slots (default 4); other queries wait, and get 503 `busy` if they wait longer than the query timeout. Cached results need no slot.
- Worst-case query memory is therefore about `QUERY_MAX_CONCURRENT` times `QUERY_MEMORY_LIMIT_MB`. Measured figures are in `PROGRESS.md`.

**RLS exemptions (decided at the M3 review)**
- Admins stay exempt by default. The compiler reports when an exemption applied (`rlsBypassed` in every query result) and the UI shows an "RLS bypassed" badge.
- `UserContext.may_be_exempt = False` makes every rule apply whatever its role list says. Share links and any anonymous access will use it (M6), so they never inherit an owner's exemption.
- "View as" a specific user or role on a dashboard is planned for M6.

## ADR-021: Dashboards, the editor and the v1 visuals
- **Status:** accepted, M4

**Storage and versions**
- `dashboards` holds the title, the latest version number and the published version number. `dashboard_versions` holds one immutable row per save: the full spec, author type (user, agent or system), author and an optional note. Specs are stored as JSON, not JSONB, so key order survives (same reason as models, ADR-019).
- The draft is the latest version. Published is a pointer to one version. Restore copies an old version into a new one after validating it against today's models; history is never rewritten.
- `PUT /api/dashboards/{id}` takes the full spec and `baseVersion`. A stale value returns 409 `version_conflict`. JSON Patch writes for the agent arrive with MCP in M5.

**Validation**
- Two phases (ADR-004). Phase one is `spec/dashboard.schema.json`. Phase two checks unique ids, that the visual exists, that bindings satisfy the manifest's roles (count and field kind), that `config` matches the manifest's `configSchema`, that every field is reachable from the item's dataset, that sorts and filters refer to real fields, and that items fit the grid. Every problem carries a JSON pointer path, for example `/items/0/bindings/value/0`.
- Invalid specs are never stored. The JSON tab applies nothing unless the whole document is valid.

**Queries**
- The browser never sends a query for a dashboard item. It sends the item id and the view state (filter values, cross-filter selections, drill grains) to `POST /api/dashboards/{id}/data`, and the backend derives the semantic query from the stored spec. A viewer can therefore only ask the questions the dashboard author defined.
- Editors may send an unsaved spec inline (`source: "inline"`) so the editor can preview changes; it is validated the same way and needs `dashboards:edit`. Editors also get the derived query back for the "show query" use; viewers do not.
- Viewers read the published version only. Row-level security applies as in ADR-019, and `rlsBypassed` is passed through so each tile shows an "RLS bypassed" badge when an exemption applied.
- Filter option lists come from `POST /api/dashboards/{id}/filter-options`, which runs a distinct-values query on the filter's field under the same RLS.

**Filters, cross-filtering and drill-down**
- Filter controls: single select, multi-select, date range (relative ranges including financial years, or fixed dates) and text search. A filter applies to every item whose dataset can reach its field, or to the items listed in `targets`.
- Clicking a mark on a chart, or a row value in a table, sets a cross-filter on that field for the other items. It is view state, not part of the spec, shown as a removable chip. A dashboard can switch it off (`interactions.crossFilter`).
- Drill-down through time grains is a per-tile grain selector (year, quarter, month, week, day, and the fiscal grains when the binding uses them). It is also view state.
- KPI comparison (`previous_period` or `previous_year`) runs a second query with the date range shifted.

**Visuals**
- Each visual is a folder in `frontend/src/visuals/<id>/` with `manifest.json` (roles, `configSchema`, minimum size) and `Render.tsx`. The frontend finds them with `import.meta.glob`; the backend reads the same manifests from `VISUALS_DIR` to validate specs. v1: table, KPI, bar, line, area, combo, pie or donut, scatter, text.
- Charts use ECharts 6 with the canvas renderer, loaded as a lazy chunk. Text is a small safe Markdown subset rendered as React elements (no HTML pass-through; links limited to http, https and mailto).
- Chart rules, fixed in code rather than left to each dashboard: an eight-colour categorical palette checked for colour-blind separation in light and dark; colours assigned by series name in a fixed order so a filter never repaints the survivors; a ninth and later series folded into "Other"; bars at most 24px wide with a rounded data end; 2px lines; a legend whenever there are two or more series; text in ink colours, never the series colour; every chart can be switched to a table of the same data.
- The combo chart draws bars and lines on one value axis by default. **Changed at the M4 review:** a measure in the bars or lines role may ask for the right-hand axis with `{"field": ..., "axis": "right"}` (the usual case is gift amount as bars against gift count as a line, which is flat on a shared axis). Roles opt in with `"axis": true` in the manifest, so no other visual accepts it. To keep two scales readable, each axis is named after its measures, the legend marks right-axis series, and only the left axis draws grid lines.
- Scatter plots colour at most three groups plus "Other".
- Themes: `theme.mode` (auto, light, dark) and an optional palette override in the spec. Named presets beyond "default" wait for branding in M6.

**Editor**
- react-grid-layout 2 for drag and resize. Only a finished drag or resize is recorded as an edit; the grid's own re-packing after a removal or an undo is not, otherwise it buried the real step in the undo history.
- Selection is on an element inside each tile, because the grid library owns the tile's own mouse handlers. Tiles can also be selected from the keyboard.
- **Deviation (accepted at the M4 review for v1):** field wells are role lists with an "Add a field" selector and remove buttons, not drag-and-drop of fields. Drag-and-drop is in the M8 backlog.
- Undo and redo keep up to 100 states in memory. Version history shows a line diff of the JSON between a version and what is on screen.
- Monaco is bundled locally (no CDN) as a lazy chunk with its JSON worker, so it works under the CSP and offline. It is about 3.4 MB (0.9 MB gzipped) and loads only when the JSON tab is opened.

## ADR-022: MCP server, access tokens, agent writes and live updates
- **Status:** accepted, M5

**Transport**
- The official MCP Python SDK (2.3), using its low-level `Server` with our own handlers, served over Streamable HTTP at `{BASE_PATH}/mcp`. It is added to the FastAPI app as one exact route, not a mount, so no trailing-slash redirect can happen behind a proxy.
- **Stateless, with plain JSON responses.** Every request stands alone: there is no session id to keep, nothing to store and nothing to stream, which is the simplest thing to carry through a reverse proxy. GET and DELETE on the endpoint answer 405, as the MCP spec allows for a server that offers no stream. The cost is that the server cannot push notifications to an MCP client; no tool needs that.
- Tool code is ordinary synchronous service code and runs in a worker thread, so a slow query never blocks the event loop.

**Tools**
- Defined in `opsis/mcp/tools.py` as plain functions behind one entry point, `tools.call`. Nothing in that file knows about MCP, so the in-app assistant (M7) will call the same functions.
- 21 tools. Not built yet: `share_dashboard` and `create_share_link`, which need the sharing model of M6. The catalogue supports a feature flag per tool, so a tool of a feature that is off is not listed and cannot be called.
- Written for small models (asked for at the M4 review):
  - datasets are referred to by name and fields by `dataset.field`, never by internal id; a dashboard may be named by title when the title is unique;
  - every description says when to use the tool and ends with one example input, and a test checks that each example is valid input for its tool;
  - arguments are validated against the schema of the tool before anything runs. An object or list sent as a JSON string, and a number sent as a string, are accepted;
  - `list_visuals` with a dataset returns a ready-to-use example item for every visual, built from the real fields of that dataset. A test checks that every example passes validation as it stands;
  - every failure is text with a stable code, the problems by JSON path, and a "What to do" line naming the next call. A wrong name gets "Did you mean" and the list of real names; a stale version says which version is current and to read again and retry;
  - results are compact JSON, lists are capped (`run_query` 1,000 rows, `preview_dataset` 100), and write results say what to do next and carry the editor link.
- Agent edits to dashboards and models are JSON Patch (RFC 6902) with `base_version`. The patch code is our own (`opsis/dashboards/patch.py`) so that each error names the operation and what is really at that path. All operations apply or none do, and the result goes through the same validation as a save from the UI.
- Row-level security: `run_query` goes through the semantic layer like everything else. `preview_dataset`, `profile_column` and the sample values in `describe_dataset` read rows directly, so they are refused (or left out) when a rule applies to the role of the caller.

**Who is calling**
- `MCP_AUTH=none` (allowed only on a loopback bind): calls run as the built-in admin.
- `MCP_AUTH=token`: `Authorization: Bearer <token>`. Personal access tokens are made on the Access tokens page, shown once, and stored as a SHA-256 hash (the token is 256 random bits, so a slow password hash adds nothing). Each has scopes (`read`, `edit`, `publish`, `admin`), an expiry of at most a year, can be revoked, and records when it was last used.
- A call runs as the owner of the token. A tool needs both the role permission of the owner and the scope on the token, so a token can narrow what its owner may do but never widen it. Tools the caller cannot use are not listed. The `admin` scope exists but no tool needs it yet.
- Tokens work for MCP only, not for the REST API.
- A request that carries an `Origin` header from another site is refused, so a web page cannot drive a local server from the browser.
- Not built: OAuth for claude.ai connectors (a later milestone in the brief).

**Audit**
- Versions created through MCP have `author_type = agent` and the user the call ran as. Every write tool also adds an audit row `agent.<tool>` with the tool, a summary of its arguments (paths of patch operations, not whole specs), the version it created, and how the caller was authenticated.

**Live updates**
- `GET /api/dashboards/{id}/events` is a server-sent event stream for an open editor: `hello` (the current version, on every connect), `version_created` and `published`.
- The hub is in the memory of the api process (`opsis/services/events.py`). All dashboard writes happen in that one process (REST, MCP, and later the assistant), so no broker is needed. If the api ever runs as several processes this must become Postgres LISTEN/NOTIFY; the interface is two functions.
- Each browser tab sends a random id with its writes (`X-Opsis-Client`), echoed in the event, so a tab ignores the events it caused.
- In the editor: a change from elsewhere is loaded at once when there is no unsaved work, with a notice "Updated by assistant. Undo". Undo restores the version the editor had before, as a new version, so the change by the agent stays in the history. With unsaved work the editor keeps it and offers to load the new version instead.

**Testing**
- Playwright (`e2e/`, run with `npm run smoke`) drives a real browser, and the official MCP TypeScript client, against the running stack at the root and at the sub-path. It replaces manual browser checks from M5 on.

## ADR-023: Sign-in, access lists, share links and tokens on the REST API
- **Status:** accepted, M6

**Sign-in**
- Passwords are hashed with argon2id (`argon2-cffi` defaults). Rules: at least 12 characters, not the email, at least four different characters. A failed sign-in takes as long for an unknown email as for a wrong password, and the message is the same.
- Sessions are rows in `sessions` keyed by the SHA-256 of a random cookie value (`opsis_session`, HttpOnly, SameSite=Lax, Secure under https, `Path` = base path). They end after `SESSION_IDLE_HOURS` without use (default 12), after `SESSION_MAX_DAYS` (default 14), on sign-out, on "sign out everywhere", when the password changes (other sessions only) and when the user is deactivated. `last_seen_at` is written at most once a minute.
- A password set by someone else (`ADMIN_INITIAL_PASSWORD`) must be changed before anything else works: every other route answers 403 `password_change_required`.
- Lockouts (`login_attempts`): failures are counted per email (5), per client address (30) and per share link (5) within 15 minutes; each failure past the limit locks the key for 1, 2, 4 ... minutes, at most an hour. Counting in Postgres makes it hold across restarts and processes. A lockout can be used to annoy someone; it is short and backs off, which is the usual trade-off.
- Invitations and admin resets are one-time links (`/set-password/<token>`, stored hashed, 7 days and 24 hours), shown once to the admin to pass on. `FEATURE_EMAIL` (M8) will send them instead.
- **Deviation:** `ADMIN_INITIAL_PASSWORD` is not required. With `AUTH_MODE=full` the first admin can sign in with Google instead; otherwise the log says how to get a link with `python -m opsis reset-link`. `ADMIN_EMAIL` is required.

**Google**
- Authorization code flow with PKCE (S256), state and nonce, scopes `openid email profile`. The flow's secrets travel in a 10-minute encrypted cookie (Fernet with `ENCRYPTION_KEY`, bound to its purpose), so a callback from another browser is refused. The ID token is verified against the provider's published keys: signature (RS256), issuer, audience, expiry and nonce; the keys are fetched again once if they have rotated.
- Only a Google-verified email is used. The allowlists (domains, addresses) are checked first, then the account: linked by Google subject, else matched by email (which links it), else created as a viewer only with `GOOGLE_AUTO_PROVISION=true`, which in turn requires an allowlist. Each refusal returns to the sign-in page with a reason, and is audited.
- **Deviation:** Authlib provides PKCE and token generation; the ID token is verified with `joserfc` (Authlib's JOSE library, which Authlib 1.9 depends on); the two HTTP calls use the standard library rather than an Authlib client, to avoid adding an HTTP client dependency.
- `GOOGLE_DISCOVERY_URL` exists so the smoke tests can use a fake provider (`e2e/fake-oidc.mjs`). It defaults to Google.

**Access**
- Dashboards: owner, shares to `user:<id>`, `group:<id>` or `everyone` as viewer or editor, and admins. Viewers see the published version only, including its title in lists. Editing also needs the editor role; deleting needs the owner or an admin. A dashboard someone cannot view answers 404, so its existence is not revealed (MCP lookups by title and "Did you mean" follow the same rule).
- Datasets: unrestricted by default; when restricted, only admins and granted users or groups may query, preview, model or build on them. A dataset someone may not use counts as missing when they validate or save a dashboard. Viewing a dashboard built on it needs no grant: the dashboard decides the questions, and RLS applies as the viewer.
- **Decided at the M5 review:** raw previews (REST and MCP) read through the same RLS-filtered scan as queries (`compiler.secured_scan`), and leave out the total row count when a rule applies. A rule that cannot be applied refuses.
- "View as" (admins): data requests may carry `viewAs`, a user id or a role and attribute set; RLS then applies as for that user. It shows their data, not whether they may open the dashboard.

**Share links**
- 32 random bytes, stored as a SHA-256, shown once; optional expiry and password; revocable; view count. They serve only the published version, and stop when the dashboard is unpublished.
- The link's attribute set is chosen when it is made. Empty (the default) means every RLS rule denies. Only an admin may choose any set; others may give none or their own, so a link can never show more than its maker can see. The link's viewer is never exempt from a rule.
- Available only with `FEATURE_PUBLIC_SHARE_LINKS`, which needs server mode and sign-in; otherwise the routes are not registered.

**Access tokens on the REST API (decided at the M5 review)**
- The same personal access tokens as MCP, with the same scopes: `read` for reading (including POSTs that change nothing, such as queries and validation), `edit` for changes, `publish` for publishing and sharing, `admin` for admin work. A token never exceeds its owner's role.
- Tokens cannot create tokens, change passwords or sign out sessions: those need a person in a browser.

**Local mode**
- With `AUTH_MODE=none` none of the sign-in, people, groups, sharing or share-link routes exist (404). The audit log page works in every mode.

## ADR-024: Dataset owners, sharing rights and restricted data through dashboards
- **Status:** accepted, decided at the M6 review, built at the start of M7

**Who restricts a dataset**
- A dataset's owner is the person who created it. The owner (while an editor) and admins can restrict it and change its list; the owner is always on the list.
- A restriction saved by an admin is admin-set (`datasets.restricted_by_admin`), and is the admins' alone: only an admin changes who is on its list or lifts it (decided at the M7 review). Its owner then sees it read-only. Restrictions made in M6, when only admins could make them, count as admin-set (migration 0008).

**Who shares a dashboard**
- Only the dashboard's owner and admins change its sharing: people, groups, everyone, and share links. Editors change content, not access. The MCP tools `share_dashboard` and `create_share_link` follow the same rule.

**Restricted data cannot leave its list through a dashboard**
- A dashboard built on a restricted dataset shows that dataset's data to everyone who can see the dashboard. So only the dataset's owner or an admin may show it to someone outside the dataset's list. The check runs wherever the audience or the data changes: sharing (new viewers against the published version's datasets, new or upgraded editors against the draft's), creating a share link (a link's audience is always outside), saving a draft (datasets new to the draft against everyone who edits it, its owner included), and publishing (datasets new to the published version against everyone who sees it, including active links). A refusal is 403 `restricted_data`, naming the dataset, its owner and who is outside.
- The share dialog lists the restricted datasets a dashboard uses and who would gain access to their data, and blocks the save when the person may not allow that. The dataset access panel lists the dashboards that already show the dataset's data outside its list.
- Cross-filters in data requests are limited to fields the dashboard itself uses, so a viewer cannot probe other fields of a dataset through a dashboard.

## ADR-025: The in-app assistant
- **Status:** accepted, M7

**Providers** (`opsis/assistant/providers.py`)
- `anthropic`: the official Python SDK (`anthropic` 2.x, which speaks API version 2026-10-21), streamed. Model `ANTHROPIC_MODEL` (default `claude-opus-5-5`). Thinking is adaptive (always on for current models) with `display: "updates"` (beta `thinking-display-updates-2026-08-18`), so the short notes the model writes between tool calls reach the panel. Effort comes from `ANTHROPIC_EFFORT` (default `medium`, the model's own default).
- Refusal fallbacks are on: `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`) re-runs a request the safety checks decline on the model Anthropic recommends for it, and the panel says which model answered. After a fallback in mid-reply, the declined part's thinking and tool calls are not sent back, as the API requires. `ANTHROPIC_FALLBACKS=false` turns this off, for a model without server-side fallbacks. A declined reply's partial output is dropped, not stored.
- No forced tool use (current models refuse it) and no sampling parameters (API version 2026-10-21 refuses them). Every tool has `eager_input_streaming`; each input is checked against the tool's JSON Schema before it runs (the MCP `call` already does this), a reply cut off at `max_tokens` never runs its calls, and an input the SDK cannot parse at all is asked for again, at most twice.
- Prompt caching: a breakpoint on the system prompt caches the tools and instructions, and automatic caching covers the conversation. The instructions never change (no date or state in them) and the tools are sorted by name, so the prefix is stable. The history is append-only and sent back byte for byte, thinking blocks included, which both the cache and Claude's preserved thinking need.
- `ollama`: Ollama's own chat API (`/api/chat`, streamed as JSON lines) at `OLLAMA_URL`, over `httpx2` (the SDK is for Claude only). Model `OLLAMA_MODEL` (default `gemma4:12b`). Every request sets the context size, `options.num_ctx` from `OLLAMA_NUM_CTX` (default 32,768, at least 16,384), so a default Ollama install works: its own default (4,096 tokens on this laptop) cuts off the instructions and tools, which take about 10,000 tokens.
- **Deviation, decided at the M7 review:** the brief names Ollama's OpenAI-compatible endpoint. That endpoint cannot set the context size per request, so in M7 it needed `OLLAMA_CONTEXT_LENGTH` set where Ollama runs. The native API replaced it. A reply that used 95% or more of the context is reported (Ollama drops the oldest messages of a conversation that does not fit), with a suggestion to start a new conversation or raise `OLLAMA_NUM_CTX`. The `ollama` Compose service is pinned to 0.40.16.
- Fail fast: `ASSISTANT_PROVIDER=anthropic` needs `ANTHROPIC_API_KEY`; the URL settings must be http or https; the model must not be empty. `ANTHROPIC_BASE_URL` exists for the smoke tests' fake model.

**The tool loop** (`opsis/assistant/loop.py`)
- Our own loop rather than the SDK's beta tool runner: one loop drives both providers, enforces the budgets, stops on request, and stores each step as it happens.
- Tools are 17 of the 23 MCP tools, called through `opsis.mcp.tools.call` with the person's own role and access, so every rule (RLS, restricted datasets, validation, audit as `agent.<tool>`) applies unchanged. Left out: `create_dashboard`, `publish_dashboard`, `share_dashboard` and `create_share_link` (the person does those with the editor's buttons), `list_connections` and `validate_model`. `patch_dashboard` and `restore_version` may change only the open dashboard.
- Budgets per request: `ASSISTANT_MAX_STEPS` tool calls (25), `ASSISTANT_MAX_OUTPUT_TOKENS` written by the model (100,000; at most 32,000 per call), `ASSISTANT_TIMEOUT_SECONDS` (600), and `ASSISTANT_MAX_ROWS` rows of a tool result shown to the model (100), with every result cut at 40,000 characters. Every tool call gets a result, run or not, so the history stays valid for the next request.
- Token usage per request is logged, audited (`assistant.request`, with tokens, cached tokens, model calls, steps, model and outcome) and added to the conversation's totals.
- Small local models: with gemma4 12B, Ollama often could not convert a long nested `patch_dashboard` call from the model's own tool-call format to JSON, logged "tool call parsing failed", and sent only the text before it, or nothing. So for Ollama the instructions ask for one dashboard item per call (three of three such calls came through in testing, against about half of the all-at-once calls), and a reply that is empty, or only announces a step ("I will add ..."), is answered with a request to make the call again, at most twice a request; the panel says when that happened. Claude keeps the all-at-once instruction, and its replies are taken as they come.
- Tried and reverted at the M7 review: every gemma4 build first wrote the line chart's time field as a bare object instead of a list of one, which validation refuses with its path, and fixed it in the next call. Showing the list form in `list_visuals` and the guide made it worse: gemma4 then wrote `x:[{...}]` in its own tool-call format with one bracket too many, Ollama dropped 3 calls in 3 builds, and one build ran out of time (against 3 of 3 builds and no dropped calls before). The examples stay as they were; the extra call is cheaper than a dropped one.
- Context per request: a `<context>` block at the start of the person's message with today's date, the open dashboard (id, version, items, filters), the selected item's spec and the datasets the person may use.

**Conversations and runs**
- Tables (migration 0009): `assistant_conversations` (one person, one dashboard, the provider and model), `assistant_messages` (the provider's own history, JSON rather than JSONB to keep key order, append-only) and `assistant_entries` (what the panel shows). Only the person can read their conversations, admins included. A conversation continues only on the provider and model that wrote it; after a change the next request starts a new one.
- A request runs as a task of its own on the event loop (`opsis/assistant/runs.py`). `POST .../assistant/messages` streams its events as server-sent events, but closing the panel does not stop it; Stop does, keeping the text written so far. One request per conversation at a time (409). In-process, like the live-update hub (R5).
- Only people use the panel, not access tokens: it spends the workspace's model budget on a person's behalf, and scripts and agents have MCP.

**The panel**
- A tab in the editor. Replies stream in; progress notes show in grey; each tool call is a step that opens to show its input and result; each change shows the version it saved, with **Undo** (restore the version before it) and **Undo all of this** per request. Undo is the ordinary restore: a new version, nothing lost.
- Send waits until the editor's changes are saved, so the assistant works on what the person sees. With the tab open the editor reloads the assistant's versions without its "Updated by assistant" banner, since the panel shows each change.

## ADR-026: Connectors, schedules, backups, email and the M8 backlog
- **Status:** accepted, M8

**Google Sheets** (`opsis/connectors/gsheets`)
- Each tab of one spreadsheet is a table. Opsis signs in as a Google Cloud service account with the read-only Sheets scope, so the spreadsheet is shared with the account's email address as a Viewer. The account's JSON key is a secret field of the connection (encrypted like any password); the form can read it from a file (`x-file-text` in the schema form).
- **Deviation:** the brief names the Google Sheets API client. The connector calls the Sheets REST API directly (two read calls) over `httpx2`, with `google-auth` only for the token: the full client library is large and adds nothing for two calls.
- Types come from the cells, read unformatted: numbers (integers when every value is whole), booleans, and cells formatted as dates or date-times, which become dates and timestamps in the spreadsheet's timezone (Sheets counts days from 30 December 1899). Mixed columns are text. A sheet has no reliable change marker, so only full refresh is offered.

**Salesforce** (`opsis/connectors/salesforce`)
- Each object (standard or custom) is a table, read with SOQL through the REST API (`simple-salesforce` 1.12). Sign-in goes to the org's My Domain, either with an External Client App using the OAuth client credentials flow (what Salesforce recommends for integrations) or with a username, password and security token.
- Incremental refresh merges by `Id`, following `SystemModstamp` (else `LastModifiedDate` or `CreatedDate`). SOQL date-time literals have whole seconds, so each refresh reads from the last second seen (`>=`) and the merge removes the repeats. Append is not offered: a record changes in place, so appending would keep every old copy. Records deleted in Salesforce stay until a full refresh, as with the SQL connectors. Compound fields (addresses, locations) and files are skipped; their parts are fields of their own.
- Test connection names the org's edition and whether it is a sandbox, so a test org can be confirmed as a Developer Edition before any data is read. Requests time out after 120 seconds.

**Schedules** (`opsis/services/schedules.py`, migration 0010)
- One `schedules` row per thing: `refresh:<dataset>`, `snapshot:<dashboard>:<person>`, `backup` and `cleanup`. Each has a cron expression in `WORKSPACE_TIMEZONE`, worked out with `croniter`, so 06:00 stays 06:00 across daylight saving. At most every 15 minutes. Presets cover the usual cases, and the dataset page shows the next runs before saving.
- The job runner (inline thread or worker) ticks every 30 seconds. A due row queues its job (with the dedupe key, so a refresh already queued or running is not queued twice) and moves on to its next time after now: runs missed while the runner was down are not made up, one run happens. Rows of deleted datasets and dashboards are removed. Each kind follows its flag.

**Backups** (`opsis/services/backups.py`)
- A backup is one tar file in `BACKUP_DIR`: a manifest, the metadata database (`pg_dump`, custom format), the uploaded files, and each dataset's current snapshot, so dashboards work straight after a restore. It runs as a job, and the runner runs one job at a time, so no refresh swaps a snapshot during a backup. `BACKUP_SCHEDULE` (default 02:30) and `BACKUP_RETENTION` (default 7 kept). The image carries `pg_dump` and `pg_restore` 16 from the PostgreSQL apt repository, to match the database.
- Restore is a command run with the api and worker stopped: `python -m opsis restore <file> --yes`. It replaces the database and the data folder's uploads and datasets, moving the previous ones aside. `ENCRYPTION_KEY` is not in a backup (it is in `.env`): without it a restored database's connection passwords cannot be read.
- **The clean-up job** runs every night at 03:15. It removes sessions a week after they ended, password links a month after they were used or expired, share links three months after they ended, sign-in failure counts after a day, and finished jobs after a month (each dataset keeps its latest). The audit log is kept.

**Email** (`opsis/services/mail.py`, `FEATURE_EMAIL`)
- SMTP with `smtplib`: STARTTLS, implicit TLS or none (`SMTP_SECURITY`), with optional sign-in. A message is sent while its request or job runs, with a 20 second timeout, and is never stored, so a password link never sits in a table. Each message has a plain-text part and an HTML part with inline styles. `FEATURE_EMAIL=true` without `SMTP_HOST` and an `SMTP_FROM` address fails fast.
- Invitations and reset links are emailed. If sending fails, the admin gets the link to pass on, with the reason. Without email, links are shown as before.
- "Forgotten your password?" on the sign-in page answers the same whether or not the address has an account. Every request counts, known address or not: 3 per address and 10 per client in 15 minutes, then a wait for everyone alike.
- **Emailed dashboards** ("scheduled snapshots" in the brief): a person asks for a dashboard to be emailed to them on a preset schedule. Each email is built as that person (their access, row-level security, the filters at their defaults, the version their dashboard page shows) and holds each visual's numbers: a KPI's value and comparison, and for the others the first 10 rows, with a bar beside a chart's first measure. **Deviation:** no pictures of charts. That would need a headless browser in the image, while tables show reliably in every mail client, and the email links to the dashboard. A subscription is personal and needs sign-in (an address to send to). If the person can no longer open the dashboard, or their account ends, the subscription is removed when it is next due, and nothing is sent.

**Agent inputs** (`opsis/mcp/lenient.py`, decided at the M7 review)
- The MCP tools and the assistant accept a single value where a list is expected and read it as a list of one, before validation. Where lists are expected comes from the JSON Schemas themselves (references, `allOf`, `if`/`then`/`else`, `oneOf`/`anyOf`, object properties, array items). Each normalisation is logged at debug level (`opsis.agent.lenient`) with the client, the tool and the path, to see which slips models make. Stored specs are always the strict canonical form, and the REST API and the JSON tab stay strict.

**The M8 backlog**
- **Models as a folder:** `GET /api/model-files` is a zip of `models/<dataset>.yml`, one per dataset the caller may use. `POST /api/model-files` takes such a zip, or `.yml` files: every file is checked first and nothing is saved unless all are valid (`dryRun=true` only reports). Files whose model is already the same are left alone; each changed model gets a version noted "Imported from <file>". At most 200 files and 20 MB.
- **Two-hop joins:** a dimension can come from a dataset two joins away (gifts to donors to regions). Every join on the way must be many-to-one or one-to-one. A path never goes through a restricted dataset. If two paths lead to the same dataset, a direct join must say which. After a left join the next join is left too, so no rows of the query's dataset are lost. The compiler, the spec check and the field lists share one rule (`semantic.model.reachable`), and REST and MCP queries check access to every dataset a query reads.
- **Drag and drop** in the editor: fields from a list into a visual's roles, and bound fields between roles. A role only accepts a field that fits it. The "Add a field" menus stay for the keyboard. This replaces the selectors-only wells of ADR-021.
- **Demo data:** `python -m opsis seed-demo`, or Settings, Load demo data. Three made-up datasets (15,000 gifts over the last three years, 1,500 donors, 8 regions) with their models and joins, and a published dashboard that uses a field two joins away. The rows depend only on the day. Running it again finishes a half-made demo and otherwise changes nothing.

**Other**
- A POST, PUT, PATCH or DELETE to an `/api` path that no router has answers 404, as a GET does, so a feature whose flag is off looks the same to every method. It answered 405 before, because the frontend's catch-all route matched the path.
- **Deviation, decided at M8:** SQL Lab is not in v1. `FEATURE_SQL_LAB`, its permission and its menu entry are gone; it is on the post-v1 backlog. The brief's rule still holds: there is no raw SQL from users or agents anywhere.
