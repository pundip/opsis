# Opsis on a Windows laptop

This is a single-user install: no sign-in, reachable only from your own machine.
For a server with sign-in, see [deploy/proxmox/README.md](../proxmox/README.md).

## 1. Docker Desktop

Install Docker Desktop with the **WSL 2 backend** (the default), and Git for Windows.
Give WSL enough memory: Opsis runs queries in memory, and Docker Desktop's default can
be tight. Create or edit `%UserProfile%\.wslconfig`:

```ini
[wsl2]
memory=8GB
processors=4
```

Then run `wsl --shutdown` in a terminal and start Docker Desktop again. Docker Desktop
must be running (its whale icon in the tray) before the next step.

## 2. Opsis

In PowerShell or a command prompt:

```bash
git clone https://github.com/pundip/opsis.git
cd opsis
copy .env.local.example .env
docker compose up -d
```

The first start builds the image, which takes a few minutes; later starts take seconds.
`docker compose ps` shows `opsis-api-1` as `healthy` when it is ready. Then open
http://localhost:8080. There is no sign-in on a laptop: you are the built-in admin.

To have something to look at straight away, open **Settings** and choose **Load demo
data**. It makes three datasets of made-up giving data and a dashboard,
**Giving overview (demo)**. To connect your own data (files, PostgreSQL, MySQL, Google
Sheets, Salesforce), see the [README](../../README.md#your-own-data).

For development with hot reload (`npm run dev`), file watching is much faster when
the repository lives inside the WSL filesystem (for example `\\wsl$\Ubuntu\home\you\opsis`)
rather than on `C:\`.

## 3. Ollama (optional, for the in-app assistant)

Install Ollama for Windows from https://ollama.com and pull a model that can call
tools, for example `ollama pull gemma4:12b`, then set `OLLAMA_MODEL` in `.env`.
Opsis reaches it at `http://host.docker.internal:11434` (`OLLAMA_URL`). Running
Ollama natively uses the GPU; the `ollama` Compose profile runs it in a container
instead, on the CPU.

Nothing needs changing in Ollama itself: the assistant asks for a 32,768-token context
with each request (`OLLAMA_NUM_CTX` in `.env`), which its instructions and tools need.
The installer adds Ollama to Windows startup (`Ollama.lnk` in the Startup folder), so
it usually runs in the tray already; `curl http://localhost:11434/api/version` shows
whether it is.

To use Claude instead, set `ASSISTANT_PROVIDER=anthropic` and `ANTHROPIC_API_KEY` in
`.env`, then `docker compose up -d`.

## 4. Claude Code and Claude Desktop (MCP)

The repository's `.mcp.json` points Claude Code at `http://localhost:8080/mcp`, so
Claude Code started in the `opsis` folder finds the server by itself. From anywhere else:

```bash
claude mcp add --transport http opsis http://localhost:8080/mcp
```

Claude Desktop connects through the `mcp-remote` bridge. In
`%AppData%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "opsis": { "command": "npx", "args": ["-y", "mcp-remote", "http://localhost:8080/mcp"] }
  }
}
```

With `MCP_AUTH=token` (or for a server) add an access token, made under **Account**,
**Access tokens**: see [deploy/proxmox/README.md](../proxmox/README.md), section 6.

## 5. Backups and schedules (optional)

On a laptop both are off by default. To turn them on, set in `.env`:

```ini
FEATURE_SCHEDULED_REFRESH=true
FEATURE_BACKUPS=true
```

and run `docker compose up -d`. Datasets then get a **Refresh schedule** card, and a
backup is made every night at 02:30 if the laptop is on (**Settings** has **Back up
now**). Backups stay in Docker's `backups` volume; to copy one out:

```bash
docker compose cp api:/backups/opsis-20261124-023000.tar .
```

Restoring is described in the [README](../../README.md#schedules-and-backups).

## 6. Updating, stopping, removing

```bash
git pull
docker compose up -d --build
```

updates to the latest version (database changes are applied when the api starts).
`docker compose down` stops Opsis and keeps everything. `docker compose down -v` also
**deletes all data** (datasets, dashboards, backups): only for starting over.
