# Progress

Last updated: 2026-11-20 (end of Milestone 7)

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
| M6 Auth and sharing | Done, reviewed; review changes done at the start of M7 |
| M7 In-app assistant | Done, awaiting review |
| M8 More connectors and hardening | Not started |

## M7 In-app assistant

**From the M6 review, done first** (ADR-024):

- **Dataset owners:** the editor who created a dataset can restrict it and change its list, admins can do so for any dataset, and a restriction an admin set can only be lifted by an admin. Restrictions made in M6 count as admin-set.
- **Sharing** (people, groups, everyone, share links) is for the dashboard's owner and admins only, in the UI, the REST API and the MCP tools. Editors still edit content.
- **Restricted data through dashboards:** only the dataset's owner or an admin can show a restricted dataset's data to people outside its list, whether by sharing, by a share link, by letting outside editors work on a draft that uses it, or by publishing it to an audience that includes outsiders. The share dialog says which datasets are restricted and who would gain access, and blocks what the person may not do; the dataset access panel lists the dashboards that already show its data outside its list. The MCP sharing tools follow the same rules. Cross-filters can only use fields the dashboard uses, so a viewer cannot probe other fields.
- **Google sign-in:** a checklist in `deploy/proxmox/README.md`, part of the M8 install check.

**Built in M7** (ADR-025):

- **Providers:** Claude through the official `anthropic` SDK (2.11, API version 2026-10-21), and Ollama through its OpenAI-compatible endpoint. Claude runs with adaptive thinking, progress notes between tool calls (`display: "updates"`), effort from `ANTHROPIC_EFFORT`, prompt caching, and eager tool-input streaming with every input checked against its schema. **Refusal fallbacks are on by default** (`fallbacks: "default"`): a request Claude's safety checks decline is re-run on the model Anthropic recommends for it, and the panel says which model answered. `ANTHROPIC_FALLBACKS=false` turns that off.
- **Tool loop** over 17 of the MCP tools, as the signed-in person with their access, with budgets: tool calls, output tokens, time, and rows shown to the model. It can change only the open dashboard, and cannot create, publish or share. Token use per request is logged, audited (`assistant.request`) and shown in the panel.
- **Conversations** per person per dashboard, in Postgres (migration 0009): the provider's history byte for byte, and the panel's entries. A request runs on its own, so closing the panel does not stop it; **Stop** does.
- **The Assistant tab** in the editor: streaming replies, progress notes, each tool call as a step you can open, **Undo** on each change and **Undo all of this** per request, Stop, New conversation, and the selected visual as context. Send waits until your own changes are saved. The editor reloads the assistant's versions as they land, without its banner while the tab is open. The disabled Assistant button in the header is gone.
- **Settings:** `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (default `claude-opus-5-5`), `ANTHROPIC_EFFORT`, `ANTHROPIC_FALLBACKS`, `OLLAMA_URL`, `OLLAMA_MODEL` (default `gemma4:12b`), `ASSISTANT_MAX_STEPS`, `ASSISTANT_MAX_OUTPUT_TOKENS`, `ASSISTANT_TIMEOUT_SECONDS`, `ASSISTANT_MAX_ROWS`. `ASSISTANT_PROVIDER=anthropic` without a key fails fast. The `ollama` Compose image is pinned (0.40.16) and given a 32k context.
- **Ollama's context:** its OpenAI-compatible endpoint cannot set the context size, and its default (4,096 tokens on this laptop) cuts off the assistant's instructions and tools. The assistant detects that and says how to fix it (`OLLAMA_CONTEXT_LENGTH=32768`); the README and the Windows guide say so too.
- **Small models:** gemma4 12B writes long nested tool calls that Ollama cannot convert to JSON, and Ollama then drops the call (its log says "tool call parsing failed"). So with Ollama the assistant asks for one dashboard item per call, and a reply that is empty or only announces a step is answered with a request to make the call again, at most twice a request, with a note in the panel.
- **Tests:** a scripted fake model (`e2e/fake-llm.mjs`) streams like both APIs and refuses what the real Claude API refuses (forced tool use, thinking turned off, sampling parameters). The smoke run plugs it in at the root and the sub-path (`compose.fakellm.yml`, Ollama's API) and in server mode (Claude's API), then puts your stack back as `.env` has it.

### How M7 was verified

- **Backend, automated:** 252 pytest tests pass (20 new in `tests/test_assistant.py`; `tests/test_review_m6.py` and updated M6 tests cover the review changes); `ruff check`, `ruff format --check` and `mypy --strict` pass. The assistant tests run both providers through their real clients against scripted fake servers (the Anthropic SDK parses the fake's event stream) and cover: building a dashboard step by step; what Claude is sent (adaptive thinking with updates, effort, fallbacks, cache breakpoints, sorted tools with eager streaming, no sampling parameters or forced tool choice, the beta headers); the history sent back append-only, with Claude's thinking blocks and signatures unchanged; follow-ups that see the whole conversation; token totals and the audit; refusals (nothing kept), a fallback in mid-reply (the declined part not sent back), API errors; tool calls cut off at max_tokens (answered, not run); the step budget; calls on another dashboard, unreadable arguments and unknown tools; Ollama's context check; Stop (keeps the partial text) and one request at a time; empty replies and replies whose tool call was dropped; template tokens a local model leaks; who may use it (viewers, other people's conversations, access tokens, the flag off); row and length caps; the context block; settings that fail fast.
- **Frontend, automated:** `tsc`, ESLint and 48 Vitest tests pass (3 new, for the event stream and the live view).
- **Browser, automated with `npm run smoke`, all passed** at http://localhost:8080, at http://localhost:8088/opsis/ behind the proxytest nginx, and in server mode behind it (sign-in, the worker, and the assistant on Claude's API). The new assistant test asks the fake model, from the panel, to build a dashboard from a fresh dataset: the reply streams, Stop shows while it works, three steps appear (one opened to show its input), the canvas follows along without a banner, Undo restores the empty dashboard as version 3, the history reads user, assistant, user, and after a reload the conversation and its token line are still there, with no script errors or CSP violations. At the root and /opsis this goes through the Ollama provider, in server mode through Claude's, whose progress note shows too. The earlier tests (dashboards, MCP with live updates, tokens, local mode; in server mode sign-in, sharing, share links, Google and tokens) pass with the M6 review changes in.
- **With a real model, Ollama:** `gemma4:12b` on this laptop (through a 32k-context copy of it, see Q4), asked through the panel's API to "Build a dashboard of giving from the gifts dataset: the total amount, the amount by fund, and the amount by month". It described the dataset, listed the visuals, then added a KPI card, a bar chart by fund and a line chart by month, one per call. Its first line chart was refused by validation (the x binding was not a list) and it fixed that from the error message. 7 model calls, 6 tool calls, 5 min 37 s, 49,553 input tokens (40,814 of them cached) and 2,583 output tokens; each visual returns data, and the history shows three versions by the assistant with its notes. Before the one-item-per-call change, two runs stopped at that step: Ollama's log showed it had dropped the model's long `patch_dashboard` call ("gemma4 tool call parsing failed"). So with this model a build works, slowly, but not every time.
- **With a real model, Claude:** not run. There is no `ANTHROPIC_API_KEY` here, so Claude was exercised only through the fake (in pytest and in the server-mode browser run). See Q2.

Not verified:

- The real Claude API (no key), so also not the fallbacks, progress notes and prompt caching against the live service, or how well Claude builds dashboards with these tools.
- The Sonnet run of the MCP tools (not run yet, as you said).
- The `ollama` Compose profile itself (the pinned image and its context setting were not started).
- Long conversations near a model's context limit with real models, and several people using the assistant at once.

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

**In-app assistant (M7)**
- `opsis/assistant/`: `providers.py` (Claude and Ollama, streamed), `loop.py` (the tool loop and its budgets), `prompt.py` (instructions and the context block), `runs.py` (requests in progress, Stop).
- `opsis/services/assistant.py` (conversations, history, entries), `opsis/api/assistant.py` (the panel's routes). Migration `0009`. `opsis/mcp/guide.py` now shares its spec reference (`SPEC_GUIDE`) with the assistant.
- Frontend: `components/dashboard/AssistantPanel.tsx`, `lib/assistant.ts` (state, the event stream, the live view), the Assistant tab in `DashboardEditor`.
- `e2e/fake-llm.mjs`, `e2e/tests/assistant.spec.ts`, `compose.fakellm.yml`.

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
- ADR-025 (new, M7): the assistant uses 17 of the 23 MCP tools (no creating, publishing or sharing from the panel); only people in the editor can use it, not access tokens; Ollama needs `OLLAMA_CONTEXT_LENGTH` because its OpenAI-compatible endpoint cannot set the context size; our own tool loop rather than the SDK's beta tool runner, so one loop serves both providers.
- ADR-015: the task runner is npm scripts, not `tasks.py`; `httpx2` for the test client (approved), and now also for Ollama.

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
- Assistant conversations are kept until their dashboard or their person is deleted; there is no clean-up or export yet.
- A request to the assistant runs in the api process (R5): a restart stops it (Stop is pressed for it, and after 10 seconds it is cancelled). The next request carries on with the conversation.
- A local model is slow on this laptop (see the Ollama result above), so `ASSISTANT_TIMEOUT_SECONDS` may need raising for local models.
- Risks still open: R2 (DuckDB memory in the api process), R3 (snapshot rewrite cost), R5 (single api process), R8 (hot reload on the Windows filesystem). R10 (share links on RLS datasets) is closed: links deny by default, never inherit an exemption, and cannot exceed their maker's access.

## Backlog agreed at reviews

- **M8:** bulk export and import of a `models/` folder; two-hop joins; the 10 million row check; drag-and-drop of fields into wells; email for invitations, resets and snapshots; a clean-up job for ended sessions and links.
- **Later (brief):** OAuth for the MCP server so it can be a claude.ai connector; TOTP second factor.

## Questions for review

- **Q1. Lists of admin-set restrictions.** As built, an admin's restriction can only be lifted by an admin, but the dataset's owner can still change who is on its list (your answer, read literally). Should an admin-set restriction be admin-only entirely, list included?
- **Q2. Claude for real.** I could not run the assistant against the real Claude API. To check it, put a key in `.env` yourself (`ASSISTANT_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=...`) and tell me, and I will run the same "build a dashboard" check with it. Please do not paste the key into the chat.
- **Q3. Ollama's API.** The brief names Ollama's OpenAI-compatible endpoint, which cannot set the context size, so Ollama must run with `OLLAMA_CONTEXT_LENGTH=32768` or more. Ollama's own API (`/api/chat`) can set it per request and would work with a default install. Keep the OpenAI-compatible endpoint (as now), or switch?
- **Q4. The model alias in your Ollama.** To test with a 32k context without changing how your Ollama runs, I created `gemma4-opsis-32k`, a copy of `gemma4:12b` with a larger context (no download; `ollama rm gemma4-opsis-32k` removes it). Your `.env` is unchanged, so the assistant there still uses `gemma4:12b` and will say the context is too small until you set `OLLAMA_CONTEXT_LENGTH` or `OLLAMA_MODEL=gemma4-opsis-32k`. Which do you prefer?
- **Q5. Refusal fallbacks** are on by default for Claude (a declined request is re-run on the model Anthropic recommends, and the panel says so). Keep them on?

## Exact next step

After review, start **M8 (More connectors and hardening)**. Read the M8 section of BUILD_BRIEF.md first. In outline:

1. Google Sheets and Salesforce (incremental) connectors.
2. The backup job with a restore document; the scheduled refresh UI with cron presets; email behind `FEATURE_EMAIL` (invitations, resets, snapshots).
3. The performance check: a typical dashboard on a 10 million row dataset loads in under 1.5 s warm.
4. Full Windows and Proxmox documentation, a demo seed dataset, and the install check on each target from the README alone, including the Google sign-in checklist.
5. The M8 backlog agreed at reviews: model export and import, two-hop joins, drag-and-drop fields, a clean-up job for ended sessions and links.
