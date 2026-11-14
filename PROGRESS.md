# Progress

Last updated: 2026-11-14 (end of Milestone 1)

## Milestone status

| Milestone | Status |
|---|---|
| M0 Plan | Done, reviewed |
| M1 Skeleton and modes | Done, awaiting review |
| M2 Data ingestion | Not started |
| M3 Semantic layer and queries | Not started |
| M4 Dashboards and visuals | Not started |
| M5 MCP | Not started |
| M6 Auth and sharing | Not started |
| M7 In-app assistant | Not started |
| M8 More connectors and hardening | Not started |

## M0 review outcomes

- Keep psycopg (LGPL-3.0).
- Timezone `Australia/Melbourne`, weeks start Monday, financial-year support in v1 (ADR-012).
- Opsis is licensed Apache 2.0.
- The three M0 deviations (ADR-005, 006, 007): the review answer for this item arrived empty. They are still in place and assumed accepted. **Confirm or object at the M1 review.**

## What exists after M1

- **Run:** `copy .env.local.example .env` then `docker compose up -d`, open http://localhost:8080.
- **Compose:** `compose.yml` (api, postgres; `server` profile adds worker and caddy; `tunnel` adds cloudflared; `ollama` adds ollama) and `compose.dev.yml` (hot reload, Vite on 5173, `tools` service for tests).
- **Image:** `backend/Dockerfile`, multi-stage (Node build, Python runtime, `dev` target with test tools), non-root user, visual manifests and `spec/` copied in.
- **Settings:** `backend/opsis/settings.py` (pydantic-settings). Fail-fast rules are listed in ADR-014.
- **API:** `/healthz`, `GET /api/config`, `GET /api/me`, one error shape, security headers, SPA fallback. Guards `require(permission)` and `require_feature(flag)` in `opsis/api/deps.py`.
- **Auth model:** `Principal`, `Role`, `Permission` in `opsis/auth/principal.py`. `AUTH_MODE=none` resolves to a built-in admin row and goes through the same permission checks. Other modes return 401 until M6.
- **Database:** Alembic baseline `0001` with `users`, `sessions`, `audit_log`, `jobs`. Migrations run when the api starts.
- **Jobs:** `opsis/jobs/queue.py` (enqueue, claim with SKIP LOCKED, finish) and `opsis/jobs/worker.py` (loop used by the worker container). No handlers yet. The inline runner thread is not built yet: it arrives with the first job type in M2.
- **Calendar:** `opsis/semantic/calendar.py`, fiscal grains and ranges in `spec/semantic-query.schema.json`.
- **Frontend:** React, TypeScript, Vite, TanStack Query, React Router, Tailwind v4. A shell with navigation driven by `/api/config` and the user's permissions, and a home page showing mode, features and calendar.
- **Other:** `README.md`, `LICENSE`, `branding.json`, `.claude/launch.json`, root `package.json` task scripts, `deploy/caddy/Caddyfile`.

## How M1 was verified

All on the Windows laptop with Docker Desktop.

- **Backend:** 75 pytest tests pass in the dev image (settings rules and both env templates, API, permissions, migration drift check, job queue, worker, calendar). `ruff check`, `ruff format --check` and `mypy --strict` pass.
- **Frontend:** `tsc`, ESLint and 5 Vitest tests pass; the production build succeeds.
- **Local env template:** stack starts healthy; `/healthz`, `/api/config` and `/api/me` respond; the container runs as uid 10001; the shell renders in the Browser pane with no console errors.
- **Flag change:** editing four values in `.env` and running `docker compose up -d` changed `/api/config` and the UI (SQL Lab link gone, Schedules link added, name and financial year start updated).
- **Fail fast:** `BIND_ADDRESS=0.0.0.0` plus share links stopped the api with four named problems; `check-config` exits with code 2.
- **Server env template:** started unmodified except for the Caddy host ports (8081 and 8443, because port 80 is in use on this laptop). api, worker, caddy and postgres came up; `/api/config` through Caddy showed server flags and HSTS; `/api/me` returned 401; the UI showed the sign-in notice; the worker claimed a queued job.
- **Dev mode:** `compose.dev.yml` started, Vite served the app on 5173 and proxied `/api`.

Not verified:

- The Proxmox LXC target (no access from here). Server mode was only run on Docker Desktop.
- The `tunnel` and `ollama` profiles (they need a token and a model download).
- Automatic HTTPS in Caddy (needs a real domain).
- Hot reload after editing a file in dev mode (the servers started; I did not time or test a reload).
- The Mermaid diagrams are still not checked in a renderer.
- The JSON Schemas are still not loaded in ajv.

## Open issues and notes

- `AUTH_MODE=local` or `full` currently gives a sign-in notice only. A server install is not usable until M6.
- An invalid config makes the api container restart in a loop (restart policy), repeating the message in the logs. That is visible in `docker compose ps` and `docker compose logs api`.
- The server template boots with a placeholder database password. The README and the template say to change it; nothing enforces it (ADR-014).
- `cloudflared` and `ollama` images are on `latest` until M6 and M7.
- Risks R1 to R10 from M0 still stand. R1 (psycopg licence) is closed by the review. R7 (timezone and week start) is closed by ADR-012.

## Questions for review

- **Q1.** Confirm the three M0 deviations (see above).
- **Q2.** ADR-013: is a loopback-only publish of the api on the server acceptable, or do you want a strictly Caddy-only server (which needs a second small Compose file)?
- **Q3.** Fiscal labels: is "FY2027" for July 2026 to June 2027 (named by the ending year) the convention you want? Some organisations write "FY26/27".

## Exact next step

After review, start **M2 (Data ingestion)**: connector registry; CSV/Excel upload, Postgres and MySQL connectors; `ENCRYPTION_KEY` and encrypted connection secrets; connections UI with schema-generated forms (add rjsf and shadcn/ui here); datasets from streams; refresh jobs through the queue with an inline runner thread and the worker; Parquet snapshots with atomic pointer swap and retention; job status UI and dataset preview. New tables: connections, datasets, snapshots.
