# Build brief: Opsis, a self-hosted, AI-native BI platform

> How to use: put this file and `CLAUDE.md` in the root of an empty git repo, open the folder in the Claude desktop app's Code tab (local session, Worktree off), choose Fable in the model picker, then start with:
> "Read BUILD_BRIEF.md and complete Milestone 0 only."

---

## 0. Your role and how to work

You are the lead engineer building a self-hosted, AI-native business intelligence platform. Think Power BI, but open source, Docker-based, and designed so that an AI agent and a human edit the same dashboards. You will build it end to end in this repository.

Working rules:

1. **One milestone at a time** (section 14). At the end of each milestone, stop and report: what you built, how to run it, how you verified it, and known gaps. Wait for my go-ahead before starting the next one.
2. **Keep `PROGRESS.md` current.** Update it at the end of every milestone and before any long pause: milestone status, decisions made, open issues, and the exact next step. A fresh session must be able to resume from `PROGRESS.md` plus this brief, without re-reading the whole codebase.
3. **Log decisions** in `docs/DECISIONS.md` as short ADR entries. If you deviate from this brief, record why.
4. **Files:** if you are working directly in the repo with file-editing tools, edit in place. If you are handing me code to copy, always give complete files, never partial patches or "rest unchanged" placeholders.
5. **Be economical.** Don't re-read files you just wrote, don't echo large files back to me, don't gold-plate. Prefer boring, well-known libraries. Ask me only when genuinely blocked; otherwise make a reasonable choice and log it.
6. **Verify as you go.** Run the tests, run `docker compose up`, call the endpoints, and check UI changes in the desktop app's Browser pane. Never say something works unless you ran it.
7. **Git:** at the end of each milestone, commit with a clear message and push to `origin`. Never commit `.env` or any secrets.
8. **Style:** never use em dashes in anything you write: docs, code comments, UI text, or messages to me.

---

## 1. Product summary

The product is called **Opsis** (Greek for "sight" or "seeing"). Use `opsis` for the Python package, Docker images, the Compose project name, the MCP server name and folders. User-facing branding (name, logo, colours) comes from `branding.json` plus an `APP_NAME` env var (default `Opsis`), so a rebrand later is a config change, not a code change.

What it does:

- Connects to data sources through **pluggable connectors** and pulls data on a schedule into a local analytical store. Dashboards never query the source live.
- Has a **semantic model** (dimensions, measures, joins, row-level security) defined once and reused everywhere.
- Lets users build dashboards in a **drag-and-drop editor or by asking an AI agent**. Both edit the same versioned JSON spec.
- Has **pluggable visuals**. Adding a visual or connector means adding a folder, not changing core code.
- Supports **publishing and sharing**: draft vs published, sharing with users and groups, and public share links (server mode only).
- Integrates AI through an **MCP server** (Claude Code, Claude Desktop, claude.ai) and an optional **in-app assistant** (Anthropic API or local Ollama).
- Runs identically on a **Windows laptop (Docker Desktop)** and a **Proxmox Ubuntu LXC server**, with features toggled by config.

Primary user: a data operations manager, strong in Python and SQL, whose main sources are Salesforce, MySQL, Google Sheets and CSV/Excel files.

---

## 2. Hard constraints

- **Free and open source only**, except the AI provider. No paid services, paid tiers or cloud dependencies.
- **Licences:** prefer MIT, Apache 2.0, BSD or PostgreSQL licence. No AGPL dependencies in core, so the product can be distributed or sold later.
- **Hardware:** Intel x86_64 on both targets. Single-arch amd64 images are fine.
- **Targets:**
  - (a) Windows 11 laptop, Docker Desktop with the WSL2 backend.
  - (b) Ubuntu 24.04 LXC on Proxmox VE, running Docker Engine with nesting enabled.
- **One codebase, one `compose.yml`, the same images.** All differences come from `.env`, Compose profiles and feature flags.
- **Offline-capable on the laptop**, apart from calls to a remote AI provider or remote data sources.

---

## 3. Deployment modes and feature flags

`DEPLOY_MODE=local|server`. The repo ships `.env.local.example` and `.env.server.example`. The user copies one to `.env` (gitignored). `COMPOSE_PROFILES` is set inside it, so the start command is always `docker compose up -d`.

| Variable | Purpose | Local default | Server default |
|---|---|---|---|
| `AUTH_MODE` | `none` / `local` / `full` (local accounts + Google) | `none` | `full` |
| `JOB_RUNNER` | `inline` (inside api) / `worker` (separate container) | `inline` | `worker` |
| `FEATURE_SCHEDULED_REFRESH` | Cron-based refresh schedules | `false` | `true` |
| `FEATURE_PUBLIC_SHARE_LINKS` | Tokenised public read-only links | `false` | `true` |
| `FEATURE_EMAIL` | SMTP invites, resets, scheduled snapshots | `false` | `false` (opt-in) |
| `FEATURE_ASSISTANT` | In-app AI panel | `true` | `true` |
| `ASSISTANT_PROVIDER` | `anthropic` / `ollama` | `ollama` | `anthropic` |
| `MCP_AUTH` | `none` (only when bound to 127.0.0.1) / `token` | `none` | `token` |
| `FEATURE_SQL_LAB` | Admin-only read-only raw SQL | `true` | `false` |
| `FEATURE_BACKUPS` | Nightly backup job | `false` | `true` |
| `BIND_ADDRESS` | Host interface for the published port | `127.0.0.1` | the LXC's LAN IP (reached by my reverse proxy) |
| `PUBLIC_BASE_URL` | The externally visible URL. The app derives its base path from it (empty locally, `/opsis` on the server). Used for routing, OAuth redirects, share links and MCP URLs | `http://localhost:8080` | `https://urlloom.com/opsis` |
| `TRUSTED_PROXIES` | IPs allowed to set `X-Forwarded-*` headers | (empty) | my reverse proxy's IP |

Rules:

- A typed settings module (pydantic-settings) parses flags once at startup. **Invalid combinations fail fast** with a clear message, for example `AUTH_MODE=none` with a non-loopback bind, public share links with `AUTH_MODE=none`, or an `https` `PUBLIC_BASE_URL` with no `TRUSTED_PROXIES` set.
- **Flags are enforced in the backend** (routes return 404 when a feature is off), not just hidden in the UI.
- `GET /api/config` returns the non-secret flags. The frontend uses it to show or hide features.
- The MCP server **only registers tools for enabled features**.

Compose profiles:

| Profile | Services |
|---|---|
| (none) | `api`, `postgres` |
| `server` | adds `worker` |
| `caddy` | adds a bundled Caddy proxy, only for installs without their own reverse proxy (not used on my server) |
| `tunnel` | adds `cloudflared` (optional) |
| `ollama` | adds an `ollama` container (optional; on Windows, Ollama runs natively on the host instead) |
| `proxytest` | adds an nginx container that mimics the server's sub-path setup on the laptop (see 3.1) |

### 3.1 Hosting at a sub-path behind my existing reverse proxy

On the server, Opsis is served at **`https://urlloom.com/opsis`** through the reverse proxy I already use for other LXC projects on the same domain. That proxy terminates TLS and forwards to the LXC. Getting sub-path hosting right is a first-class requirement from Milestone 1, not a deployment afterthought.

**One setting drives everything.**
- `PUBLIC_BASE_URL=https://urlloom.com/opsis`. The app derives `BASE_PATH=/opsis` from it.
- Locally the base path is empty and Opsis runs at the root.
- No URL or path is ever hard-coded.

**The app owns the prefix.**
- Every route is mounted under `BASE_PATH`: UI, `/api`, `/mcp`, share links and health checks.
- The proxy forwards the path unchanged (no prefix stripping), so behaviour doesn't depend on proxy quirks.
- Requests outside `BASE_PATH` return 404.

**The frontend base path is set at runtime, not build time**, because the same image runs at `/` and at `/opsis`.
- Build with Vite `base: './'`.
- The backend serves `index.html` with an injected `<base href="{BASE_PATH}/">` and a JSON config block, for example `<script id="opsis-config" type="application/json">`, that the bundle reads at startup.
- React Router's `basename`, the API client and SSE URLs all come from that config.
- Deep links such as `/opsis/dashboards/42` and browser refresh must work, with the SPA fallback active only inside `BASE_PATH`.

**Isolation on a shared domain.** Other apps share the `urlloom.com` origin, so:
- Cookies use `Path=/opsis`, and their names are prefixed `opsis_` (session, CSRF), so they never collide with or leak to other apps.
- Auth tokens live only in HttpOnly cookies, never in localStorage or sessionStorage. Any non-sensitive localStorage keys are prefixed `opsis:`.
- A strict CSP with no inline scripts; the injected config is a JSON data block, not executable script.
- The app does not send HSTS when behind an external proxy, because HSTS applies to the whole domain and is the proxy's decision.

**Forwarded headers.**
- Run Uvicorn with proxy headers enabled, trusting only `TRUSTED_PROXIES`.
- The app then sees `https` (for Secure cookies and correct redirect URIs) and the real client IP (for rate limiting and the audit log).
- Forwarded headers from any other IP are ignored.

**Streaming through the proxy.**
- SSE (live editor updates) and MCP Streamable HTTP must survive the proxy.
- The app sends `X-Accel-Buffering: no` and a keep-alive comment every 15 s.
- The docs list the proxy settings: buffering off and a long read timeout.

**Uploads.** The app has a configurable upload limit (default 200 MB), and the docs remind me to raise the proxy's request body limit to match.

**Resulting URLs**, all derived from `PUBLIC_BASE_URL`:

| Purpose | URL |
|---|---|
| App | `https://urlloom.com/opsis/` |
| Google OAuth redirect URI | `https://urlloom.com/opsis/api/auth/google/callback` |
| Google authorised JavaScript origin | `https://urlloom.com` |
| MCP endpoint | `https://urlloom.com/opsis/mcp` |
| Share links | `https://urlloom.com/opsis/s/<token>` |
| Health check | `https://urlloom.com/opsis/healthz` |

**MCP OAuth (later milestone).** OAuth discovery documents normally sit at the domain root (`/.well-known/...`). Use the path-aware form, for example `/.well-known/oauth-protected-resource/opsis/mcp`, and document the extra proxy rule that routes it to Opsis.

**Proxy docs.** `deploy/proxmox/README.md` includes ready-to-paste configs that preserve the prefix:
- **Nginx (including a Nginx Proxy Manager custom location):**
  - `location /opsis/ { proxy_pass http://<lxc-ip>:8080; ... }`. Note there is no URI part on `proxy_pass`, so the prefix is kept.
  - Add `location = /opsis { return 301 /opsis/; }`, plus the forwarded headers, `proxy_buffering off` and `proxy_read_timeout` for streaming, and `client_max_body_size`.
- **Caddy:** `redir /opsis /opsis/` and `handle /opsis/* { reverse_proxy <lxc-ip>:8080 }`. Use `handle`, not `handle_path`, which would strip the prefix.

**Test it on the laptop.** The `proxytest` profile runs nginx with the same config, serving Opsis at `http://localhost:8088/opsis/` and a dummy page at `/`. Sub-path behaviour, cookies and streaming can then be verified locally, including in the desktop app's Browser pane, before deploying.

---

## 4. Architecture

Services:

- **`api`**: FastAPI app on Python 3.12. Serves everything under `BASE_PATH` (see 3.1): the REST API at `{BASE_PATH}/api`, the MCP server at `{BASE_PATH}/mcp`, and the built frontend as static files. Runs jobs in-process when `JOB_RUNNER=inline`.
- **`worker`** (`server` profile): same image, different command. Runs refresh jobs, schedules, backups and emails. The job queue is a Postgres table using `SELECT ... FOR UPDATE SKIP LOCKED`. **No Redis.**
- **`postgres`**: Postgres 16, used in both modes for parity. Holds metadata only: users, sessions, connections, datasets, models, dashboards, versions, shares, audit log and jobs.
- **`caddy`** (`caddy` profile, optional): a bundled reverse proxy with TLS for installs that don't already have one. My server doesn't use it; my existing proxy fronts Opsis (see 3.1).
- **`cloudflared`** (`tunnel` profile): optional Cloudflare Tunnel for external access.

Analytical storage and query:

- **Parquet snapshots.** Each dataset refresh writes an immutable snapshot to `/data/datasets/<dataset_id>/<snapshot_id>/`, and Postgres points to the current one. Swapping is an atomic pointer update, so readers never see partial data. Old snapshots are pruned by a retention setting (keep N).
  - Rationale: this avoids DuckDB single-writer lock problems, works well on Windows, and gives refresh history for free.
- **DuckDB, in-process.** Queries run inside `api` and `worker` against the current snapshot's Parquet files.
- **`QueryEngine` interface.** The engine sits behind an interface so it can later be swapped (for example Cube or ClickHouse) without touching the API.
- **Result cache.** In-process LRU keyed by (compiled SQL hash, snapshot ids, RLS context). A refresh changes the snapshot id, so cache invalidation is automatic.
- **Named Docker volumes** for `/data` and Postgres. No bind mounts for data (slow and unreliable on Windows).

---

## 5. Tech stack

Fixed unless you log a reason in `DECISIONS.md`. Use current stable versions, pinned in lockfiles.

- **Backend:** Python 3.12, uv, FastAPI, Uvicorn, SQLAlchemy 2, Alembic, Pydantic v2, pydantic-settings, DuckDB, PyArrow, sqlglot (SQL generation and validation), jsonschema, Authlib (Google OIDC), argon2-cffi, cryptography (Fernet for connector secrets), croniter, httpx, and the official MCP Python SDK. Tooling: pytest, ruff, mypy.
- **Frontend:** React with TypeScript (strict), Vite, TanStack Query, React Router, react-grid-layout, Apache ECharts (echarts-for-react), Tailwind CSS with shadcn/ui, react-jsonschema-form (@rjsf) for config panels generated from schemas, Monaco editor for the JSON spec and SQL Lab, and ajv. Tests: Vitest, plus one Playwright smoke test.
- **Connector libraries:** psycopg, PyMySQL, simple-salesforce, the Google Sheets API client, and openpyxl. A connector may wrap a dlt source internally if that saves work, but the plugin interface is ours.

---

## 6. Semantic layer

**Storage.** One model per dataset, plus cross-dataset joins. Models are stored in Postgres, editable in the UI and through MCP, and importable/exportable as YAML (`models/*.yml`) for version control.

**Elements:**
- **Dimensions:** a column or SQL expression, a type, and time grains for dates.
- **Measures:** `sum`, `count`, `count_distinct`, `avg`, `min`, `max`, or an expression over other measures, plus a format.
- **Joins:** type, keys and cardinality.
- **Segments:** named filters.
- **Row-level security rules:** filter expressions that reference user attributes.

```yaml
dataset: gifts
dimensions:
  gift_date: { column: gift_date, type: time, grains: [day, week, month, quarter, year] }
  fund_name: { column: fund_name, type: string }
  region:    { column: region, type: string }
measures:
  total_amount: { agg: sum, column: amount, format: currency_aud }
  gift_count:   { agg: count }
  avg_gift:     { expr: "{total_amount} / NULLIF({gift_count}, 0)", format: currency_aud }
joins:
  - to: donors
    type: left
    on: { donor_id: id }
    relationship: many_to_one
rls:
  - filter: "region IN {{ user.attributes.regions }}"
    applies_to_roles: [viewer, editor]
```

**Semantic query.** This JSON is what both the UI and the agent send; schema in `spec/semantic-query.schema.json`:

```json
{
  "dataset": "gifts",
  "dimensions": ["gifts.fund_name"],
  "measures": ["gifts.total_amount", "gifts.gift_count"],
  "timeDimension": { "field": "gifts.gift_date", "grain": "month", "range": { "relative": "last_12_months" } },
  "filters": [{ "field": "gifts.region", "op": "in", "values": ["VIC", "NSW"] }],
  "sort": [{ "field": "gifts.total_amount", "dir": "desc" }],
  "limit": 500
}
```

**Compiler.** Turns a semantic query into DuckDB SQL by building a sqlglot AST. Rules:
- Never string-concatenate user input.
- Always apply RLS, enforce row caps and set a query timeout.
- Unit tests are mandatory, including RLS cases and injection attempts.

**Starter models.** Auto-generate a starter model from a new dataset: dimensions from columns, a count measure, and sum measures for numeric columns.

**Raw SQL** exists only in SQL Lab (admin role, behind its flag). It is read-only, parsed with sqlglot, and rejected unless it is a single `SELECT`.

---

## 7. Connectors (plugin system)

```python
class Connector(Protocol):
    id: ClassVar[str]            # "mysql"
    name: ClassVar[str]          # "MySQL"
    version: ClassVar[str]

    @classmethod
    def config_schema(cls) -> dict: ...                 # JSON Schema; secret fields marked "x-secret": true
    def __init__(self, config: dict) -> None: ...
    def test_connection(self) -> TestResult: ...
    def discover(self) -> list[StreamSpec]: ...         # tables/objects with columns and types
    def read(self, stream: str, mode: RefreshMode, state: dict | None) -> ReadResult: ...
    # ReadResult: iterator of pyarrow.RecordBatch plus final_state() for incremental cursors
```

**Registration.** Built-in connectors live in `backend/opsis/connectors/<id>/`. Third-party connectors go in `plugins/connectors/<id>/` or register through the `opsis.connectors` entry point. The registry discovers both, so adding a connector never touches core code.

**Configuration and secrets.** The connection form in the UI is generated from `config_schema()`. Secrets are encrypted at rest with a Fernet key from env (`ENCRYPTION_KEY`). They are never returned by the API and never logged.

**Refresh modes:**
- Full replace.
- Incremental append by a cursor column.
- Incremental merge by primary key, de-duplicated when the snapshot is built.

**v1 connectors, in this order:**
1. CSV/Excel upload
2. PostgreSQL
3. MySQL
4. Google Sheets (service account)
5. Salesforce (SOQL via REST, incremental on `SystemModstamp`; Bulk API is a later improvement)

**Tests.** Each connector ships with tests: fixtures for files, and throwaway containers for Postgres and MySQL where that's cheap.

---

## 8. Visuals (plugin system)

**Layout.** Each visual is a folder at `frontend/src/visuals/<id>/` containing:
- `manifest.json`
- `Render.tsx`
- optionally `ConfigExtras.tsx`

The Docker build copies all manifests into the backend image, so the API and MCP server can list visuals and validate specs. Adding a visual means adding a folder.

**Manifest.** Schema in `spec/visual-manifest.schema.json`:

```json
{
  "id": "bar",
  "name": "Bar chart",
  "version": "1.0.0",
  "description": "Compare a measure across categories.",
  "roles": {
    "category": { "kind": "dimension", "min": 1, "max": 1 },
    "value":    { "kind": "measure",   "min": 1, "max": 5 },
    "series":   { "kind": "dimension", "min": 0, "max": 1 }
  },
  "configSchema": {
    "type": "object",
    "properties": {
      "orientation": { "enum": ["vertical", "horizontal"], "default": "vertical" },
      "stacked":     { "type": "boolean", "default": false },
      "showLabels":  { "type": "boolean", "default": false }
    }
  },
  "aiHints": "Best for comparing up to about 20 categories. For time series prefer line."
}
```

**How the manifest is used:**
- The editor builds field wells from `roles` and the config panel from `configSchema` (via rjsf).
- `aiHints` is passed to the agent so it picks sensible visuals.

**v1 visuals:**
- Table (sortable, paginated, conditional formatting)
- KPI card (with comparison delta)
- Bar, line, area, combo (bar + line), pie/donut, scatter
- Text/markdown

**Filter controls:** dropdown/multi-select, date range, search.

**Interactions:** click-to-cross-filter (toggle per dashboard) and drill-down through time grains.

**Theming:** a theme object in the spec, defaulting from `branding.json`. Use an accessible categorical palette that works in light and dark mode.

---

## 9. Dashboard spec (the core contract)

**Schema.** `spec/dashboard.schema.json` is the single source of truth. It is validated in the backend (jsonschema) and the frontend (ajv). A `specVersion` field plus migration functions handle future versions.

**Queries come from bindings.** Each item binds fields to visual roles; the backend derives the semantic query from bindings plus filters. This keeps specs small and hard to get wrong.

```json
{
  "specVersion": 1,
  "title": "Gifts overview",
  "theme": { "preset": "default" },
  "filters": [
    { "id": "f_date", "type": "dateRange", "field": "gifts.gift_date", "default": { "relative": "last_12_months" } },
    { "id": "f_region", "type": "multiSelect", "field": "gifts.region" }
  ],
  "layout": { "columns": 12, "rowHeight": 40 },
  "items": [
    {
      "id": "v_total",
      "visual": "kpi",
      "position": { "x": 0, "y": 0, "w": 3, "h": 3 },
      "dataset": "gifts",
      "bindings": { "value": ["gifts.total_amount"] },
      "config": { "comparison": "previous_period" }
    },
    {
      "id": "v_by_fund",
      "visual": "bar",
      "position": { "x": 3, "y": 0, "w": 9, "h": 6 },
      "dataset": "gifts",
      "bindings": { "category": ["gifts.fund_name"], "value": ["gifts.total_amount"] },
      "sort": [{ "field": "gifts.total_amount", "dir": "desc" }],
      "limit": 15,
      "config": { "orientation": "horizontal" }
    }
  ],
  "interactions": { "crossFilter": true }
}
```

**Versions.**
- Tables: `dashboards` plus `dashboard_versions` (immutable rows: spec JSON, `author_type` user|agent, `author_id`, message, timestamp).
- Every save creates a new version. Draft is the latest version; Published is a pointer to one specific version. Viewers only ever see the published version.

**Editing.**
- The UI saves the full spec.
- The agent uses **JSON Patch (RFC 6902)** with `base_version` for optimistic concurrency. A stale base returns 409, and the agent re-fetches and retries.
- Every write is validated. Invalid specs are rejected with precise JSON paths, so the agent can self-correct.

**Editor UX.**
- Undo/redo.
- Version history with a diff view and restore.
- A JSON spec tab (Monaco) for direct editing.
- Live updates: each dashboard has an SSE channel, so agent edits appear immediately in an open editor, with a toast reading "Updated by assistant. Undo".

---

## 10. AI integration

### 10.1 MCP server

**Transport.** Built with the official MCP Python SDK and mounted in `api` at `/mcp` (Streamable HTTP). It calls the same service layer as REST, so there is no duplicated business logic.

**Client setup to document:**
- Claude Code (desktop Code tab or CLI): a project-level `.mcp.json` in the repo pointing at `http://localhost:8080/mcp`, so the agent building Opsis can also test Opsis through its own MCP server. For the server, document `claude mcp add --transport http opsis https://urlloom.com/opsis/mcp` plus an `Authorization: Bearer <PAT>` header.
- Claude Desktop: through an `mcp-remote` bridge config.

**Auth:**
- `MCP_AUTH=none` is allowed only when bound to 127.0.0.1.
- Server mode uses **personal access tokens (PATs)**. Users create them in settings; each is shown once, stored hashed, scoped (`read`, `edit`, `publish`, `admin`), has an expiry, is revocable, and tracks when it was last used. MCP calls run as the token's user, with that user's permissions.
- Later milestone: OAuth 2.1 endpoints per the MCP authorization spec, so the server can be added as a claude.ai custom connector.

**Tools.** All are permission-checked and flag-gated.

| Group | Tools |
|---|---|
| Discovery | `list_connections`, `list_datasets`, `describe_dataset` (columns, types, sample values, row count, freshness), `get_semantic_model`, `list_visuals` (roles + aiHints), `get_spec_schema` |
| Data | `preview_dataset`, `run_query` (semantic query, max 1000 rows), `profile_column` |
| Modelling | `patch_semantic_model`, `validate_model` |
| Dashboards | `list_dashboards`, `get_dashboard`, `create_dashboard`, `patch_dashboard` (id, base_version, ops, message), `validate_spec`, `list_versions`, `restore_version` |
| Publishing | `publish_dashboard`, `share_dashboard`, `create_share_link` (server + flag only) |
| Ops | `trigger_refresh`, `get_job_status` |

**Tool design rules:**
- Keep results compact: cap rows and summarise large payloads.
- Write descriptions for an LLM: when to use the tool, plus one example input.
- Expose the spec schema and visual manifests as MCP resources.
- Provide a `build_dashboard` prompt that walks through: describe datasets, propose a layout, create, validate, then summarise.
- Audit every agent write: tool, argument summary, user, and the version created.

### 10.2 In-app assistant (`FEATURE_ASSISTANT`)

**Providers.** A chat side panel in the editor, behind a provider abstraction:
- `anthropic`: Messages API with tool use; `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`.
- `ollama`: OpenAI-compatible endpoint at `OLLAMA_URL` (default `http://host.docker.internal:11434`); `OLLAMA_MODEL`.

**Tools.** Uses the same tool implementations as MCP, calling service functions directly.

**Context.** The current dashboard, the selected visual and the available datasets.

**UX.** Streams responses and shows each tool call as a collapsible step. Every change becomes a new version with one-click undo.

**Budgets.** Limit tool iterations per request and rows returned to the model, and log token usage per request.

---

## 11. Auth, users and sharing

### Modes (`AUTH_MODE`)
- **`none`:** a single built-in admin who is signed in automatically. Allowed only when bound to 127.0.0.1. Permission checks still run, so the code paths are identical to the other modes.
- **`local`:** email and password accounts (argon2id). Registration is invite-only by an admin by default. Password resets go by email if `FEATURE_EMAIL` is on; otherwise an admin resets them.
- **`full`:** local accounts **plus Sign in with Google** (OIDC via Authlib; scopes `openid email profile`; validate state and nonce, use PKCE). Config:
  - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
  - `GOOGLE_ALLOWED_DOMAINS` (comma list; empty means any)
  - `GOOGLE_ALLOWED_EMAILS`
  - `GOOGLE_AUTO_PROVISION` (default `false`: only invited emails can sign in)
  - Redirect URI: `${PUBLIC_BASE_URL}/api/auth/google/callback`
  - A Google identity links to an existing local account only when Google reports the email as verified and it matches.

### Bootstrap
`ADMIN_EMAIL` and `ADMIN_INITIAL_PASSWORD` create the first admin on first start, with a forced password change.

### Sessions and request security
- **Sessions:** a server-side session table with an opaque random ID in the `opsis_session` cookie (HttpOnly; Secure under https; SameSite=Lax; `Path={BASE_PATH}`). Idle and absolute timeouts. Revoked on logout and password change. "Sign out everywhere" option.
- **CSRF:** a double-submit token on every state-changing browser request.
- **CORS:** off. The UI is same-origin, so no cross-origin access is allowed.
- **Login protection:** rate limiting with lockout backoff, stored in Postgres. TOTP 2FA is a later item.

### Authorisation
- **Workspace roles:**
  - `admin`: users, connections, settings, everything.
  - `editor`: datasets, models and dashboards.
  - `viewer`: published dashboards shared with them.
- **Groups** are used for sharing.
- **Dataset ACL:** users can only query datasets they have access to, with RLS on top.
- **Dashboard ACL:** owner, editors and viewers, assigned to users or groups.

### Publishing and share links
- Dashboards are Draft or Published.
- **Share links** (server mode, behind their flag):
  - A random 32-byte token URL serving the published version read-only.
  - Optional expiry and password, revocable, with a view count.
  - On datasets with RLS, the link uses an explicit attribute set chosen by the owner when creating it, and defaults to deny.

### Audit and headers
- **Audit log:** logins (success and failure), user and role changes, connection changes, publishes, shares, link creation and revocation, and agent writes. Admins can browse it in the UI.
- **Security headers:** CSP and frame protection, set by the app. HSTS is left to the external reverse proxy (see 3.1); the bundled Caddy profile may set it.

---

## 12. Repo layout

```
/
  BUILD_BRIEF.md  PROGRESS.md  README.md
  compose.yml  compose.dev.yml
  .env.local.example  .env.server.example
  .gitattributes            # * text=auto eol=lf
  branding.json
  backend/
    Dockerfile  pyproject.toml
    opsis/
      main.py  settings.py
      api/          # routers only
      services/     # business logic shared by REST, MCP and assistant
      auth/
      connectors/   # base.py, registry.py, csv/, postgres/, mysql/, gsheets/, salesforce/
      semantic/     # models, compiler, rls
      query/        # engine interface, duckdb engine, cache
      storage/      # parquet snapshots, retention
      dashboards/   # spec validation, versions, json patch
      mcp/          # server, tools, resources, prompts
      assistant/    # providers, tool loop
      jobs/         # queue, runner, schedules
      db/           # SQLAlchemy models, alembic
    tests/
  frontend/
    package.json
    src/
      app/  pages/  components/  editor/  lib/
      visuals/<id>/manifest.json, Render.tsx
  spec/
    dashboard.schema.json  visual-manifest.schema.json  semantic-query.schema.json
  plugins/connectors/       # drop-in third-party connectors
  deploy/
    caddy/Caddyfile         # optional bundled proxy
    proxytest/nginx.conf    # mimics urlloom.com/opsis on the laptop
    proxmox/README.md
    windows/README.md
  docs/
    ARCHITECTURE.md  DECISIONS.md
```

---

## 13. Docker requirements

**Images and services:**
- `compose.yml` sets the top-level `name: opsis`, so containers and volumes are named consistently whatever folder the repo is cloned into.
- A multi-stage Dockerfile builds the frontend with Node, then produces one Python runtime image (`opsis/app`) containing the static build. Both `api` and `worker` use it.
- Containers run as a non-root user, with pinned base images.
- Healthchecks on `api` and `postgres`, with `depends_on: condition: service_healthy`.
- `extra_hosts: ["host.docker.internal:host-gateway"]` on `api` and `worker`, so the same `OLLAMA_URL` works on Windows and Linux.

**Ports and volumes:**
- `api` publishes `${BIND_ADDRESS}:${HTTP_PORT}:8080` in both modes. Locally that is `127.0.0.1`. On the server it is the LXC's LAN IP, which my reverse proxy forwards to; the Proxmox README should suggest an LXC firewall rule that only allows the proxy's IP on that port. Caddy's 80/443 are published only with the optional `caddy` profile.
- Healthchecks call `{BASE_PATH}/healthz`.
- Named volumes: `pgdata`, `opsisdata`, `caddy_data`, `backups`. On the server, `backups` may be a bind mount to an NFS path.

**Cross-platform safety:**
- `.gitattributes` forces LF line endings.
- Prefer Python over shell for entrypoints and task scripts. Windows has no `make`, so use a cross-platform `tasks.py` or npm/uv scripts.
- `compose.dev.yml` adds hot reload (uvicorn `--reload` and the Vite dev server with an API proxy). Document that on Windows the repo should live inside the WSL filesystem for fast file watching.

**Quick start to document:**
- Laptop: `copy .env.local.example .env` then `docker compose up -d`
- Server: `cp .env.server.example .env` then `docker compose up -d`

**`deploy/proxmox/README.md`:** unprivileged LXC with `nesting=1` and `keyctl=1`, Docker Engine install, storage notes, suggested resources (4 vCPU, 8 GB RAM, disk sized for snapshots), and a backup and restore procedure.

**`deploy/windows/README.md`:** Docker Desktop with the WSL2 backend, `.wslconfig` memory (8 GB or more), native Ollama install, and Claude Code / Claude Desktop MCP setup.

---

## 14. Milestones (stop after each one)

**M0. Plan (no app code).**
- Produce:
  - `docs/ARCHITECTURE.md`: Mermaid component diagram, data flow for refresh and query, auth flows
  - `docs/DECISIONS.md`
  - the three JSON Schemas in `spec/`
  - the `Connector` and `QueryEngine` interfaces as Python stubs
  - one example visual manifest
  - `PROGRESS.md`
- List risks and questions. Keep it tight.
- *Done when:* I can review the schemas and interfaces in under 20 minutes.

**M1. Skeleton and modes.**
- Compose file with profiles, and both env templates.
- Settings module with flag validation; `/healthz` and `/api/config`.
- Postgres and Alembic baseline: users, sessions, audit_log, jobs.
- `AUTH_MODE=none` flow, with the user model and permission checks in place from day one.
- Frontend shell that reads the config; multi-stage Dockerfile; README quick start.
- `.claude/launch.json` so the Claude desktop app can start the stack and open it in its Browser pane. One configuration for the full stack (`docker compose up`, port 8080) and one for dev mode (compose plus `compose.dev.yml`, Vite port). Format:
  ```json
  {
    "version": "0.0.1",
    "configurations": [
      { "name": "opsis", "runtimeExecutable": "docker", "runtimeArgs": ["compose", "up"], "port": 8080 }
    ]
  }
  ```
- Sub-path support from day one (3.1): app mounted under `BASE_PATH`, runtime-injected base href and config, path-scoped cookies, forwarded-header handling, and the `proxytest` profile.
- *Done when:*
  - `docker compose up -d` works with both env files.
  - Changing a flag changes `/api/config` and the UI.
  - Invalid combinations fail fast with a clear message.
  - With the `proxytest` profile, the app works at `http://localhost:8088/opsis/`: deep links and refresh load correctly, assets and API calls stay under `/opsis`, and cookies are scoped to `/opsis`.
  - The same image also works at the root locally.
  - Tests pass.

**M2. Data ingestion.**
- Connector registry; CSV/Excel upload, Postgres and MySQL connectors.
- Connections UI with schema-generated forms and encrypted secrets.
- Datasets from streams; refresh jobs (inline and worker); Parquet snapshots with retention.
- Job status UI and dataset preview.
- *Done when:* I can connect a MySQL database, create a dataset from a table, refresh it and preview rows. A second refresh creates a new snapshot and swaps atomically. Both job runner modes work.

**M3. Semantic layer and queries.**
- Model CRUD, YAML import/export and starter-model generation.
- sqlglot compiler with RLS; query endpoint with cache.
- Compiler tests, including RLS and injection attempts.
- *Done when:* semantic query results match a hand-written SQL check, and RLS filters rows correctly per user attribute.

**M4. Dashboards and visuals.**
- Spec validation, versions, and draft/published states.
- Editor: grid drag and resize, role-based field wells, config panel from `configSchema`, Monaco JSON tab, undo/redo, history with diff and restore.
- v1 visuals and filters, cross-filtering, theming.
- *Done when:* I can build a six-visual dashboard by hand, save, reload and restore an old version. Invalid JSON in the spec tab is rejected with a clear path.

**M5. MCP.**
- `/mcp`, PATs, all tools, resources and the prompt; audit of agent writes.
- SSE live updates in the editor.
- *Done when:*
  - From Claude Code, "build a dashboard of X from dataset Y" creates a valid dashboard that appears live in an open editor.
  - A stale `base_version` returns 409 and the agent recovers.
  - MCP and SSE both work through the `proxytest` profile at `/opsis/mcp`.

**M6. Auth and sharing (server features).**
- Local accounts and invites; Google sign-in with domain and email allowlists.
- Roles, groups, ACLs, sessions, CSRF and rate limiting.
- Share links and the audit log UI; the optional `caddy` and `tunnel` profiles; proxy configs for Nginx and Caddy in `deploy/proxmox/README.md`.
- *Done when:*
  - In server mode at `https://urlloom.com/opsis`, Google sign-in works for an allowed email and is refused for others.
  - A viewer cannot edit.
  - A share link shows only the published version and stops working once revoked.
  - None of this is exposed in local mode.

**M7. In-app assistant.**
- Anthropic and Ollama providers; tool loop with budgets; streaming UI with step display and undo.
- *Done when:* the same "build a dashboard" request works from the panel with each provider.

**M8. More connectors and hardening.**
- Google Sheets and Salesforce (incremental) connectors.
- Backup job with a restore doc; scheduled refresh UI with cron presets; email (behind its flag).
- Performance check: a typical dashboard on a 10M-row dataset loads in under 1.5 s warm.
- Full Windows and Proxmox docs; a demo seed dataset.
- *Done when:* a fresh install on each target works from the README alone.

---

## 15. Non-goals for v1

- A DAX-like formula language (measures are SQL expressions over the model).
- Real-time streaming data.
- Multi-tenant SaaS.
- Kubernetes.
- Mobile apps.
- Pixel-perfect paginated reports.
- Write-back to sources.

---

## 16. Quality bar

- **Backend:** ruff, mypy (strict on `services`, `semantic` and `dashboards`), pytest.
- **Frontend:** strict tsc, eslint, Vitest, and one Playwright smoke test (sign in, open a dashboard, edit a visual).
- **SQL safety:** no raw SQL from users or the agent outside SQL Lab; all SQL is built through sqlglot; queries have timeouts and row caps.
- **Secrets:** only from env or encrypted DB fields, never logged; `.env` is gitignored.
- **Errors:** messages are precise and useful to both humans and the agent, with JSON paths where relevant.
- **Accessibility:** basic keyboard navigation in the editor, and a chart palette with adequate contrast in light and dark mode.

---

## 17. Start

Begin with **Milestone 0 only**, then stop and report.
