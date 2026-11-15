# Opsis

A self-hosted, AI-native business intelligence platform. Open source, Docker-based, and built so that a person and an AI agent edit the same dashboards.

**Status:** early development. You can connect to CSV or Excel files, PostgreSQL and MySQL, pull tables into local storage, define a semantic model for each dataset (dimensions, measures, joins, row-level security) run queries against it, build dashboards on top with a drag-and-resize editor, and let an AI agent do the same through the built-in MCP server. Sign-in and sharing are being built milestone by milestone. See [PROGRESS.md](PROGRESS.md).

## Quick start

You need Docker (Docker Desktop on Windows, Docker Engine on Linux) and Git.

### Laptop

```
copy .env.local.example .env
docker compose up -d
```

(On macOS or Linux use `cp` in place of `copy`.) Open http://localhost:8080. There is no sign-in in local mode: you are the built-in admin, and the app only listens on your own machine.

### Try it with sample data

Set `COMPOSE_PROFILES=sample` in `.env` and run `docker compose up -d` again. This adds a small MySQL database with `donors` and `gifts` tables. Then, in Opsis:

1. **Connections, New connection.** Type MySQL, host `sample-mysql`, database `sample`, user `reader`, password `reader-sample-pw`. Press Test, then Save.
2. **Datasets, New dataset.** Choose the connection and the `gifts` table, then Create and refresh.
3. The dataset page shows a preview, the refresh history and the snapshots kept. Refresh now makes a new snapshot.

To use your own data, add a connection of type CSV or Excel file (upload it in the form), PostgreSQL or MySQL.

### Semantic models

Every dataset gets a generated model on its first refresh. Open **Semantic models**, choose a dataset, and you can:

- **Try a query:** tick measures and dimensions, choose a date field with a grain (including financial quarter and year) and a range such as "Last financial year", and run it. The generated SQL is shown under the result.
- **Edit the model as YAML:** rename fields, add measures such as `avg_gift: {expr: "{total_amount} / NULLIF({gift_count}, 0)"}`, join to another dataset, or add row-level security. Check reports every problem with its location; Save stores it.
- **Test row-level security:** as an admin, set "Run as" to a viewer and give that user's attributes as JSON.

A model with a join and a security rule:

```yaml
dataset: gifts
dimensions:
  fund_name: {column: fund_name, type: string, label: Fund}
  gift_date: {column: gift_date, type: time}
measures:
  total_amount: {agg: sum, column: amount, format: currency_aud}
  gift_count: {agg: count}
  avg_gift: {expr: "{total_amount} / NULLIF({gift_count}, 0)", format: currency_aud}
joins:
  - to: donors
    on: {donor_id: id}
    relationship: many_to_one
rls:
  - filter: "fund_name IN {{ user.attributes.funds }}"
```

The rule above lets viewers and editors see only the funds listed in their `funds` attribute. A user with no such attribute sees no rows. The full format is in [spec/semantic-model.schema.json](spec/semantic-model.schema.json).

More that a model can say:

- **Conditional measures:** `online_amount: {agg: sum, column: amount, filter: "channel = 'online'"}` counts only matching rows. A segment can be used by name: `filter: "{large} AND region = 'VIC'"`.
- **Timezones:** timestamps that carry an offset in the source are stored as instants and shown in `WORKSPACE_TIMEZONE`. A timestamp without one is taken as workspace-local time; if the source keeps it in another zone, say so on the dimension: `created: {column: created, type: time, timezone: UTC}`.
- **Version history:** every save is a version, with who and when. The model page lists them and can restore any of them.

### Dashboards

Open **Dashboards**, name a new one, and you are in the editor:

- **Add** a visual from the toolbar: table, KPI card, bar, line, area, combo, pie or donut, scatter, or text. Drag a tile to move it and pull its corner to resize it. On a combo chart each measure can use the left or the right value axis (for example amount as bars on the left, number of gifts as a line on the right).
- Click a tile to select it. The **Visual** tab picks its dataset, puts fields into the visual's roles (for example Category, Values, Split by), sets sort and row limit, and shows the visual's own options.
- **Filters** adds controls above the dashboard: a dropdown, a multi-select, a date range (including "This financial year") or a text search.
- **Dashboard** holds the title, grid and theme (auto, light or dark). **History** lists every saved version, shows what changed, and can restore one. **JSON** is the whole dashboard as text; Apply checks it first and reports each problem with its path.
- **Undo** and **Redo** (Ctrl+Z, Ctrl+Y) cover everything since you opened the editor. **Save** makes a new version. **Publish** makes the saved version the one viewers see.

When viewing, click a bar, slice, point or table value to filter the other visuals by it, change the time grain on a time chart (year to day), and switch any chart to a table of the same data. A tile shows "RLS bypassed" when row-level security rules exist on its data but do not apply to your role.

The format is [spec/dashboard.schema.json](spec/dashboard.schema.json). Each visual declares its roles and options in `frontend/src/visuals/<id>/manifest.json`.

### Working with an AI agent (MCP)

Opsis has an MCP server at `/mcp` (for example `http://localhost:8080/mcp`). Through it an agent can look at datasets, run queries, and build or change dashboards. Everything it saves is a normal version marked as made by the assistant, and an open editor shows the change at once with an **Undo** button.

**Claude Code.** This repository has a `.mcp.json` that points at `http://localhost:8080/mcp`, so Claude Code started in this folder finds the server by itself. Anywhere else:

```bash
claude mcp add --transport http opsis http://localhost:8080/mcp
```

Then ask, for example: "Build a dashboard of giving by fund and month from the gifts dataset." There is also a ready-made prompt, `build_dashboard`.

**Claude Desktop** connects through the `mcp-remote` bridge. Add this to its `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "opsis": { "command": "npx", "args": ["-y", "mcp-remote", "http://localhost:8080/mcp"] }
  }
}
```

**Access tokens.** On a laptop install (`MCP_AUTH=none`) the server accepts local connections without a token. On a server (`MCP_AUTH=token`) every client needs a personal access token: open **Access tokens**, create one, and copy it when it is shown (it is shown only once). A token has scopes (read, edit, publish), expires, can be revoked, and never allows more than your own role. Use it like this:

```bash
claude mcp add --transport http opsis https://your-domain/opsis/mcp --header "Authorization: Bearer <your token>"
```

The tools: `list_datasets`, `describe_dataset`, `list_visuals`, `get_spec_schema`, `list_connections`, `get_semantic_model`, `preview_dataset`, `run_query`, `profile_column`, `validate_model`, `patch_semantic_model`, `list_dashboards`, `get_dashboard`, `validate_spec`, `create_dashboard`, `patch_dashboard`, `list_versions`, `restore_version`, `publish_dashboard`, `trigger_refresh`, `get_job_status`.

### Server

```
cp .env.server.example .env
```

Edit `.env` and set every value marked `CHANGE` (public URL, trusted proxy, database password, encryption key). Generate the encryption key with:

```
docker compose run --rm --no-deps api generate-key
```

Then:

```
docker compose up -d
```

The stack refuses to start while a `CHANGE` value is still a placeholder. Server mode adds a worker container. Sign-in (local accounts and Google) is part of a later milestone, so a server install currently shows a sign-in notice and nothing else.

### Behind a reverse proxy at a sub-path

Opsis can live at a path such as `https://example.com/opsis` next to other apps on the same domain. Set `PUBLIC_BASE_URL` to that address and `TRUSTED_PROXIES` to your proxy's IP. The proxy must forward the path unchanged (do not strip the prefix). [deploy/proxytest/nginx.conf](deploy/proxytest/nginx.conf) is a working nginx example with the settings needed for streaming and uploads.

To try this on a laptop, put these in `.env` and open http://localhost:8088/opsis/:

```
COMPOSE_PROFILES=proxytest
PUBLIC_BASE_URL=http://localhost:8088/opsis
TRUSTED_PROXIES=172.16.0.0/12
```

### Useful commands

| Command | What it does |
|---|---|
| `docker compose ps -a` | Show the services and their health |
| `docker compose logs config-check` | Show why the configuration was rejected |
| `docker compose logs api` | Show the API log |
| `docker compose down` | Stop the stack (data is kept in named volumes) |
| `docker compose up -d --build` | Rebuild after pulling new code |

### Compose profiles

| Profile | Adds |
|---|---|
| (none) | `api` and `postgres` |
| `server` | `worker`, needed for `JOB_RUNNER=worker` |
| `caddy` | A bundled Caddy proxy with TLS, for installs with no reverse proxy of their own |
| `tunnel` | Cloudflare Tunnel |
| `ollama` | An Ollama container |
| `proxytest` | nginx serving Opsis at `http://localhost:8088/opsis/`, to test sub-path hosting |
| `sample` | A MySQL database with sample data |

## Configuration

Everything is set in `.env`. The two templates document each value. After a change, run `docker compose up -d` again.

| Variable | Purpose | Local | Server |
|---|---|---|---|
| `DEPLOY_MODE` | `local` or `server` | `local` | `server` |
| `AUTH_MODE` | `none`, `local`, or `full` (local accounts plus Google) | `none` | `full` |
| `JOB_RUNNER` | `inline` (inside the api) or `worker` (own container) | `inline` | `worker` |
| `MCP_AUTH` | `none` (loopback only) or `token` | `none` | `token` |
| `FEATURE_SCHEDULED_REFRESH` | Cron-based refresh schedules | `false` | `true` |
| `FEATURE_PUBLIC_SHARE_LINKS` | Public read-only links | `false` | `true` |
| `FEATURE_EMAIL` | SMTP invites, resets and snapshots | `false` | `false` |
| `FEATURE_ASSISTANT` | In-app AI panel | `true` | `true` |
| `ASSISTANT_PROVIDER` | `anthropic` or `ollama` | `ollama` | `anthropic` |
| `FEATURE_SQL_LAB` | Admin-only read-only SQL | `true` | `false` |
| `FEATURE_BACKUPS` | Nightly backup job | `false` | `true` |
| `BIND_ADDRESS`, `HTTP_PORT` | Where the api container is published on the host | `127.0.0.1`, `8080` | the server's LAN IP, `8080` |
| `PUBLIC_BASE_URL` | The address users type. A path becomes the app's base path | `http://localhost:8080` | your https address |
| `TRUSTED_PROXIES` | IPs or CIDR ranges allowed to set forwarded headers | empty | your proxy's IP |
| `WORKSPACE_TIMEZONE` | Timezone for relative dates such as "this month" | `Australia/Melbourne` | same |
| `WEEK_START` | `monday` or `sunday` | `monday` | same |
| `FISCAL_YEAR_START_MONTH` | First month of the financial year, 1 to 12 | `7` | same |
| `FISCAL_YEAR_LABEL` | `fy_end_year` ("FY2027") or `year_span` ("2026-27") | `fy_end_year` | same |
| `MAX_UPLOAD_MB` | Largest CSV or Excel upload | `200` | same |
| `SNAPSHOT_RETENTION` | Snapshots kept per dataset (minimum 2) | `3` | same |
| `QUERY_TIMEOUT_SECONDS` | A query running longer than this is stopped | `30` | same |
| `QUERY_MAX_ROWS` | Most rows one query may return | `10000` | same |
| `QUERY_MEMORY_LIMIT_MB` | Memory one query may use before spilling to disk | `1024` | same |
| `QUERY_MAX_CONCURRENT` | Queries that run at once; others wait | `4` | same |
| `ENCRYPTION_KEY` | Encrypts saved connection passwords | empty (generated and kept in the data volume) | required |
| `POSTGRES_PASSWORD` | Password of the internal metadata database | template value | required, 12 characters or more |
| `APP_NAME` | Product name shown in the UI | `Opsis` | same |

Unsafe or contradictory settings stop the stack once, before anything else starts, with a message that names the variable and the fix. For example, `AUTH_MODE=none` with `BIND_ADDRESS=0.0.0.0` is refused, and so is a server install with a placeholder password. To check a config without starting anything:

```
docker compose run --rm --no-deps api check-config
```

Feature flags are enforced by the backend. The UI reads `GET /api/config` only to decide what to show.

Colours and tagline come from `branding.json`; the name comes from `APP_NAME`.

## Development

The only tools needed on the host are Docker and Node.js (for the frontend tests and the task scripts). Backend tooling runs in containers.

| Command | What it does |
|---|---|
| `npm run dev` | Hot reload: API with auto-restart, Vite dev server on http://localhost:5173 |
| `npm run test:backend` | pytest in the dev image, against a throwaway `opsis_test` database and the sample MySQL |
| `npm run lint:backend` | ruff and mypy (strict) |
| `npm run test:frontend` | TypeScript, ESLint and Vitest |
| `npm run smoke` | Playwright browser tests, including an MCP client, at http://localhost:8080 and then behind nginx at http://localhost:8088/opsis/. Add `-- --no-build` to skip the image rebuild |
| `npm run lock:backend` | Regenerate `backend/uv.lock` after editing `pyproject.toml` |
| `node scripts/benchmark.mjs --rows 5000000` | Load a synthetic dataset into the running stack and time ingestion and queries |

Run `npm install` inside `frontend/` once before the frontend commands. The smoke tests install their own dependencies and a Chromium browser the first time (in `e2e/`).

On Windows, hot reload uses file polling. It is noticeably faster when the repository lives inside the WSL filesystem.

In the Claude desktop app, `.claude/launch.json` defines two launch configurations: `opsis` (the built stack on port 8080) and `opsis-dev` (hot reload on port 5173).

## Documentation

- [BUILD_BRIEF.md](BUILD_BRIEF.md): the product spec and milestones
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, data flows, auth flows
- [docs/DECISIONS.md](docs/DECISIONS.md): decision log
- [spec/](spec): JSON Schemas for dashboards, semantic queries and visual manifests

## Licence

[Apache License 2.0](LICENSE).
