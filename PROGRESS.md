# Progress

Last updated: 2026-11-14 (end of Milestone 2)

## Milestone status

| Milestone | Status |
|---|---|
| M0 Plan | Done, reviewed |
| M1 Skeleton and modes | Done, reviewed, catch-up to the revised brief done |
| M2 Data ingestion | Done, awaiting review |
| M3 Semantic layer and queries | Not started |
| M4 Dashboards and visuals | Not started |
| M5 MCP | Not started |
| M6 Auth and sharing | Not started |
| M7 In-app assistant | Not started |
| M8 More connectors and hardening | Not started |

## Review outcomes so far

- M0: keep psycopg; `Australia/Melbourne`, Monday weeks, financial years in v1 (ADR-012); Apache 2.0; deviations ADR-005, 006 and 007 approved.
- M1: brief replaced with the current version (sub-path hosting, section 3.1); ADR-013 superseded by ADR-016; `FISCAL_YEAR_LABEL` added; one-shot config check; placeholder secrets refused in server mode; `httpx2` kept.

## What exists

Run: `copy .env.local.example .env`, `docker compose up -d`, open http://localhost:8080. Add `COMPOSE_PROFILES=sample` for a sample MySQL source (see README).

**Platform (M1 and catch-up)**
- `compose.yml` profiles: `server` (worker), `caddy`, `tunnel`, `ollama`, `proxytest`, `sample`. A one-shot `config-check` service gates `api` and `worker`.
- `opsis/settings.py`: all flags, fail-fast rules (ADR-014), `base_path` derived from `PUBLIC_BASE_URL`, `TRUSTED_PROXIES`.
- `opsis/main.py`: everything mounted under the base path; `index.html` served with a runtime `<base href>` and JSON config block; security headers; CSRF check (`opsis/auth/cookies.py`).
- `opsis/auth/principal.py`: `Principal`, roles, permissions. `AUTH_MODE=none` uses a built-in admin. Other modes return 401 until M6.
- `opsis/semantic/calendar.py`: grains, relative ranges, fiscal years and labels.
- `opsis/api/sse.py`: SSE helper for use behind proxies (first real stream in M5).

**Data ingestion (M2)**
- Connectors: `opsis/connectors/file` (CSV, Excel), `postgres`, `mysql`, with `registry.py`, `sql_base.py` and `tabular.py`. Drop-in connectors go in `plugins/connectors/<id>/`.
- `opsis/services/`: `connections.py` (validation, encrypted secrets, test, discover), `datasets.py`, `refresh.py` (the refresh job), `crypto.py` (Fernet).
- `opsis/storage/snapshots.py`: Parquet snapshots for full, append and merge refresh; preview; cleanup.
- `opsis/jobs/`: `queue.py` and `worker.py` (`run_one`, the worker loop, and `InlineRunner` for `JOB_RUNNER=inline`).
- `opsis/api/ingestion.py`: `/api/connectors`, `/api/connections` (+ `/test`, `/{id}/test`, `/{id}/streams`), `/api/uploads`, `/api/datasets` (+ `/refresh`, `/preview`, `/snapshots`), `/api/jobs`.
- Migration `0002`: `connections`, `datasets`, `snapshots`.
- Frontend: Connections (schema-generated form, test, edit, delete), Datasets (create from a table, refresh), dataset page (preview, refresh history, snapshots), Jobs. Live status by polling.

## How M2 and the catch-up were verified

All on the Windows laptop with Docker Desktop.

- **Backend:** 119 pytest tests pass; `ruff check`, `ruff format --check` and `mypy --strict` pass. The tests run against real Postgres and MySQL 8.4 containers and cover: every fail-fast rule and both env templates; sub-path routing, base href injection, cookie scope and CSRF; connector discovery and type mapping for both databases; CSV and Excel; full, append and merge refresh; the atomic swap, retention and a failed refresh leaving data untouched; encrypted secrets never returned, logged in errors or written to the audit log; upload limits and path tricks; awkward and hostile identifiers; both job runner modes; restart recovery.
- **Frontend:** `tsc`, ESLint and 14 Vitest tests pass; the production build succeeds.
- **M2 acceptance, in the Browser pane at http://localhost:8080:** created a MySQL connection through the form (Test reported "Connection works"), created a dataset from `gifts`, saw 5,000 rows and the preview, pressed Refresh now and saw a second snapshot marked Current with the first one kept. The Jobs page listed both runs.
- **Worker mode:** with `JOB_RUNNER=worker` and the `server` profile, a refresh was claimed and completed by the worker container (`locked_by` began `worker:`). Inline mode was the default for the browser run (`inline:`).
- **Sub-path, with the `proxytest` profile at http://localhost:8088/opsis/:** another app answers at `/`; `/opsis` redirects to `/opsis/`; deep links and refresh load; assets and API calls stay under `/opsis`; `/healthz` and `/api/config` outside the prefix are 404; the cookie is `opsis_csrf` with `Path=/opsis`; a CSV upload, connection, dataset and refresh all worked through the proxy; a POST without the CSRF header was refused.
- **Root hosting:** the same image served the app at `/` with `<base href="/">` and `Path=/`.
- **Forwarded headers:** with `TRUSTED_PROXIES` set to the proxy's IP only, a spoofed `X-Forwarded-For` sent straight to the api was ignored, and a request through the proxy was attributed to the real client.
- **Config check:** the server template as shipped stopped once with three named problems (trusted proxies, database password, encryption key) and no restart loop. With those filled in, server mode started with the worker, `Secure` cookies and `Path=/opsis`.

A bug found and fixed during this testing: two `opsis_csrf` cookies on one host (root and sub-path installs on different ports) made the CSRF check fail. See ADR-016.

Not verified:

- The Proxmox LXC target and the real reverse proxy. Sub-path hosting was only tested behind the local nginx stand-in, over plain http.
- https end to end (a `Secure` cookie through a TLS-terminating proxy). Unit tests cover the cookie attributes only.
- The `caddy`, `tunnel` and `ollama` profiles after the profile split.
- SSE through the proxy. The helper has a unit test; there is no stream endpoint until M5.
- Large data. The biggest refresh run was 5,000 rows. Memory and speed at millions of rows are untested (M8 has the performance check).
- Uploading a file by clicking through the browser form. The upload API and the form's other fields were tested; the file was sent from the page with a script.
- A MySQL or Postgres server other than the test containers (MySQL 8.4, Postgres 16), including MariaDB and `ssl=verify`.
- Dev mode (`compose.dev.yml`) after the M2 changes, and hot reload itself.
- The Mermaid diagrams in a renderer, and the JSON Schemas in ajv (see ADR-018).

## Deviations from the brief

- ADR-005, 006, 007 (approved).
- ADR-016: CSRF protection brought forward from M6, because M2 adds write endpoints.
- ADR-017: the CSV and Excel connector id is `file`, not `csv`.
- ADR-018: connection forms use a small built-in schema form, not react-jsonschema-form, because ajv's compiler conflicts with the strict CSP. shadcn/ui is also not added yet. **Needs a decision before M4.**
- ADR-015: the task runner is npm scripts, not `tasks.py`; `httpx2` for the test client (approved).

## Open issues and notes

- A server install is not usable until M6: `AUTH_MODE=local` or `full` shows a sign-in notice only.
- Merge refresh does not remove rows deleted in the source. A full rebuild does.
- Uploads from an abandoned connection form stay on disk.
- A connection's snapshot columns show JSON source columns as `string` (the logical JSON type is not kept in Parquet).
- One job runs at a time per install. A long refresh delays the others.
- There is no cancel button for a running job, and no progress figure while it runs.
- Datasets cannot be edited after creation (mode, cursor, columns). Delete and recreate.
- `cloudflared` and `ollama` images are on `latest` until M6 and M7.
- `deploy/proxmox/README.md` and `deploy/windows/README.md` are not written yet (M6 and M8 in the brief). `deploy/proxytest/nginx.conf` is the reference proxy config meanwhile.
- No `.mcp.json` yet (M5).
- Risks from M0 still open: R2 (DuckDB memory in the api process), R3 (incremental refresh rewrites or relinks whole snapshots), R4 (model expressions are the main injection surface, M3), R5 (single api process), R6 (MCP mounting, M5), R8 (hot reload on the Windows filesystem), R10 (share links on RLS datasets).

## Questions for review

- **Q1.** ADR-018: are you happy to keep the built-in schema form for connections? For M4 visual config panels, should I (a) extend it, or (b) use rjsf with a validator that works under the strict CSP? I recommend deciding after I prototype (b) at the start of M4.
- **Q2.** Server template: `BIND_ADDRESS` defaults to `0.0.0.0` with a note to set the LXC's LAN IP. Do you want it to be a required CHANGE value that fails the config check until set?
- **Q3.** Editors can see the list of connections and their non-secret settings (host, database, user) so they can create datasets. Should that be admin only?

## Exact next step

After review, start **M3 (Semantic layer and queries)**: tables for semantic models; model CRUD with validation; YAML import and export (`models/*.yml`); starter-model generation from a dataset's snapshot columns; the sqlglot compiler from semantic query to DuckDB SQL (dimensions, measures, expression measures, time grains including fiscal, relative ranges through `opsis/semantic/calendar.py`, filters, segments, joins, sort, limit); RLS injection with deny by default; an allow-list for model expressions; `DuckDBEngine` implementing `QueryEngine` with timeout, memory limit and row cap; the LRU result cache; `POST /api/query`; and the compiler test suite including RLS and injection attempts. A minimal UI to edit a model and run a query is enough for the M3 acceptance check.
