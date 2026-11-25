# Progress

Last updated: 2026-11-25 (join warnings, after M8)

## Milestone status

| Milestone | Status |
|---|---|
| M0 Plan | Done, reviewed |
| M1 Skeleton and modes | Done, reviewed, catch-up to the revised brief done |
| M2 Data ingestion | Done, reviewed |
| M3 Semantic layer and queries | Done, reviewed |
| M3.5 Timezones, conditional measures, model versions, benchmark | Done, reviewed |
| M4 Dashboards and visuals | Done, reviewed |
| M5 MCP | Done, reviewed |
| M6 Auth and sharing | Done, reviewed |
| M7 In-app assistant | Done, reviewed |
| M8 More connectors and hardening | Done, awaiting review. Still to do: your part of the install check (Proxmox, real Google sign-in), and real Salesforce and Google Sheets runs with your credentials |
| After M8: legacy data | Done, awaiting review |
| After M8: join warnings | Done, awaiting review (branch `claude/unruffled-northcutt-320a28`) |

## After M8: warnings for two join traps

Both were found while writing the user guide: a lookup that lists a key twice silently doubled totals, and a row-level security rule on a joined dataset did not restrict the base dataset's rows. Query results are unchanged; the model now says so (details in ADR-028).

- **Repeated join keys:** for every `many_to_one` and `one_to_one` join a query can use, two joins away included, the key columns of the joined dataset's current data are checked. A warning names the key, how many values repeat and up to 5 examples. It is checked again after a refresh (the result is cached by snapshot; nothing is stored, and the refresh job is unchanged). Examples are hidden from people who may not use that dataset or whom its row-level security applies to.
- **Rules across joins:** a model with no rules of its own that joins to a dataset with rules gets a warning.
- **Never an error:** Check and Save go through. Warnings show on the model page (at the top, and after Check and Save), in the model API (get, save, restore, validate) and in MCP `get_semantic_model`, `validate_model` and `patch_semantic_model`.
- **Guide:** chapters 4, 5 and 9. The guide commit (`d92767f`) was fast-forwarded into this branch first.

### How it was verified

- **Backend:** 325 pytest tests pass (8 new, in `tests/test_model_warnings.py`: the doubled totals and the check after a refresh; composite keys, the 5 examples, empty keys and which relationships are checked; two joins away and restricted datasets; rules across joins; examples hidden from an editor on the page, Check, Save and over MCP; the MCP tools; a check that cannot run). `ruff check`, `ruff format --check` and `mypy --strict` pass.
- **Frontend:** typecheck, lint and the 48 unit tests pass.
- **Browser:** the smoke tests, with the new `e2e/tests/model-warnings.spec.ts`, pass at the root, under the sub-path and in server mode. Your stack was running on port 8080, so nothing of it was touched: the root run used this branch's own stack (project `opsis-joinwarn`, port 8091) with the same commands as `npm run smoke`, and the other two ran as `npm run smoke -- --subpath --server`. The test stacks, their volumes and image, and the test `.env` were removed afterwards.

Not done: dashboards, the models list, the model file import and `describe_dataset` show no warnings.

## After M8: legacy data that does not fit its column

The bug: a dataset from a MyISAM table (utf8mb3, non-strict `sql_mode`) failed with `Reading 'a_codes' failed: object of type <class 'str'> cannot be converted to int`. PyMySQL returns zero dates as text, and building the Arrow timestamp column failed the whole read. Fixed at the root, for every connector (details in ADR-027):

- **One conversion layer** (`opsis/connectors/convert.py`) that every connector builds its batches through: CSV, Excel, PostgreSQL, MySQL, Google Sheets and Salesforce. A value that cannot become its column's type is stored as NULL and counted, per column and reason, with up to 5 raw samples. One bad value never fails a read. Clean columns still go straight through Arrow.
- **Invalid dates** (`0000-00-00`, `2020-00-00`, `2020-05-00`, `2021-02-30`, Postgres `infinity` and dates outside years 1 to 9999) follow a new setting on every connection, **Invalid dates**: store as NULL (the default), read the connection's date columns as text, or stop the refresh.
- **The report** is kept with each snapshot (migration 0012) and is the refresh job's `dataQuality` result: per column the count stored as NULL, the reason and samples. It shows on the dataset page (**Data quality**) and in the refresh history, and MCP's `describe_dataset` and `get_job_status` return it. Samples are left out for people who may not use a restricted dataset and where row-level security hides rows from them.
- **Column types by hand** on the dataset page (**Column types**): text, whole number, decimal number, date (with a format such as `%d/%m/%Y`), date and time, or true or false. Saving rebuilds the dataset; the choices are kept when the source's columns change, and audited.
- **Suggestions:** a text column whose values are consistently dates (ISO or a common format), numbers or true/false gets a suggested type with a **Use this type** button. Nothing is applied by itself; placeholders such as `n/a`, codes with leading zeros, and columns with too few values are handled.
- **Errors** from conversion or a cursor column name the table, the column, its source type and a sample value.
- **Type fixes:** MySQL `BIGINT UNSIGNED` and `BIT(64)` are exact decimals, `BIT(n)` is a number, `DECIMAL` or `numeric` beyond 38 digits is a double, `TIME` reads as `HH:MM:SS` (also past 24 hours), Postgres intervals read as Postgres writes them, binary data shows as `0x...`. An incremental cursor ignores invalid dates.

### How it was verified

- **Backend:** 317 pytest tests pass (22 new); `ruff check`, `ruff format --check` and `mypy --strict` pass. New tests cover the conversion layer (zero, zero-day, zero-month and impossible dates; NULLs; garbage in number columns; no rounding of 1.5 into an integer column; samples capped at 5; the three invalid-date settings; suggestions; overrides with formats), a MyISAM table created in `sql_mode=''` in the sample MySQL with ENUM, SET, YEAR, BIT(1), BIT(8), BIT(64), TINYINT(1), DECIMAL(10,2), DECIMAL(65,30), BIGINT UNSIGNED, JSON, TIME and virtual and stored generated columns (the report through the API, the job and MCP; column types set by hand, refused when wrong, kept after the source gains a column; stop and text; an append cursor over zero dates), Postgres `infinity`, BC dates, year 10000 and intervals, CSV and Excel suggestions and overrides, text in Google Sheets date and boolean columns, unparseable Salesforce values, and samples hidden from someone who may not see a restricted dataset.
- **Frontend:** typecheck, lint and the 48 unit tests pass.
- **Smoke tests:** `npm run smoke` passes at the root, under the sub-path and in server mode, with a new spec (`e2e/tests/data-quality.spec.ts`): suggestions shown but not applied, one taken and a type chosen by hand, the dataset rebuilt, the report's table with reasons and samples, the count in the refresh history, and back to the source's types. The root and the sub-path were run again on the final code.
- **Your `a_codes` table:** the dataset that failed now refreshes (154 rows). Its report shows 96 values in `end_date` and 2 in `date_created` stored as NULL, all zero dates, and MCP's `describe_dataset` returns the same.

## M8 More connectors and hardening

Built (details in ADR-026):

- **Google Sheets connector:** each tab of a spreadsheet is a table, read-only. It signs in with your own Google account through your own OAuth client (`credentials.json`, Desktop or Web), or with a service account. **Connect Google account** runs the consent flow (offline access, prompt=consent, PKCE, `spreadsheets.readonly` and `drive.metadata.readonly`). On a laptop Google comes straight back to Opsis; on a server you paste the address Google sent you to. A `token.json` can be used instead. The refresh token is stored encrypted and shown to nobody, the connected account is shown with a **Reconnect** button, and you pick the spreadsheet from your list. When Google answers `invalid_grant`, the connection says it needs reconnecting, its scheduled refreshes pause, and its owner is emailed. Migration 0011. Column types come from the cells. Full refresh only.
- **Salesforce connector:** each object is a table. Sign-in through an External Client App (client credentials, recommended) or a username, password and security token. Merge refresh follows `SystemModstamp` and merges by `Id`. Test connection names the org's edition, so a Developer Edition can be confirmed before anything is read.
- **Refresh schedules** (`FEATURE_SCHEDULED_REFRESH`): presets or cron in the workspace timezone, the next runs shown before saving, at most every 15 minutes, and a **Schedules** page. Migration 0010.
- **Backups** (`FEATURE_BACKUPS`): a nightly job writes one file to the `backups` volume (database, uploads, each dataset's current data), keeps the newest 7, and **Settings** has **Back up now**. Restore is `python -m opsis restore <file> --yes` with the api and worker stopped; the previous data is moved aside. The image now carries `pg_dump` 16.
- **Clean-up job**, nightly: ended sessions, used password links, long-ended share links, old sign-in counts and old finished jobs. The audit log is kept.
- **Email** (`FEATURE_EMAIL`, SMTP): invitations and password reset links are emailed (the admin gets the link instead if sending fails), **Forgotten your password?** on the sign-in page (the same answer for any address, rate-limited), **Send me a test email** in Settings, and **Email me** on a dashboard: the dashboard's numbers on a schedule, as that person sees them, listed under Account. Messages are never stored.
- **Lenient agent inputs** (from the M7 follow-up): the MCP tools and the assistant read a single value as a list of one where a list is expected, worked out from the schemas, and log each one at debug level (`opsis.agent.lenient`). Stored specs, the REST API and the JSON tab stay strict.
- **Models as a folder:** **Export all** (a zip of `models/<dataset>.yml`) and **Import** on the Semantic models page: every file checked first, nothing saved unless all are valid, unchanged models left alone.
- **Two-hop joins:** a dimension two joins away (gifts to donors to regions), every join many-to-one or one-to-one, never through a restricted dataset; two ways to one dataset need a direct join to say which.
- **Drag and drop** of fields into a visual's roles, and between roles, in the editor. The menus stay for the keyboard.
- **Demo data:** **Settings**, **Load demo data**, or `python -m opsis seed-demo`: three made-up datasets with their models and joins, and a published dashboard that uses a field two joins away.
- **SQL Lab is out of v1** (your M8 instructions): `FEATURE_SQL_LAB`, its permission and its menu entry are gone, and it is on the post-v1 backlog.
- **Docs:** the README (demo data, Google Sheets and Salesforce set-up, schedules, backups and restore, email), the Windows guide, the Proxmox guide (backups on NFS, restore, email, an install checklist), ADR-026, an architecture section, and `BACKUP_*` and `SMTP_*` in both env templates.

### How M8 was verified

- **Backend:** 295 pytest tests pass (42 new); `ruff check`, `ruff format --check` and `mypy --strict` pass. New tests cover: both connectors against realistic fakes of the Google and Salesforce APIs (types, header rows, sign-in failures, merge refresh with the repeated second, append refused); schedules (presets, cron checks, the next runs across the end of daylight saving, the tick, no make-up runs, flags); backups (a real `pg_dump` and `pg_restore` into a separate database, retention, the manifest) and the clean-up job; email against a local SMTP server (invitations and resets by email and when sending fails, the same answer for unknown addresses, the rate limit, the test email, emailed dashboards with row-level security, escaping and the removal of a subscription when access ends); lenient inputs; the models folder (export, unchanged re-import, all-or-nothing, dry run, bad files); two-hop joins in the compiler and through the API, with ambiguous, unsafe, restricted and three-hop paths refused; the demo data locally and in server mode. Google Sheets through your own OAuth client is tested against a fake Google token endpoint on a local port, which checks the PKCE verifier, renews tokens, revokes them and answers tokeninfo. The tests cover: the consent address (offline access, prompt=consent, S256, both scopes); finishing straight back in local mode and by a pasted address on a server; a Web client's redirect URIs; unticked scopes; the wrong kind of file; token.json (with and without its scopes, from another client, revoked); the refresh token never in an answer or in plain text; invalid_grant marking the connection, emailing its owner once, pausing its schedule and resuming it after a reconnect; and a new OAuth client ending the old sign-in.
- **Frontend:** `tsc`, ESLint and 48 Vitest tests pass.
- **Browser (`npm run smoke`), all passed** at http://localhost:8080, at http://localhost:8088/opsis/ and in server mode. New: a dataset's refresh schedule (a preset, its next runs, a too-frequent cron refused), the Schedules page and Settings; dragging a field into a role in the editor; and in server mode, against a stand-in mail server whose messages the test reads (`e2e/fake-smtp.mjs`): an invitation by email whose link sets the password, **Email me** and stopping it from Account, **Forgotten your password?** through to a new password, the test email, a real backup made by the worker, and the demo data with its dashboard. Also, at all three, Google Sheets through your own Google account against a stand-in Google (`e2e/fake-google.mjs`). At the root Google comes straight back to Opsis; under /opsis and in server mode the test pastes the address. Then it picks a spreadsheet from the list and refreshes it, the sign-in is revoked (invalid_grant), the connection asks to be reconnected (in server mode its owner is emailed), and it is reconnected and refreshes again. The first run found two problems, both fixed: the Settings page stopped looking for a finished backup or demo after its first check (so a slow job never showed as done), and a wrong button name in the new test.
- **Fresh install from the README:** see below.

### Performance, 10 million rows

`node scripts/benchmark.mjs --rows 10000000` against your stack on this laptop (Docker Desktop, 16 CPUs, 15.4 GB for containers, default query settings, inline job runner). The new step opens a typical dashboard as a browser does: 8 visuals (4 KPI cards with a comparison to the year before, a line by month and channel, a pie, a bar, a table through a join), so 12 queries, 6 requests at a time, with a date range and a region filter.

| Step | Result |
|---|---|
| Ingestion, 10.2 million rows from Postgres to Parquet | 32.2 s (322,000 rows/s); snapshot 147 MB; api peak 290 MB |

| Dashboard load | Total | Slowest item | Cached |
|---|---|---|---|
| Cold: first open | 492 ms | 380 ms | 0 of 8 |
| **Warm: opened again** | **81 ms** | 65 ms | 8 of 8 |
| After a filter change (two regions) | 516 ms | 381 ms | 0 of 8 |
| That filter, warm | 92 ms | 71 ms | 8 of 8 |

**The brief's target, under 1.5 s warm on 10 million rows, is met: 81 ms warm, and 0.5 s even with nothing cached.**

| Query | Cold | Warm | Rows |
|---|---|---|---|
| KPI: total, count, distinct donors, last 12 months | 198 ms | 15 ms | 1 |
| Bar: total by fund, two regions | 84 ms | 15 ms | 8 |
| Line: total by month and channel, 3 years | 114 ms | 13 ms | 148 |
| Join: by donor tier and financial quarter | 296 ms | 13 ms | 39 |
| Table: top 100 of 200,000 donors | 185 ms | 12 ms | 100 |

- Ten different uncached queries at once finished in 3.4 s and 2.6 s (two rounds), while `/healthz` answered in a median of 16 and 12 ms. Peak api memory during the queries: 422 MB.
- At twice the M3.5 size, single queries are about 1.2 times slower and ingestion runs at the same speed per row.

Not measured: the Proxmox LXC (fewer cores, less memory), slow disks, worker mode, and many people at once.

### Fresh install from the README

On this laptop, from a clean clone from GitHub (the `m8-wip` branch, before the merge) into an empty folder, following the README alone. Two test-only differences, because your own stack holds port 8080 and the project name `opsis`: ports 8090 and 8091, and the Compose project name `opsis-fresh`, so it had volumes of its own. All of it was removed afterwards (containers, volumes, image and folder).

- **Laptop template:** `copy .env.local.example .env`, then `docker compose up -d`. The image built from scratch, with no cache, in 64 s; `config-check` reported a valid configuration and the api became healthy. `docker compose exec api python -m opsis seed-demo` loaded the demo data. The browser tests that fit the default settings passed against it (4 of 4): building a dashboard, local mode, MCP with live updates, and access tokens.
- **Server template:** `cp .env.server.example .env`, the key from `generate-key`, and every `CHANGE` value filled with test values (`FEATURE_ASSISTANT=false`, for want of a Claude key). Postgres, the api and the worker became healthy, serving at `/opsis`. The first sign-in asked for a new password, and the nightly backup and clean-up were scheduled. The worker loaded the demo data (all 8 visuals with rows) and made a backup; `python -m opsis backup` wrote another. After the demo dashboard was deleted, the README's restore (stop, `restore <file> --yes`, start) brought it back with its data and moved the previous data aside, and the admin's password was again the one from the time of the backup.
- Not covered here: TLS, a real reverse proxy, Google sign-in, real email, and Linux itself. The server-mode smoke test covers a proxy, a fake Google and a stand-in mail server; the rest is the Proxmox install check.

### Your stack

- **Test leftovers deleted**, as you approved: the ten check dashboards ("Real model check" four times, "Ollama check 1" to "3" twice each), and "Smoke mwh0pics" with its dataset and connection.
- **The demo data is loaded** (with `python -m opsis seed-demo`): **Giving overview (demo)** and the datasets `demo_gifts`, `demo_donors` and `demo_regions`.
- Left as they were: "Gifts overview", "Giving overview (built over MCP)", and **"Test1"** (made today at 17:04, not one of mine, so not on the list).
- Your `.env` still has `FEATURE_SQL_LAB=true`. It is now ignored, so the line can go.

Not verified:

- Salesforce and Google Sheets against the real services (they need your credentials; see the next step).
- A real mail server (only the stand-in and a local SMTP server in pytest), and how emailed dashboards look in Outlook and Gmail.
- The Proxmox install, backups on NFS, the real reverse proxy and Google sign-in: your part, the checklist in `deploy/proxmox/README.md`, section 11.
- Restoring a backup made by an older version.

## M7 In-app assistant (reviewed)

Built in M7 (ADR-025): the Assistant tab in the editor, on Claude (the `anthropic` SDK, adaptive thinking, prompt caching, refusal fallbacks) or Ollama (its own chat API, with the context size sent on every request), a tool loop over 17 of the MCP tools with budgets and Stop, and conversations kept in Postgres. At the M7 review: admin-set restrictions became admin-only, Ollama moved to its native API, and the real Ollama check passed 3 of 3 builds (2 min 21 s to 3 min 4 s, no dropped tool calls). The real Claude check was skipped, as agreed.

## M6 Auth and sharing (reviewed)

Built in M6 (ADR-023): sign-in with local accounts and Google, sessions and lockouts, people and groups, access lists for dashboards and datasets, share links, View as, view state in the URL, the audit log screen, tokens on the REST API, the MCP sharing tools, and the Proxmox and Windows guides. Verified with 231 backend tests, 42 frontend tests and the smoke tests at the root, the sub-path and in server mode. Real Google sign-in and the real server are still to check: that is now a checklist in the Proxmox guide, part of the M8 install check.

## M5 MCP (reviewed)

Built in M5 (ADR-022): the MCP server at `/mcp` (stateless, JSON), tools written for small models, JSON Patch with `base_version`, personal access tokens, audit of agent writes, live editor updates over server-sent events, the Playwright smoke tests and the combo chart's right-hand axis.

M3.5 benchmark, in one line: 5 million rows on this laptop, typical queries 77 to 250 ms cold and 11 to 14 ms warm; 10 uncached queries at once finished in 3.4 to 3.6 s; peak api memory 1.1 GB (full tables below).

## Review outcomes so far

- **M0:** keep psycopg; `Australia/Melbourne`, Monday weeks, financial years in v1 (ADR-012); Apache 2.0; deviations ADR-005, 006 and 007 approved.
- **M1:** brief replaced with the current version (sub-path hosting, section 3.1); ADR-013 superseded by ADR-016; `FISCAL_YEAR_LABEL` added; one-shot config check; placeholder secrets refused in server mode; `httpx2` kept.
- **M2:** keep the built-in schema form for connections; at the start of M4 prototype rjsf with a CSP-safe validator and decide then (ADR-018). `BIND_ADDRESS` stays `0.0.0.0` in the server template and is not a required value. Editors may see connections and their non-secret settings.
- **M3:** approved with M3.5 (timezones, conditional measures, model versions, value checks, benchmark; all done). Admins stay exempt from RLS by default, with a visible badge (done in M4), share links never exempt and "View as" in M6. Bulk model export and two-hop joins go to M8. Every milestone is verified at the root and under /opsis.
- **M4:** approved. Field selector is fine for v1 (drag-and-drop to the M8 backlog). Playwright smoke test at the start of M5, at both URLs, replacing manual browser checks from then on. Combo chart gets an optional right-hand axis per measure. Filter and drill state in the URL in M6 with share links. MCP tool descriptions and errors must work for a smaller model: anything Sonnet struggles with counts as a tool design issue.
- **M5:** approved. REST previews apply the RLS filter, or refuse where a rule cannot be applied. Access tokens on the REST API in M6 with the MCP scopes. Keep MCP stateless. In M6, verify `MCP_AUTH=token` against the running container in server mode through the proxytest profile (done).
- **M6:** approved. Dataset owners restrict their datasets; admins can override, and only an admin lifts an admin's restriction. Sharing is limited to a dashboard's owner and admins. Restricted data must not reach people outside its list through a dashboard, with warnings in the share dialog, and the same rules in the MCP sharing tools. Real Google sign-in becomes a checklist in the Proxmox guide and part of the M8 install check. The Sonnet test has not been run yet. (All done at the start of M7.)
- **M7:** approved. Admin-set restrictions are admin-only, list included. The real Claude check is skipped for now. Ollama moves to its own chat API with the context size set per request (`OLLAMA_NUM_CTX`, default 32768), keeping one visual per call; then the test alias goes and `OLLAMA_MODEL=gemma4:12b`. Refusal fallbacks stay on by default. (All done.)
- **M7 follow-up and M8 instructions:** agent tool inputs accept a single value where a list is expected (stored specs, REST and the JSON tab stay strict; each normalisation logged at debug level). Salesforce is tested against a Developer Edition org only, never a production org, with credentials you provide when needed; for Google Sheets I say when a service account is needed and what access it needs. The Proxmox install and the real Google sign-in are the final install check, done by you from the checklist. SQL Lab is not built for v1 (post-v1 backlog). The test leftovers in your stack are deleted, keeping the demo data. (Done, apart from the real Salesforce and Google Sheets runs and the Proxmox check: see the next step.)
- **For the LXC work (noted at the M2 review):** the server will use port forwarding to the LXC. Check then what source address the api sees for proxied requests, because that is the address `TRUSTED_PROXIES` must list (it may be the forwarder or the Docker gateway, not the reverse proxy itself).

## What exists

Run: `copy .env.local.example .env`, `docker compose up -d`, open http://localhost:8080, then Settings, Load demo data. Add `COMPOSE_PROFILES=sample` for a sample MySQL source (see README).

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

**Sign-in and sharing (M6)**
- `opsis/auth/`: `passwords.py` (argon2id), `google.py` (OpenID Connect), `principal.py` (groups, scopes, session).
- `opsis/services/`: `accounts.py` (first admin, invitations, password sign-in, Google matching, admin changes), `sessions.py`, `lockout.py`, `access.py` (dashboard and dataset access, sharing, groups), `links.py` (share links), `crypto.py` (sealed cookies).
- `opsis/api/`: `auth.py`, `admin.py` (users, groups, audit), `sharing.py` (access lists, links, the public side); `deps.py` (sessions, tokens, scopes). Migration `0007`. CLI `reset-link`.
- Frontend: `pages/Login`, `SetPassword`, `Account`, `Users`, `Audit`, `PublicDashboard`; `components/dashboard/SharePanel`, `components/DatasetAccessPanel`; `lib/access.ts`, `lib/urlState.ts`.
- `e2e/auth.spec.ts`, `e2e/local.spec.ts`, `e2e/global-setup.ts`, `e2e/fake-oidc.mjs`, `compose.servertest.yml`; `deploy/proxmox/README.md`, `deploy/windows/README.md`.

**In-app assistant (M7)**
- `opsis/assistant/`: `providers.py` (Claude and Ollama, streamed), `loop.py` (the tool loop and its budgets), `prompt.py` (instructions and the context block), `runs.py` (requests in progress, Stop).
- `opsis/services/assistant.py` (conversations, history, entries), `opsis/api/assistant.py` (the panel's routes). Migration `0009`. `opsis/mcp/guide.py` now shares its spec reference (`SPEC_GUIDE`) with the assistant.
- Frontend: `components/dashboard/AssistantPanel.tsx`, `lib/assistant.ts` (state, the event stream, the live view), the Assistant tab in `DashboardEditor`.
- `e2e/fake-llm.mjs`, `e2e/tests/assistant.spec.ts`, `compose.fakellm.yml`.

**Connectors, schedules, backups, email (M8)**
- Connectors: `opsis/connectors/gsheets`, `opsis/connectors/salesforce`; `x-file-text` in the schema form reads a key file into a field.
- `opsis/services/`: `schedules.py` (cron, presets, the tick), `backups.py` (backup and restore), `maintenance.py` (the clean-up job), `mail.py` (SMTP and the messages), `subscriptions.py` (emailed dashboards), `model_files.py` (the models folder), `demo.py` (the demo data). Migration `0010` (`schedules`).
- `opsis/api/operations.py`: schedules, backups, the test email, emailed dashboards and the demo data. `POST /api/auth/forgot-password`. `GET|POST /api/model-files`. CLI `backup`, `restore`, `seed-demo`.
- `opsis/mcp/lenient.py`; two-hop joins in `opsis/semantic/compiler.py` with `reachable` in `opsis/semantic/model.py`.
- Frontend: `pages/Schedules`, `pages/Settings`, `components/ScheduleCard`, `components/dashboard/EmailPanel`, drag and drop in `EditorPanels`, import and export on `pages/Models`, `lib/operations.ts`.
- `e2e/fake-smtp.mjs`, `e2e/tests/operations.spec.ts`; `scripts/benchmark.mjs` times a dashboard too.

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
- ADR-018: connection forms use a small built-in schema form; visual config panels use rjsf with our own CSP-safe validator. shadcn/ui is still not added.
- ADR-019: a fourth spec file; query values are SQL literals rendered by sqlglot (approved); the model editor is still a plain text area.
- ADR-021: field wells were selectors (drag and drop came in M8, ADR-026); cross-filter and drill state live in the view (now also in the URL), not in the spec; the chart palette and mark rules are fixed in code.
- ADR-022: the MCP endpoint is stateless with JSON responses (kept at the M5 review); JSON Patch is our own code.
- ADR-023 (new, M6): `ADMIN_INITIAL_PASSWORD` is optional (Google or `reset-link` instead); Google's ID token is verified with `joserfc` (Authlib's JOSE library) and the HTTP calls use the standard library; `GOOGLE_DISCOVERY_URL` exists for testing; invitations and resets are links the admin passes on unless email is on (M8); the audit log page is available in local mode too.
- ADR-025 (new, M7): the assistant uses 17 of the 23 MCP tools (no creating, publishing or sharing from the panel); only people in the editor can use it, not access tokens; Ollama through its own chat API (`/api/chat`, with `num_ctx` on every request) rather than the OpenAI-compatible endpoint the brief names (decided at the M7 review); our own tool loop rather than the SDK's beta tool runner, so one loop serves both providers.
- ADR-026 (new, M8): Google Sheets signs in with your own OAuth client (installed-app style, with a pasted address where Google cannot come back) or a service account, through its REST API with `google-auth` for the token, not the Google API client library; emailed dashboards carry numbers and tables, not pictures of charts; SQL Lab is not in v1 (your decision); unknown non-GET `/api` paths answer 404.
- ADR-015: the task runner is npm scripts, not `tasks.py`; `httpx2` for the test client (approved), and now also for Ollama.

## Open issues and notes

- No TOTP second factor (a later item in the brief).
- "View as" shows a person's data, not whether they may open the dashboard.
- A dashboard's owner cannot be changed in the UI, and a deleted owner leaves admins and existing shares as the only way in.
- Share links show filters but no "reset filters" control; reloading without `?s=` resets them.
- Lockouts can be used to lock someone out for a while by guessing their email; they back off from one minute to an hour. The per-address limit only works if the api sees real client addresses, which depends on `TRUSTED_PROXIES` being right.
- The MCP SDK is on 2.3, a new major version, used through its low-level API.
- The live event hub lives in the single api process; several processes would need Postgres LISTEN/NOTIFY (R5).
- A right-hand axis showing counts can have decimal tick labels.
- Bundle size: main chunk 839 kB (257 kB gzipped), up from 804 kB with the M8 pages; ECharts (562 kB) and Monaco (3.4 MB) are lazy.
- Joins reach two hops at most. The query builder has one filter row.
- A repeated join key is found by comparing the values as stored: two that differ only after the join casts one side's type are not seen.
- Merge refresh does not remove rows deleted in the source (Salesforce included) until a full rebuild. One job runs at a time, so a long refresh delays a scheduled backup or email. Datasets cannot be edited after creation.
- Google Sheets refresh in full only (a sheet has no change marker). Dates in sheets must be formatted as dates to be read as dates. With an OAuth app in Testing status, Google ends the sign-in after 7 days (reconnect, or publish the app). Picking a spreadsheet from the list needs the Google Drive API enabled in the OAuth client's project.
- Emailed dashboards show numbers and short tables, not chart pictures, and use each filter's default.
- A schedule run missed while the stack was down is not made up.
- Backups on NFS need the export to let uid 110001 write (an unprivileged LXC's view of the containers' uid 10001).
- Assistant conversations are kept until their dashboard or their person is deleted; there is no clean-up or export yet.
- A request to the assistant runs in the api process (R5): a restart stops it (Stop is pressed for it, and after 10 seconds it is cancelled). The next request carries on with the conversation.
- A local model is slower than Claude: `gemma4:12b` builds a three-visual dashboard in 2.5 to 3 minutes on this laptop. After a dropped tool call it can think for several minutes, and one such build ran out of the 10-minute budget (`ASSISTANT_TIMEOUT_SECONDS`).
- Risks still open: R2 (DuckDB memory in the api process), R3 (snapshot rewrite cost), R5 (single api process), R8 (hot reload on the Windows filesystem). R10 (share links on RLS datasets) is closed: links deny by default, never inherit an exemption, and cannot exceed their maker's access.

## Backlog agreed at reviews

- **M8:** bulk export and import of a `models/` folder; two-hop joins; the 10 million row check; drag-and-drop of fields into wells; email for invitations, resets and snapshots; a clean-up job for ended sessions and links. (All done.)
- **Post-v1 (decided at M8):** SQL Lab, the admin-only read-only SQL page (`FEATURE_SQL_LAB` in the brief).
- **Later (brief):** OAuth for the MCP server so it can be a claude.ai connector; TOTP second factor.

## Questions for review

- **Q1. Emailed dashboards carry numbers, not pictures of charts** (ADR-026): a KPI's value and its comparison, the first 10 rows of each other visual with a bar beside a chart's first measure, and a link to the dashboard. Pictures would need a headless browser in the image (a few hundred MB more, and a browser to keep patched), while tables show reliably in every mail client. Is that enough for v1?

## Exact next step

Your part, then the M8 review:

1. **Salesforce, Developer Edition only.** In your Developer Edition org, create an External Client App as the README's Salesforce section describes (OAuth with the `api` scope, the Client Credentials Flow, running as a user who can read Account, Contact and Opportunity). Then add a **Salesforce** connection yourself in Opsis at http://localhost:8080 with the My Domain URL, the consumer key and the consumer secret, press **Test** (it should name a Developer Edition org), save it, and tell me its name. I will confirm the edition before reading anything, then test discovery, a full refresh and a merge refresh. I will not type the key or the secret myself.
2. **Google Sheets with your own Google account.** No service account is needed. In the Google Cloud project of the OAuth client your Python scripts use, enable the Google Sheets API and the Google Drive API. Then add a **Google Sheets** connection in Opsis at http://localhost:8080: sign in with **My Google account (OAuth)**, load your `credentials.json`, save, and choose **Connect**. Google comes straight back to Opsis on the laptop. Pick a spreadsheet (a few tabs, with numbers, dates and text is best) and tell me the connection's name. I will then test discovery, the column types, a refresh and a reconnect. If your OAuth app is in Testing status, its sign-in will last 7 days.
3. **The install check on Proxmox:** follow `deploy/proxmox/README.md` from section 1, then its section 11 checklist, which includes the Google sign-in checklist in section 3. Tell me anything that fails, and which address the audit log shows for your sign-in.
