# Opsis

A self-hosted, AI-native business intelligence platform. Open source, Docker-based, and built so that a person and an AI agent edit the same dashboards.

**Status:** early development. The application shell, configuration system and database baseline exist. Connectors, the semantic layer, dashboards and the MCP server are being built milestone by milestone. See [PROGRESS.md](PROGRESS.md).

## Quick start

You need Docker (Docker Desktop on Windows, Docker Engine on Linux) and Git.

### Laptop

```
copy .env.local.example .env
docker compose up -d
```

(On macOS or Linux use `cp` in place of `copy`.) Open http://localhost:8080. There is no sign-in in local mode: you are the built-in admin, and the app only listens on your own machine.

### Server

```
cp .env.server.example .env
```

Edit `.env` and change every value marked `CHANGE`, then:

```
docker compose up -d
```

Server mode adds a worker and a Caddy reverse proxy. Sign-in (local accounts and Google) is part of a later milestone, so a server install currently shows a sign-in notice and nothing else.

### Useful commands

| Command | What it does |
|---|---|
| `docker compose ps` | Show the running services and their health |
| `docker compose logs api` | Show the API log, including configuration errors |
| `docker compose down` | Stop the stack (data is kept in named volumes) |
| `docker compose up -d --build` | Rebuild after pulling new code |

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
| `BIND_ADDRESS`, `HTTP_PORT` | Where the api container is published on the host | `127.0.0.1`, `8080` | `127.0.0.1`, `8080` |
| `PUBLIC_BASE_URL` | The address users type | `http://localhost:8080` | your https address |
| `WORKSPACE_TIMEZONE` | Timezone for relative dates such as "this month" | `Australia/Melbourne` | same |
| `WEEK_START` | `monday` or `sunday` | `monday` | same |
| `FISCAL_YEAR_START_MONTH` | First month of the financial year, 1 to 12 | `7` | same |
| `APP_NAME` | Product name shown in the UI | `Opsis` | same |

Unsafe or contradictory settings stop the api container at startup with a message that names the variable and the fix. For example, `AUTH_MODE=none` with `BIND_ADDRESS=0.0.0.0` is refused. To check a config without starting anything:

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
| `npm run test:backend` | pytest in the dev image, against a throwaway `opsis_test` database |
| `npm run lint:backend` | ruff and mypy (strict) |
| `npm run test:frontend` | TypeScript, ESLint and Vitest |
| `npm run lock:backend` | Regenerate `backend/uv.lock` after editing `pyproject.toml` |

Run `npm install` inside `frontend/` once before the frontend commands.

On Windows, hot reload uses file polling. It is noticeably faster when the repository lives inside the WSL filesystem.

In the Claude desktop app, `.claude/launch.json` defines two launch configurations: `opsis` (the built stack on port 8080) and `opsis-dev` (hot reload on port 5173).

## Documentation

- [BUILD_BRIEF.md](BUILD_BRIEF.md): the product spec and milestones
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, data flows, auth flows
- [docs/DECISIONS.md](docs/DECISIONS.md): decision log
- [spec/](spec): JSON Schemas for dashboards, semantic queries and visual manifests

## Licence

[Apache License 2.0](LICENSE).
