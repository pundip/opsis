# Progress

Last updated: 2026-11-14 (end of Milestone 0)

## Milestone status

| Milestone | Status |
|---|---|
| M0 Plan | Done, awaiting review |
| M1 Skeleton and modes | Not started |
| M2 Data ingestion | Not started |
| M3 Semantic layer and queries | Not started |
| M4 Dashboards and visuals | Not started |
| M5 MCP | Not started |
| M6 Auth and sharing | Not started |
| M7 In-app assistant | Not started |
| M8 More connectors and hardening | Not started |

## What exists

- `docs/ARCHITECTURE.md`: component diagram, refresh and query flows, dashboard write flow, auth flows.
- `docs/DECISIONS.md`: ADR-001 to ADR-011.
- `spec/semantic-query.schema.json`, `spec/dashboard.schema.json`, `spec/visual-manifest.schema.json`.
- `backend/opsis/connectors/base.py` (`Connector` protocol) and `backend/opsis/query/engine.py` (`QueryEngine` protocol). Stubs only, no behaviour.
- `frontend/src/visuals/bar/manifest.json`: example manifest.
- `.gitignore` (ignores `.env`) and `.gitattributes` (LF).

No app code, no Compose file, no dependencies yet.

## How M0 was verified

In a throwaway `python:3.12-slim` container with `jsonschema`:

- All three schemas are valid draft 2020-12 and the cross-file `$ref`s resolve through a registry.
- The example semantic query, dashboard spec and manifest from the brief validate unchanged.
- 22 negative cases are rejected with useful paths (injection in a field reference, typo keys, wrong default types, missing operands, and so on).
- The stubs import without pyarrow, duck-typed classes satisfy both protocols, and `ruff check` and `mypy --strict` pass.

Not verified: the Mermaid diagrams have not been rendered (check them on GitHub), and the schemas have not been loaded in ajv yet (M1 or M4).

## Deviations from the brief

- `Connector.read` has a fourth optional argument `options`, and connectors have `close()` (ADR-007).
- Bindings may be `{ field, grain }` objects as well as strings (ADR-005).
- Filters gain an `in_range` operator; `timeDimension.grain` is optional (ADR-006).

## Risks

- **R1. psycopg is LGPL-3.0.** Not AGPL, and fine to use as an unmodified library, but it is outside the brief's preferred licence list. Alternatives: pg8000 (BSD) or asyncpg (Apache 2.0). See question Q1.
- **R2. DuckDB memory inside `api`.** A heavy query on a 10M-row dataset shares memory with the web process. Mitigation: per-connection memory limit and thread cap, query timeout by interrupt, row caps, and the M8 performance check.
- **R3. Incremental refresh still rewrites the snapshot.** Immutable snapshots mean append and merge modes save source load, not local write time. Acceptable at 10M rows. If it hurts, a later option is snapshots that share unchanged Parquet files.
- **R4. Model expressions are author-written SQL.** Measure `expr`, segments and RLS filters must be parsed with sqlglot and limited to an allow-list of nodes (no subqueries, no table functions such as `read_csv`). This is the main injection surface and gets the heaviest tests in M3.
- **R5. Single api process (ADR-010).** The cache and SSE are in-process. Fine for v1; scaling out needs `LISTEN/NOTIFY`.
- **R6. MCP Streamable HTTP mounted inside FastAPI.** The SDK's session manager needs to be wired into the app lifespan. I will check the current SDK docs at the start of M5 before designing around it.
- **R7. Relative date ranges need a timezone and a week start.** See Q2.
- **R8. Dev hot reload on the Windows filesystem.** The repo is at `C:\Users\...`, so file watching through bind mounts will be slow in `compose.dev.yml`. Running the built stack is unaffected. Moving the repo into WSL fixes it.
- **R9. Visual manifests in two places at runtime.** The backend reads manifests copied at image build. Dev mode needs a mount or a path setting so new visuals show up without a rebuild.
- **R10. Share links on RLS datasets.** Default deny is safe but will look like an empty dashboard. The create-link UI must say so clearly (M6).

## Questions for review

- **Q1.** Is psycopg (LGPL-3.0) acceptable, or should I use a permissively licensed Postgres driver? My recommendation: keep psycopg unless you plan to ship a closed-source bundle, in which case pg8000.
- **Q2.** Workspace defaults for relative dates: timezone `Australia/Melbourne` and weeks starting Monday? Do you need financial-year grains (July to June) in v1? I have assumed calendar years only.
- **Q3.** What licence should Opsis itself carry? The repo needs a `LICENSE` file in M1. My recommendation: Apache 2.0.
- **Q4.** Are you happy with the three deviations listed above?

## Exact next step

After review, start **M1 (Skeleton and modes)**: `compose.yml` with profiles, both env templates, the pydantic-settings module with fail-fast flag validation, `/healthz` and `/api/config`, the Alembic baseline (users, sessions, audit_log, jobs), the `AUTH_MODE=none` principal with permission checks, the frontend shell, the multi-stage Dockerfile, `.claude/launch.json` and the README quick start.
