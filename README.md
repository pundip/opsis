# Opsis

A self-hosted, AI-native business intelligence platform. Open source, Docker-based, and built so that a person and an AI agent edit the same dashboards.

**Status:** version 1 is feature-complete and in its final install checks. You can connect to CSV or Excel files, PostgreSQL, MySQL, Google Sheets and Salesforce, pull tables into local storage on demand or on a schedule, define a semantic model for each dataset (dimensions, measures, joins, row-level security), build dashboards with a drag-and-drop editor, let an AI agent do the same through the built-in MCP server or the assistant in the editor, and, on a server, sign in (local accounts and Google), share dashboards, publish read-only share links, have dashboards emailed, and take nightly backups. See [PROGRESS.md](PROGRESS.md).

## Quick start

You need Docker (Docker Desktop on Windows, Docker Engine on Linux) and Git.

### Laptop

```
copy .env.local.example .env
docker compose up -d
```

(On macOS or Linux use `cp` in place of `copy`.) Open http://localhost:8080. There is no sign-in in local mode: you are the built-in admin, and the app only listens on your own machine. The first start takes a minute or two while the image builds. The Windows set-up (Docker Desktop memory, Ollama, Claude Desktop) is in [deploy/windows/README.md](deploy/windows/README.md).

### Demo data

To have something to look at straight away, open **Settings** and choose **Load demo data**, or run:

```
docker compose exec api python -m opsis seed-demo
```

It makes three datasets of made-up giving data (`demo_gifts`, `demo_donors`, `demo_regions`, joined to each other), their semantic models, and a published dashboard, **Giving overview (demo)**. To remove it, delete the dashboard and the three datasets.

### Your own data

Open **Connections**, **New connection**, and choose the type:

- **CSV or Excel file:** upload it in the form. Each sheet of a workbook is a table.
- **PostgreSQL or MySQL:** host, port, database, and a user that can read the tables. From inside Docker, a database on your own machine is at `host.docker.internal`.
- **Google Sheets:** see below.
- **Salesforce:** see below.

Press **Test**, then **Save**. Then **Datasets**, **New dataset**: choose the connection and a table, then **Create and refresh**. The dataset page shows a preview, the refresh history and the snapshots kept; **Refresh now** makes a new snapshot. Saved passwords and keys are encrypted with `ENCRYPTION_KEY` and never shown again.

**Old and untidy data.** A value that cannot be stored in its column's type, such as a MySQL zero date (`0000-00-00`, `2020-05-00`) or text in a number column, never fails a refresh. It is stored as empty (NULL), and **Data quality** on the dataset page shows, per column, how many, why, and a few of the raw values; so do the refresh job and MCP's `describe_dataset`. Each connection's **Invalid dates** setting chooses what happens to dates that cannot exist: store them as NULL (the default), read the connection's date columns as text exactly as stored, or stop the refresh. **Column types** on the dataset page sets a column to text, a whole or decimal number, a date (with a format such as `%d/%m/%Y` if it is not written `2026-12-31`), a date and time, or true or false. Opsis suggests a type for text columns that consistently hold dates, numbers or true/false values, but never applies one by itself. Your choices are kept when the source's columns change.

A sample MySQL database is there to try connections with: set `COMPOSE_PROFILES=sample` in `.env`, run `docker compose up -d` again, and connect to host `sample-mysql`, database `sample`, user `reader`, password `reader-sample-pw`.

**Google Sheets.** Opsis reads spreadsheets with read-only access. It signs in with your own Google account, through your own OAuth client (the same `credentials.json` a Python script would use), or with a service account.

With your Google account:

1. In the Google Cloud console, pick or create a project, and enable the **Google Sheets API** and the **Google Drive API** (APIs and services, Library). Drive is used only to list your spreadsheets and to show which account is connected.
2. Set up the OAuth consent screen (Google Auth Platform). While its publishing status is **Testing**, add your account as a test user. Google then ends the sign-in's refresh token after 7 days, so you would reconnect every week; setting the app to **In production** stops that.
3. Create an OAuth client ID of type **Desktop app** and download its JSON (`credentials.json`). A **Web application** client works too.
4. In Opsis, a new **Google Sheets** connection: sign in with **My Google account (OAuth)**, load `credentials.json`, and save. Then choose **Connect** on the connection: Google asks you to allow read-only access to your spreadsheets and to the names of your Drive files.
   - On a laptop, with Opsis at `http://localhost:8080`, Google comes straight back to Opsis.
   - On a server, or under a sub-path, Google sends the browser to an address that does not load (it starts with `http://localhost`). Copy the whole address from the address bar and paste it into Opsis. With a Web application client whose authorised redirect URIs include Opsis's own address (for example `https://example.com/opsis/`), Google comes straight back instead.
   - Or use an existing `token.json` instead of consenting again, if it was made with the same OAuth client and may read spreadsheets.
5. Pick a spreadsheet from your list, or paste its URL in the connection's settings. Each tab becomes a table. Column types come from the cells: numbers, true or false, and cells formatted as dates or date-times. Sheets refresh in full.

Opsis keeps only a refresh token, encrypted, which nobody can see. If Google stops accepting it (you removed the app's access, your password changed, or 7 days passed in Testing), the connection shows **Needs reconnecting**, its scheduled refreshes pause, and, with email on, its owner is emailed. **Reconnect** puts it right.

With a service account instead: in the Google Cloud console, under IAM and admin, **Service accounts**, create one with no roles, then **Keys**, **Add key**, **JSON**. Share the spreadsheet with its email address (ending in `iam.gserviceaccount.com`) as a **Viewer**. In Opsis, sign in with **A service account**, and give the key file and the spreadsheet's URL.

**Salesforce.** Opsis reads objects (Account, Opportunity, your custom objects) through the Salesforce API:

1. In Setup, open the **External Client App Manager** and create an External Client App. In its OAuth settings, enable OAuth with the scope **Manage user data via APIs (api)** and any callback URL (it is not used), and enable the **Client Credentials Flow**. In its policies, choose the user it runs as: an integration user with the API Enabled permission who can read the objects you want.
2. Copy the app's **Consumer Key** and **Consumer Secret**.
3. In Opsis, a new **Salesforce** connection: your My Domain URL (Setup, My Domain, for example `https://acme.my.salesforce.com`), sign in with `client_credentials`, the key and the secret. Optionally list the objects to offer, such as `Account, Opportunity, Donation__c`. **Test** shows the org's name and edition. A username, password and security token also work (`password`).
4. For a dataset, choose **Merge** as the refresh mode: each refresh then reads only the records changed since the last one (by `SystemModstamp`) and merges them by `Id`. Records deleted in Salesforce stay until a **Full rebuild** on the dataset page.

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
- **Two joins away:** a model can use a dimension of a dataset its joined dataset joins to, for example gifts to donors to regions. Every join on the way must be many-to-one or one-to-one, and a path never goes through a restricted dataset.
- **All models as files:** on **Semantic models**, **Export all** downloads a zip of `models/<dataset>.yml` for version control, and **Import** takes such a zip (or `.yml` files) back. Every file is checked first and nothing is saved unless all of them are valid; each model that changes gets a new version.

### Dashboards

Open **Dashboards**, name a new one, and you are in the editor:

- **Add** a visual from the toolbar: table, KPI card, bar, line, area, combo, pie or donut, scatter, or text. Drag a tile to move it and pull its corner to resize it. On a combo chart each measure can use the left or the right value axis (for example amount as bars on the left, number of gifts as a line on the right).
- Click a tile to select it. The **Visual** tab picks its dataset, puts fields into the visual's roles (for example Category, Values, Split by), sets sort and row limit, and shows the visual's own options. Drag a field from the list into a role, or between roles; a role only takes fields that fit it. Each role also has an **Add a field** menu.
- **Filters** adds controls above the dashboard: a dropdown, a multi-select, a date range (including "This financial year") or a text search.
- **Dashboard** holds the title, grid and theme (auto, light or dark). **History** lists every saved version, shows what changed, and can restore one. **JSON** is the whole dashboard as text; Apply checks it first and reports each problem with its path.
- **Undo** and **Redo** (Ctrl+Z, Ctrl+Y) cover everything since you opened the editor. **Save** makes a new version. **Publish** makes the saved version the one viewers see.

When viewing, click a bar, slice, point or table value to filter the other visuals by it, change the time grain on a time chart (year to day), and switch any chart to a table of the same data. A tile shows "RLS bypassed" when row-level security rules exist on its data but do not apply to your role. With email on, **Email me** sends you the dashboard on a schedule (see Email below).

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

The tools: `list_datasets`, `describe_dataset`, `list_visuals`, `get_spec_schema`, `list_connections`, `get_semantic_model`, `preview_dataset`, `run_query`, `profile_column`, `validate_model`, `patch_semantic_model`, `list_dashboards`, `get_dashboard`, `validate_spec`, `create_dashboard`, `patch_dashboard`, `list_versions`, `restore_version`, `publish_dashboard`, `trigger_refresh`, `get_job_status`, and with sign-in `share_dashboard` and (with share links on) `create_share_link`.

### The assistant in the editor

The dashboard editor has an **Assistant** tab: ask it to build a dashboard, change a visual, or answer a question about the data. It uses the same tools as MCP, with your own access, and works on the dashboard you have open (the visual you have selected is what "this" means). Its reply streams in, each tool call is a step you can open to see what it sent and got back, and every change is saved as a new version with an **Undo** button. It cannot create, publish or share dashboards: you do that. Save your own changes first; Send waits for that.

It runs on one of two providers, set in `.env`:

- **Ollama** (`ASSISTANT_PROVIDER=ollama`, the laptop default): a model on your own machine. Install Ollama, pull a model that can call tools (`ollama pull gemma4:12b`), and set `OLLAMA_MODEL`. Nothing else needs setting up in Ollama: the assistant uses Ollama's own chat API and asks for a 32,768-token context with each request (`OLLAMA_NUM_CTX`), which its instructions and tools need. With the `ollama` Compose profile, use `OLLAMA_URL=http://ollama:11434`. Local models are slower and less reliable than Claude: on this laptop `gemma4:12b` takes a few minutes to build a small dashboard.
- **Claude** (`ASSISTANT_PROVIDER=anthropic`, the server default): set `ANTHROPIC_API_KEY`, and optionally `ANTHROPIC_MODEL` (default `claude-opus-5-5`) and `ANTHROPIC_EFFORT` (`low` to `max`, default `medium`). A request Claude's safety checks decline is re-run on the model Anthropic recommends for it, and the panel says when that happened (`ANTHROPIC_FALLBACKS=false` turns it off).

Each request has budgets: `ASSISTANT_MAX_STEPS` tool calls (25), `ASSISTANT_MAX_OUTPUT_TOKENS` (100,000), `ASSISTANT_TIMEOUT_SECONDS` (600) and `ASSISTANT_MAX_ROWS` rows of a result shown to the model (100). When one runs out the assistant stops and says so; ask it to continue. Token use is shown under each request and recorded in the audit log.

### Server

```
cp .env.server.example .env
```

Edit `.env` and set every value marked `CHANGE` (public URL, trusted proxy, database password, encryption key, the first admin, and the Google client). Generate the encryption key with:

```
docker compose run --rm --no-deps api generate-key
```

Then:

```
docker compose up -d
```

The stack refuses to start while a `CHANGE` value is still a placeholder. Server mode adds a worker container, which runs refreshes, schedules, backups and emails. The full walk-through for a Proxmox container, with Nginx and Caddy configurations, firewalling, email, backups to NFS, restore, Google set-up and an install checklist, is in [deploy/proxmox/README.md](deploy/proxmox/README.md).

### Signing in and sharing

With `AUTH_MODE=local` (email and password) or `AUTH_MODE=full` (also Sign in with Google), people sign in. On the first start `ADMIN_EMAIL` and `ADMIN_INITIAL_PASSWORD` create the first admin, who must choose a new password at the first sign-in.

- **People:** admins invite people from **Users**. An invitation is a one-time link (valid 7 days): with email on it is emailed, otherwise the admin passes it on. The person sets a password with it, or, with Google sign-in on, simply signs in with Google as that address. Admins can change roles, deactivate people, give them attributes for row-level security, put them in groups, and send password reset links. With email on, the sign-in page also has **Forgotten your password?**. Locked out completely? `docker compose exec api python -m opsis reset-link <email>` prints a link.
- **Roles:** viewers see published dashboards shared with them; editors build datasets, models and dashboards; admins can do everything.
- **Sharing a dashboard:** **Share** on the dashboard page shares it with people, groups or everyone, as viewers (the published version) or editors (the draft too).
- **Restricted datasets:** on a dataset's page an admin can restrict it to named people and groups. Dashboards built on it stay viewable by those they are shared with, with row-level security applied as the viewer.
- **Share links** (server mode, `FEATURE_PUBLIC_SHARE_LINKS=true`): a public, read-only link to the published version, optionally with an expiry and a password, revocable, with a view count. Row-level security shows nothing through a link unless its maker chose otherwise.
- **View as:** an admin can look at a dashboard's data as a particular person, or as a viewer with given attributes, to check row-level security.
- **Audit log:** sign-ins, people and group changes, sharing, links, publishing and every change made through MCP, for admins.
- **Access tokens** (**Account**, **Access tokens**) work for MCP clients and for scripts on the REST API, with the scopes read, edit, publish and admin; a token never does more than its owner may.

Filter values, cross-filters and drill levels are kept in the address bar, so a filtered view can be bookmarked or sent.

### Schedules and backups

- **Refresh schedules** (`FEATURE_SCHEDULED_REFRESH=true`, on by default on a server): a dataset's page has a **Refresh schedule** card with presets (every hour, every day at 06:00, weekdays, Mondays, monthly) or a cron expression, and shows the next runs before you save. Times are in `WORKSPACE_TIMEZONE`, at most every 15 minutes. **Schedules** lists what runs on its own. A run missed while the stack was down is not made up; the next one runs as usual.
- **Backups** (`FEATURE_BACKUPS=true`, on by default on a server): every night (`BACKUP_SCHEDULE`, 02:30) a backup file goes to the `backups` volume, holding the database, the uploaded files and each dataset's current data. The newest `BACKUP_RETENTION` (7) are kept. **Settings** lists them and has **Back up now**; so does `docker compose exec api python -m opsis backup`. Keep a copy of `.env` too: its `ENCRYPTION_KEY` is not in the backups, and without it saved connection passwords cannot be read.
- **Restore** replaces everything with a backup. The previous data is moved aside, not deleted:

```
docker compose stop api worker
docker compose run --rm api restore opsis-20261124-023000.tar --yes
docker compose up -d
```

- A **clean-up job** runs every night and removes ended sessions, used password links, long-ended share links and old finished jobs. The audit log is kept.

### Email

Set `FEATURE_EMAIL=true` and the `SMTP_*` values in `.env` (with sign-in on). Opsis then emails invitations and password reset links, offers **Forgotten your password?** on the sign-in page, and lets people have a dashboard emailed to them: **Email me** on a dashboard, then a schedule. Each email shows the dashboard's numbers as that person sees them (their access and row-level security, the filters at their defaults), a KPI's value or a short table for each visual, and a link to the dashboard. Their emailed dashboards are listed under **Account**, where they can stop them. **Settings**, **Send me a test email** checks the mail server. Messages are sent straight away and never stored.

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
| `docker compose exec api python -m opsis seed-demo` | Load the demo data |
| `docker compose exec api python -m opsis backup` | Write a backup now (to the `backups` volume) |
| `docker compose exec api python -m opsis reset-link <email>` | Print a one-time link to set someone's password |

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
| `FEATURE_EMAIL` | Email: invitations, password resets, emailed dashboards | `false` | `false` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY` | The mail server, and `starttls`, `ssl` or `none` | empty, `587`, `starttls` | your mail server |
| `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` | Sign-in to the mail server (both empty: none), and the sender | empty | the sender is required with email on |
| `FEATURE_ASSISTANT` | In-app AI panel | `true` | `true` |
| `ASSISTANT_PROVIDER` | `anthropic` or `ollama` | `ollama` | `anthropic` |
| `OLLAMA_URL`, `OLLAMA_MODEL`, `OLLAMA_NUM_CTX` | Where Ollama is, the model to use, and the context size asked for with each request | `http://host.docker.internal:11434`, `gemma4:12b`, `32768` | same, if used |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | Claude for the assistant | empty, `claude-opus-5-5` | required for `anthropic` |
| `ANTHROPIC_EFFORT`, `ANTHROPIC_FALLBACKS` | How much Claude thinks; re-run declined requests on another model | `medium`, `true` | same |
| `ASSISTANT_MAX_STEPS`, `ASSISTANT_MAX_OUTPUT_TOKENS`, `ASSISTANT_TIMEOUT_SECONDS`, `ASSISTANT_MAX_ROWS` | Budgets for one request to the assistant | `25`, `100000`, `600`, `100` | same |
| `FEATURE_BACKUPS` | Nightly backup job | `false` | `true` |
| `BACKUP_SCHEDULE`, `BACKUP_RETENTION` | When backups run (cron, workspace time), and how many are kept | `30 2 * * *`, `7` | same |
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
| `ENCRYPTION_KEY` | Encrypts saved connection passwords, and sign-in cookies for Google and share links | empty (generated and kept in the data volume) | required |
| `ADMIN_EMAIL`, `ADMIN_INITIAL_PASSWORD` | The first admin, created on the first start (sign-in modes) | not used | required, then ignored |
| `SESSION_IDLE_HOURS`, `SESSION_MAX_DAYS` | A session ends after this long unused, and after this long anyway | `12`, `14` | same |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Sign in with Google (`AUTH_MODE=full`) | not used | required for `full` |
| `GOOGLE_ALLOWED_DOMAINS`, `GOOGLE_ALLOWED_EMAILS` | Who may sign in with Google | not used | your domain |
| `GOOGLE_AUTO_PROVISION` | Give an allowed but uninvited Google user a viewer account | not used | `false` |
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
| `npm run smoke` | Playwright browser tests at http://localhost:8080, then behind nginx at http://localhost:8088/opsis/, then against a throwaway server-mode stack with sign-in, a fake Google, share links and access tokens. Add `-- --no-build` to skip the image rebuild, or `-- --root`, `-- --subpath`, `-- --server` for one stage |
| `npm run lock:backend` | Regenerate `backend/uv.lock` after editing `pyproject.toml` |
| `node scripts/benchmark.mjs --rows 10000000` | Load a synthetic dataset into the running stack, time ingestion, queries and a typical dashboard, then remove it |

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
