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

## ADR-011: Verification tooling runs in containers
- **Status:** accepted, M0
- **Decision:** the dev machine has Git, Docker and Node, but no Python or uv. Backend lint, type checks and tests run inside containers (the app image from M1 onward). `tasks.py` will wrap these as `docker compose run` commands.
- **Why:** nothing extra to install on the laptop, and the same commands work on the server.
