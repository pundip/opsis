# Progress

Last updated: 2026-11-19 (end of Milestone 6)

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
| M6 Auth and sharing | Done, awaiting review |
| M7 In-app assistant | Not started |
| M8 More connectors and hardening | Not started |

## M6 Auth and sharing

Built (decisions in ADR-023):

- **From the M5 review, done first:** dataset previews (REST and MCP) and the sample values in `describe_dataset` now read through the same row-level security filter as queries, and leave out the total row count when a rule applies; a rule that cannot be applied refuses. Access tokens work on the REST API with the MCP scopes.
- **Sign-in** (`AUTH_MODE=local` and `full`): argon2id passwords, server-side sessions (idle and absolute timeouts, sign out, sign out everywhere, all other sessions end on a password change), the first admin from `ADMIN_EMAIL` / `ADMIN_INITIAL_PASSWORD` with a forced password change, lockouts with backoff counted in Postgres, and `python -m opsis reset-link <email>` for recovery.
- **Sign in with Google:** PKCE, state and nonce, ID token verified against Google's keys, verified email only, domain and address allowlists, invited-only by default, linking to an existing account by verified email, optional auto-provisioning as a viewer.
- **People and groups:** invitations and password resets as one-time links (shown once to the admin), roles, attributes for row-level security, deactivation (ends sessions and tokens), groups; the last admin cannot be removed.
- **Access lists:** dashboards are shared with users, groups or everyone as viewers (published version only, title included) or editors; only owners and admins delete; a dashboard you cannot see answers 404. Datasets can be restricted to admins and named users or groups. The MCP tools follow the same rules.
- **Share links** (server mode, flag): published version only, optional expiry and password, revocable, view count; row-level security shows nothing through a link unless its maker chose otherwise (only admins may choose any attributes); a link never inherits an exemption.
- **View as** (admins): see a dashboard's data as a given person, or as a viewer with given attributes.
- **View state in the URL:** filters, cross-filters and drill levels (`?s=`), on dashboards and share links.
- **Audit log screen** for admins, with filters and paging; sign-ins (and failures), people, groups, sharing, links and dataset access are now recorded too.
- **MCP:** `share_dashboard` (sign-in modes) and `create_share_link` (flag), 23 tools in all.
- **Deploy:** `deploy/proxmox/README.md` (LXC, Docker, `.env`, Nginx and Caddy configurations, firewalling, MCP clients, backups and restore, upgrades, recovery, troubleshooting) and `deploy/windows/README.md`. `cloudflared` pinned to 2026.11.1.
- **Frontend:** sign-in page, set-password page, account page, Users (people and groups), Audit log, the Share panel with public links, the dataset access panel, View as, and the public share-link page.

### How M6 was verified

- **Backend, automated:** 231 pytest tests pass (12 new in `tests/test_auth.py`); `ruff check`, `ruff format --check` and `mypy --strict` pass. The new tests cover:
  - settings: the new fail-fast rules, and no secret echoed in an error;
  - sign-in: same message for an unknown email and a wrong password; cookie attributes; the forced password change; a password change ending other sessions; sign out and sign out everywhere; idle and absolute timeouts; lockout after five failures and the backoff after that;
  - invitations, single-use and expiring links, the last-admin rules, groups, deactivation ending sessions and tokens, the recovery command; the built-in admin and its tokens stopping when sign-in is switched on;
  - dashboard access lists through users, groups and everyone, including a viewer shared as an editor, published-only titles, and delete rights;
  - restricted datasets across the dataset, model, query and validation routes, and still viewable through a shared dashboard;
  - access token scopes on the REST API; tokens unable to make tokens; a wrong token never falling back to a cookie;
  - share links: published only, RLS denying by default, the attribute rules, never exempt, password with lockout, revoke, expiry, unpublish, cookie attributes under https and `/opsis`;
  - View as; Sign in with Google against a fake provider that issues real RS256 tokens and checks PKCE: allowed, not allowed, not invited, unverified, nonce and audience mismatches, a forged state, a callback in another browser, auto-provisioning; the audit log API; and that local mode registers none of these routes.
- **Frontend, automated:** `tsc`, ESLint and 42 Vitest tests pass (two new, for view state in the URL).
- **Browser, automated with `npm run smoke`, all passed:**
  - **at http://localhost:8080 and at http://localhost:8088/opsis/** (AUTH_MODE=none): the M4 dashboard flow, the MCP flow with live editor updates, the token page, and a new test that local mode has no sign-in, people, sharing or share links;
  - **in server mode behind the proxytest nginx at http://localhost:8088/opsis/**, on a throwaway stack (DEPLOY_MODE=server, AUTH_MODE=full, MCP_AUTH=token, the worker container, share links on, fresh random secrets, removed afterwards): the admin signs in and changes the initial password; the M4 dashboard flow; **the MCP flow with an access token through `/opsis/mcp`, and live updates through nginx** (as asked at the M5 review); an admin invites a viewer from the Users page, who sets a password from the link, sees nothing until the dashboard is shared, then sees it with no Edit or Share, cannot open the editor, and gets 403 from the API; a share link shows the published version (not a saved draft) and stops working when revoked; **Sign in with Google works for an invited, allowed address and is refused for another domain**, against `e2e/fake-oidc.mjs`; a script's read-only token can read and cannot write; a request without credentials gets 401.
- **By hand:** the bundled `caddy` profile in plain-HTTP mode on spare ports (app, API and health check through Caddy, security headers intact); the `tunnel` profile's configuration validates.

Bugs found by this testing and fixed: an access token could create more access tokens (now only a signed-in person can); the invitation and share-link pages read their token outside a route, so they asked the API for an empty token; viewers could see the draft's title in the dashboard list.

Not verified:

- **Real Google sign-in** at https://urlloom.com/opsis. It needs your Google OAuth client; the flow was tested against a fake provider, in pytest and in the browser.
- **Your real proxy, the LXC, port forwarding and https.** The Nginx block is the one the proxytest profile runs; the Caddy block for an existing Caddy was not run. `TRUSTED_PROXIES` behind your port forwarding still needs checking on the server (the Proxmox guide says how).
- The bundled Caddy's automatic HTTPS (needs a public domain) and the Cloudflare tunnel (needs an account and token).
- The Claude Desktop `mcp-remote` configuration with a token header, as written in the Proxmox guide.
- Long sessions in practice (the timeouts are tested by moving timestamps), and many people at once.
- The Sonnet results for the MCP tools: the review message still had the placeholder, so I have nothing to act on there yet.

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

**Sign-in and sharing (M6)**
- `opsis/auth/`: `passwords.py` (argon2id), `google.py` (OpenID Connect), `principal.py` (groups, scopes, session).
- `opsis/services/`: `accounts.py` (first admin, invitations, password sign-in, Google matching, admin changes), `sessions.py`, `lockout.py`, `access.py` (dashboard and dataset access, sharing, groups), `links.py` (share links), `crypto.py` (sealed cookies).
- `opsis/api/`: `auth.py`, `admin.py` (users, groups, audit), `sharing.py` (access lists, links, the public side); `deps.py` (sessions, tokens, scopes). Migration `0007`. CLI `reset-link`.
- Frontend: `pages/Login`, `SetPassword`, `Account`, `Users`, `Audit`, `PublicDashboard`; `components/dashboard/SharePanel`, `components/DatasetAccessPanel`; `lib/access.ts`, `lib/urlState.ts`.
- `e2e/auth.spec.ts`, `e2e/local.spec.ts`, `e2e/global-setup.ts`, `e2e/fake-oidc.mjs`, `compose.servertest.yml`; `deploy/proxmox/README.md`, `deploy/windows/README.md`.

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
- ADR-021: field wells are selectors (accepted for v1); cross-filter and drill state live in the view (now also in the URL), not in the spec; the chart palette and mark rules are fixed in code.
- ADR-022: the MCP endpoint is stateless with JSON responses (kept at the M5 review); JSON Patch is our own code.
- ADR-023 (new, M6): `ADMIN_INITIAL_PASSWORD` is optional (Google or `reset-link` instead); Google's ID token is verified with `joserfc` (Authlib's JOSE library) and the HTTP calls use the standard library; `GOOGLE_DISCOVERY_URL` exists for testing; invitations and resets are links the admin passes on until email arrives (M8); the audit log page is available in local mode too.
- ADR-015: the task runner is npm scripts, not `tasks.py`; `httpx2` for the test client (approved).

## Open issues and notes

- No email yet (`FEATURE_EMAIL`, M8): invitation and reset links are shown to the admin to pass on.
- No TOTP second factor (a later item in the brief).
- "View as" shows a person's data, not whether they may open the dashboard.
- A dashboard's owner cannot be changed in the UI, and a deleted owner leaves admins and existing shares as the only way in.
- Share links show filters but no "reset filters" control; reloading without `?s=` resets them.
- Sessions and links are kept as rows after they end; there is no clean-up job yet (M8, with backups).
- Lockouts can be used to lock someone out for a while by guessing their email; they back off from one minute to an hour. The per-address limit only works if the api sees real client addresses, which depends on `TRUSTED_PROXIES` being right.
- The MCP SDK is on 2.3, a new major version, used through its low-level API.
- The live event hub lives in the single api process; several processes would need Postgres LISTEN/NOTIFY (R5).
- A right-hand axis showing counts can have decimal tick labels.
- Bundle size: main chunk 804 kB (248 kB gzipped), up from 755 kB with the M6 pages; ECharts (562 kB) and Monaco (3.4 MB) are lazy.
- Joins are one hop (two-hop: M8). The query builder has one filter row.
- Merge refresh does not remove rows deleted in the source. One job runs at a time. Datasets cannot be edited after creation.
- The `ollama` image is on `latest` until M7.
- Risks still open: R2 (DuckDB memory in the api process), R3 (snapshot rewrite cost), R5 (single api process), R8 (hot reload on the Windows filesystem). R10 (share links on RLS datasets) is closed: links deny by default, never inherit an exemption, and cannot exceed their maker's access.

## Backlog agreed at reviews

- **M8:** bulk export and import of a `models/` folder; two-hop joins; the 10 million row check; drag-and-drop of fields into wells; email for invitations, resets and snapshots; a clean-up job for ended sessions and links.
- **Later (brief):** OAuth for the MCP server so it can be a claude.ai connector; TOTP second factor.

## Questions for review

- **Q1. Who may restrict datasets.** Only admins can restrict a dataset or change its grants. Should the editor who created a dataset be able to as well?
- **Q2. Editors and sharing.** Anyone who can edit a dashboard can also change who it is shared with (as in Google Docs). Do you want that limited to the owner and admins?
- **Q3. Sonnet results.** Your M5 review message still had the placeholder for the Sonnet test results, so I had nothing to act on. If anything struggled, send the tool call and the error text it got.
- **Q4. Google on the server.** When the OAuth client exists, the first real Google sign-in on https://urlloom.com/opsis is the last unchecked item of M6's "done when". Do you want to do it at review, or shall I add a short checklist to the Proxmox guide and treat it as part of the M8 install check?

## Exact next step

After review, start **M7 (In-app assistant)**. Read the M7 section of BUILD_BRIEF.md first. In outline:

1. A provider abstraction with `anthropic` (Messages API with tool use) and `ollama` (its OpenAI-compatible endpoint), configured by `ASSISTANT_PROVIDER`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `OLLAMA_URL` and `OLLAMA_MODEL`, with fail-fast checks.
2. A tool loop over the same tool functions as MCP (`opsis/mcp/tools.py`), running as the signed-in person with their access, with budgets (steps, tokens, time) and versions marked as the assistant's.
3. A chat panel in the editor: streaming over server-sent events, each tool call shown as a collapsible step, one-click undo of every change, and context (the dashboard, the selected visual, the datasets).
4. Done when the same "build a dashboard" request works from the panel with each provider; Playwright tests at the root and under /opsis, and in server mode.
